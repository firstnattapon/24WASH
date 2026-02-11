import os
import re
import time
import logging
import requests
import firebase_admin
from firebase_admin import credentials, db
from flask import abort

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest,
    TextMessage, MessagingApiBlob
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, ImageMessageContent
)

# ==========================================
# 1. CONFIGURATION
# ==========================================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', 'YOUR_SECRET')
SLIPOK_BRANCH_ID = os.environ.get('SLIPOK_BRANCH_ID', '59844')
SLIPOK_API_KEY = os.environ.get('SLIPOK_API_KEY', 'SLIPOK_KEY')
FIREBASE_DB_URL = os.environ.get('FIREBASE_DB_URL', 'YOUR_DB_URL')

# --- Machine Mapping (ระบุเครื่องด้วยยอดเงิน) ---
# Key = ยอดเงินใน String, Value = prefix ของ Firebase path
# SlipOK ส่ง amount เป็น int (20) หรือ float (20.0, 30.01)
# ดังนั้นต้องรองรับทั้งสองแบบ
MACHINE_MAPPING_SLIP = {
    "20.0":  "20",
    # "30.0":  "30",
    "30.01": "301",
    "40.0":  "40",
    "50.0":  "50",
    # กรณี SlipOK ส่งมาเป็น integer (เช่น 20)
    "20":    "20",
    # "30":    "30",
    "40":    "40",
    "50":    "50",
}

MACHINE_PATH_MAP_COUPON = {
    "1": "20/payment_commands",
    "2": "302/payment_commands",
    "3": "301/payment_commands",
    "4": "payment_commands",
    # "5": "50/payment_commands",
}

DEFAULT_PATH = "payment_commands"

# --- SlipOK Error Codes ที่ให้ผ่าน (สลิปจริง) ---
# 1009 = ธนาคารขัดข้องชั่วคราว (สลิปจริง แต่ยังเช็คไม่ได้)
# 1010 = ธนาคาร BBL/SCB ต้องรอหลังโอน (สลิปจริง แต่ยังไม่ถึงเวลา)
SLIPOK_BYPASS_CODES = {1009, 1010}

# ==========================================
# 2. INITIALIZE SERVICES
# ==========================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not firebase_admin._apps:
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_DB_URL})

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ==========================================
# 3. HELPER FUNCTIONS
# ==========================================

def get_target_path_from_amount(amount):
    """
    เลือก Firebase path จากยอดเงิน
    หลักการ: ยอดเงินระบุเครื่อง (20.00 / 30.00 / 30.01 / 40.00 / 50.00)
    """
    if amount is None:
        return DEFAULT_PATH

    try:
        # แปลงเป็น String เพื่อเทียบกับ Key ใน Dictionary ตรงๆ
        amt_str = str(amount)
        if amt_str in MACHINE_MAPPING_SLIP:
            prefix = MACHINE_MAPPING_SLIP[amt_str]
            return f"{prefix}/payment_commands"

        # กรณี float .0 — เช่น SlipOK ส่ง 20.0 แต่เราตั้ง "20" ไว้ หรือกลับกัน
        amt_float = float(amount)
        if amt_float.is_integer():
            amt_int_str = str(int(amt_float))
            if amt_int_str in MACHINE_MAPPING_SLIP:
                prefix = MACHINE_MAPPING_SLIP[amt_int_str]
                return f"{prefix}/payment_commands"

    except Exception as e:
        logger.error(f"Error parsing amount: {e}")

    return DEFAULT_PATH


def push_command_to_firebase(data, path=None):
    """Push command ไปยัง Firebase — มี error handling"""
    target_path = path if path else DEFAULT_PATH
    try:
        ref = db.reference(target_path)
        ref.push(data)
        logger.info(f"Pushed to [{target_path}]: {data}")
        return True
    except Exception as e:
        logger.error(f"Firebase push error [{target_path}]: {e}")
        return False


def check_and_redeem_coupon(code):
    """
    ตรวจสอบคูปอง (ยังไม่ลบ — ลบหลัง push สำเร็จ)
    Returns: (exists, coupon_value)
    """
    try:
        ref = db.reference(f'coupons/{code}')
        snapshot = ref.get()
    except Exception as e:
        logger.error(f"Coupon read error: {e}")
        return False, 0

    if snapshot:
        coupon_value = 0
        if isinstance(snapshot, dict):
            coupon_value = float(snapshot.get('value', 0))
        elif isinstance(snapshot, (int, float, str)):
            try:
                coupon_value = float(snapshot)
            except (ValueError, TypeError):
                pass
        return True, coupon_value

    return False, 0


def delete_coupon(code):
    """ลบคูปองหลัง push สำเร็จแล้ว"""
    try:
        ref = db.reference(f'coupons/{code}')
        ref.delete()
    except Exception as e:
        logger.error(f"Coupon delete error: {e}")


def check_slip_with_slipok(image_binary):
    """
    ตรวจสอบสลิปกับ SlipOK API

    Returns: (is_valid, slip_data)
        - HTTP 200 + success     → (True, {amount, transRef, ...})
        - Error 1009/1010        → (True, None)  ← ผ่านเลย สลิปจริง
        - Error 1012/1013/1014   → (False, None)  ← SlipOK handle แล้ว
        - อื่นๆ                   → (False, None)
    """
    url = f"https://api.slipok.com/api/line/apikey/{SLIPOK_BRANCH_ID}"
    headers = {"x-authorization": SLIPOK_API_KEY}
    files = {"files": ("slip.jpg", image_binary, "image/jpeg")}
    # Note: multipart/form-data ส่งค่าเป็น string อยู่แล้ว → "true" ใช้ได้ปกติ
    data = {"log": "true"}

    try:
        response = requests.post(url, headers=headers, files=files, data=data, timeout=15)
        res_json = response.json()

        # ✅ สลิปถูกต้อง — มีข้อมูลครบ
        if response.status_code == 200 and res_json.get('success'):
            return True, res_json.get('data')

        # --- Handle Error Codes ---
        error_code = res_json.get('code')

        # ✅ สลิปจริง แต่ธนาคารยังไม่พร้อม → ผ่านเลย
        if error_code in SLIPOK_BYPASS_CODES:
            logger.info(f"SlipOK bypass: code={error_code}, msg={res_json.get('message')}")
            return True, None

        # ❌ สลิปไม่ผ่าน (1012 ซ้ำ / 1013 ยอดไม่ตรง / 1014 ผิดบัญชี / อื่นๆ)
        logger.warning(f"SlipOK rejected: code={error_code}, msg={res_json.get('message')}")
        return False, None

    except requests.exceptions.Timeout:
        logger.error("SlipOK timeout")
        return False, None
    except Exception as e:
        logger.error(f"SlipOK error: {e}")
        return False, None


def safe_reply(line_bot_api, reply_token, text):
    """Reply ด้วย error handling — ป้องกัน reply token หมดอายุ"""
    try:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )
    except Exception as e:
        logger.error(f"Reply failed: {e}")


# ==========================================
# 4. LINE EVENT HANDLERS
# ==========================================

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    text = event.message.text.strip()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # --- Help Command ---
        if text.upper() == "KEY":
            safe_reply(line_bot_api, event.reply_token,
                       "🔑 วิธีใช้คูปอง\nพิมพ์รหัสตามด้วยหมายเลขเครื่อง\nเช่น 12345-1 (ซ้ายไปขวา)")
            return

        # --- Coupon Logic ---
        # รองรับ: 12345-1, 12345 1, 1234501
        match_machine = re.match(r'^(\d{5})[- ]?0?([1-9])$', text)
        match_code_only = re.match(r'^(\d{5})$', text)

        if match_machine:
            code = match_machine.group(1)
            machine_num = match_machine.group(2)

            exists, _ = check_and_redeem_coupon(code)

            if exists:
                timestamp = int(time.time() * 1000)
                target_path = MACHINE_PATH_MAP_COUPON.get(machine_num, DEFAULT_PATH)

                command_data = {
                    "status": "work",
                    "method": "coupon",
                    "code": code,
                    "selected_machine": machine_num,
                    "transRef": f"coupon-{code}-{timestamp}",
                    "timestamp": timestamp
                }

                # Push ก่อน → ลบคูปองหลัง (ป้องกันคูปองหายเปล่า)
                if push_command_to_firebase(command_data, target_path):
                    delete_coupon(code)
                    safe_reply(line_bot_api, event.reply_token,
                               f"✅ รหัสถูกต้อง!\nสั่งงานเครื่องที่ {machine_num} เรียบร้อย")
                else:
                    safe_reply(line_bot_api, event.reply_token,
                               "❌ ระบบขัดข้อง กรุณาลองใหม่")
            else:
                safe_reply(line_bot_api, event.reply_token,
                           "❌ รหัสไม่ถูกต้อง หรือถูกใช้ไปแล้ว")
            return

        elif match_code_only:
            safe_reply(line_bot_api, event.reply_token,
                       f"⚠️ กรุณาระบุเลขเครื่อง\nพิมพ์เช่น: {text}-1")
            return


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    message_id = event.message.id

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob = MessagingApiBlob(api_client)

        # Get Image Content
        message_content = line_bot_blob.get_message_content(message_id)

        # Check Slip
        is_valid, slip_data = check_slip_with_slipok(message_content)

        if is_valid:
            timestamp = int(time.time() * 1000)

            if slip_data:
                # ✅ HTTP 200 — มีข้อมูลครบ
                amount = slip_data.get('amount')
                trans_ref = slip_data.get('transRef')
                target_path = get_target_path_from_amount(amount)
            else:
                # ✅ Bypass (1009/1010) — สลิปจริงแต่ไม่มีข้อมูลยอด
                amount = None
                trans_ref = f"bypass-{timestamp}"
                target_path = DEFAULT_PATH

            command_data = {
                "status": "work",
                "method": "slip",
                "amount": amount,
                "transRef": trans_ref,
                "timestamp": timestamp
            }

            push_command_to_firebase(command_data, target_path)

            safe_reply(line_bot_api, event.reply_token,
                       "✅ ได้รับยอดเงินเรียบร้อย\n*******เริ่มทำงาน*******")
        else:
            safe_reply(line_bot_api, event.reply_token,
                       "❌สลิปไม่ถูกต้องหรือซ้ำ\n*******โปรดลองใหม่*******")


# ==========================================
# 5. MAIN ENTRY POINT
# ==========================================
def line_webhook(request):
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return 'Error', 200  # Return 200 to stop LINE retries
    return 'OK'
