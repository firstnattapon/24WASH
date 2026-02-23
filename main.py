import os
import re
import time
import logging
import requests
import json
import io
import firebase_admin
from google import genai
from google.genai import types
from google.genai.types import GenerateContentConfig
from PIL import Image
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
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET       = os.environ.get('LINE_CHANNEL_SECRET', '')
SLIPOK_BRANCH_ID          = os.environ.get('SLIPOK_BRANCH_ID', '')
SLIPOK_API_KEY            = os.environ.get('SLIPOK_API_KEY', '')
FIREBASE_DB_URL           = os.environ.get('FIREBASE_DB_URL', '')
# [P1 Fix] default='' (falsy) → guard ทำงานถูกต้อง 
# ถ้าไม่ set env var → gemini_client=None → fallback gracefully
GENAI_API_KEY             = os.environ.get('GENAI_API_KEY', '')

# ==========================================
# 2. INITIALIZE SERVICES
# ==========================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Gemini Client — timeout=30 กัน LINE Webhook retry เมื่อ Gemini ช้า
gemini_client = genai.Client(
    api_key=GENAI_API_KEY,
) if GENAI_API_KEY else None

GEMINI_MODEL  = "gemini-2.5-flash-lite"
GEMINI_CONFIG = GenerateContentConfig(
    temperature=0.0,
    response_mime_type="application/json",
)

# [P3] Startup validation log
logger.info(f"Gemini client: {'✅ ready' if gemini_client else '⚠️ NOT initialized (GENAI_API_KEY not set)'}")

# Firebase
if not firebase_admin._apps:
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_DB_URL})

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler       = WebhookHandler(LINE_CHANNEL_SECRET)

# ==========================================
# 3. CONSTANTS
# ==========================================
MACHINE_MAPPING_SLIP = {
    "20.0":  "20",
    "20":    "20",
    "30.0":  "30",
    "30":    "30",
    "30.01": "301",
    "40.0":  "40",
    "40":    "40",
    "50.0":  "50",
    "50":    "50",
}

MACHINE_PATH_MAP_COUPON = {
    "1": "20/payment_commands",
    "2": "302/payment_commands",
    "3": "301/payment_commands",
    "4": "30/payment_commands",
}

DEFAULT_PATH        = "payment_commands"
SLIPOK_BYPASS_CODES = {"1009", "1010"}  # ธนาคารล่ม/ช้า

# ==========================================
# 4. HELPER FUNCTIONS
# ==========================================

def get_target_path_from_amount(amount):
    """เลือก Firebase path จากยอดเงิน"""
    if amount is None:
        return None

    try:
        amt_str = str(amount)
        if amt_str in MACHINE_MAPPING_SLIP:
            return f"{MACHINE_MAPPING_SLIP[amt_str]}/payment_commands"

        amt_float = float(amount)
        if amt_float.is_integer():
            amt_int_str = str(int(amt_float))
            if amt_int_str in MACHINE_MAPPING_SLIP:
                return f"{MACHINE_MAPPING_SLIP[amt_int_str]}/payment_commands"

    except Exception as e:
        logger.error(f"Error parsing amount: {e}")

    return None


def push_command_to_firebase(data, path=None):
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
    try:
        ref      = db.reference(f'coupons/{code}')
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
            except Exception:
                pass
        return True, coupon_value
    return False, 0


def delete_coupon(code):
    try:
        db.reference(f'coupons/{code}').delete()
    except Exception as e:
        logger.error(f"Coupon delete error: {e}")


def check_slip_with_slipok(image_binary):
    """ตรวจสอบสลิปกับ SlipOK"""
    url     = f"https://api.slipok.com/api/line/apikey/{SLIPOK_BRANCH_ID}"
    headers = {"x-authorization": SLIPOK_API_KEY}
    files   = {"files": ("slip.jpg", image_binary, "image/jpeg")}
    data    = {"log": "true"}

    try:
        response = requests.post(url, headers=headers, files=files, data=data, timeout=10)
        res_json = response.json()

        if response.status_code == 200 and res_json.get('success'):
            return True, res_json.get('data')

        error_code = res_json.get('code')
        if str(error_code) in SLIPOK_BYPASS_CODES:
            logger.warning(f"SlipOK Delayed: {error_code} - Switching to AI")
            return True, None

        return False, None

    except Exception as e:
        logger.error(f"SlipOK error: {e}")
        return False, None


def optimize_image_for_gemini(image_binary):
    """ย่อรูปและลดคุณภาพ แล้วคืน bytes โดยตรง (ลด memory footprint)"""
    try:
        image    = Image.open(io.BytesIO(image_binary))
        max_size = 1024

        if max(image.size) > max_size:
            ratio    = max_size / max(image.size)
            new_size = (int(image.width * ratio), int(image.height * ratio))
            image    = image.resize(new_size, Image.Resampling.LANCZOS)

        if image.mode != 'RGB':
            image = image.convert('RGB')

        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=85)
        return img_byte_arr.getvalue()

    except Exception as e:
        logger.error(f"Image Optimization Error: {e}")
        return image_binary


def clean_json_text(text):
    """ล้าง Markdown backticks ออกจาก JSON string กัน json.loads แตก"""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def check_slip_with_gemini(image_binary):
    """ใช้ Gemini 2.5 Flash Lite อ่านสลิปเมื่อธนาคารล่ม (AI fallback)"""
    if not gemini_client:
        logger.error("Gemini client not initialized")
        return None, None

    try:
        optimized_bytes = optimize_image_for_gemini(image_binary)

        prompt = """
        You are a system to extract data from Thai bank slips.
        Analyze this image.
        1. "amount": The transfer amount (number only, float). Ignore balance available.
        2. "trans_ref": The transaction reference number.

        Return strictly JSON: {"amount": float, "trans_ref": string}
        """

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                prompt,
                types.Part.from_bytes(data=optimized_bytes, mime_type="image/jpeg"),
            ],
            config=GEMINI_CONFIG,
        )

        try:
            result = json.loads(clean_json_text(response.text))
            logger.info(f"Gemini Analysis: {result}")

            return result.get("amount"), result.get("trans_ref")
        except json.JSONDecodeError as e:
            logger.error(f"JSON Decode Error. Raw AI Response: {response.text}")
            return None, None

    except Exception as e:
        logger.error(f"Gemini AI Error: {e}")
        return None, None


def safe_reply(line_bot_api, reply_token, text):
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
# 5. LINE EVENT HANDLERS
# ==========================================

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    text = event.message.text.strip()
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        if text.upper() == "KEY":
            safe_reply(line_bot_api, event.reply_token,
                       "🔑 พิมพ์รหัสตามด้วยหมายเลขเครื่อง\nเช่น 12345-1 (นับจากซ้ายไปขวา)")
            return

        match_machine = re.match(r'^(\d{5})[- ]?0?([1-9])$', text)

        if match_machine:
            code, machine_num = match_machine.groups()
            exists, _         = check_and_redeem_coupon(code)

            if exists:
                timestamp    = int(time.time() * 1000)
                target_path  = MACHINE_PATH_MAP_COUPON.get(machine_num, DEFAULT_PATH)
                command_data = {
                    "status":           "work",
                    "method":           "coupon",
                    "code":             code,
                    "selected_machine": machine_num,
                    "transRef":         f"coupon-{code}-{timestamp}",
                    "timestamp":        timestamp,
                }
                if push_command_to_firebase(command_data, target_path):
                    delete_coupon(code)
                    safe_reply(line_bot_api, event.reply_token,
                               f"✅ รหัสถูกต้อง!\nสั่งงานเครื่องที่ {machine_num} เรียบร้อย")
                else:
                    safe_reply(line_bot_api, event.reply_token, "❌ ระบบขัดข้อง กรุณาลองใหม่")
            else:
                safe_reply(line_bot_api, event.reply_token, "❌ รหัสไม่ถูกต้อง")


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    message_id = event.message.id

    with ApiClient(configuration) as api_client:
        line_bot_api  = MessagingApi(api_client)
        line_bot_blob = MessagingApiBlob(api_client)

        # 1. ดึงรูปภาพ
        message_content_obj = line_bot_blob.get_message_content(message_id)
        
        # [Fix Bug 2] แปลง Object ให้เป็น Bytes
        if hasattr(message_content_obj, 'content'):
            message_content = message_content_obj.content
        elif hasattr(message_content_obj, 'read'):
            message_content = message_content_obj.read()
        else:
            try:
                message_content = b"".join(message_content_obj)
            except Exception as e:
                logger.error(f"Byte extraction failed: {e}")
                safe_reply(line_bot_api, event.reply_token, "❌ ไม่สามารถอ่านรูปภาพได้ กรุณาส่งใหม่อีกครั้ง")
                return

        # 2. เช็ค SlipOK (ด่านแรก)
        is_valid, slip_data = check_slip_with_slipok(message_content)

        if not is_valid:
            safe_reply(line_bot_api, event.reply_token, "❌ สลิปไม่ถูกต้อง/ซ้ำ/ยอดเงินไม่ตรง")
            return

        amount    = None
        trans_ref = None
        method    = "slip"
        timestamp = int(time.time() * 1000)

        # 3. แยกเคส: ปกติ vs ธนาคารดีเลย์
        if slip_data:
            # เคสปกติ: ได้ข้อมูลครบจาก SlipOK
            amount    = slip_data.get('amount')
            trans_ref = slip_data.get('transRef')
        else:
            # เคสดีเลย์ (1009/1010): ให้ AI ช่วยอ่าน
            ai_amount, ai_ref = check_slip_with_gemini(message_content)

            if ai_amount:
                amount    = ai_amount
                trans_ref = ai_ref or f"ai-{timestamp}"
                method    = "ai_fallback"
                logger.info(f"AI Found amount: {amount}")
            else:
                safe_reply(line_bot_api, event.reply_token,
                           "⚠️ ธนาคารขัดข้องและระบบอ่านยอดเงินไม่ได้\nกรุณาติดต่อแอดมิน")
                return

        # 4. หา Path และส่งคำสั่ง
        target_path = get_target_path_from_amount(amount)

        if target_path:
            command_data = {
                "status":    "work",
                "method":    method,
                "amount":    amount,
                "transRef":  trans_ref,
                "timestamp": timestamp,
            }
            if push_command_to_firebase(command_data, target_path):
                msg_prefix = "✅" if method == "slip" else "🤖(AI)"
                safe_reply(line_bot_api, event.reply_token,
                           f"{msg_prefix} ได้รับยอด {amount} บาท\n*******เริ่มทำงาน*******")
            else:
                # [P2 Fix] Firebase push fail → แจ้ง user แทน silent fail
                safe_reply(line_bot_api, event.reply_token, "❌ ระบบขัดข้อง กรุณาติดต่อแอดมิน")
        else:
            safe_reply(line_bot_api, event.reply_token,
                       f"⚠️ ยอดเงิน {amount} บาท ไม่ตรงกับราคาเครื่อง\nกรุณาติดต่อแอดมิน")


# ==========================================
# 6. MAIN ENTRY POINT
# ==========================================
def line_webhook(request):
    signature = request.headers.get('X-Line-Signature')
    body      = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return 'Error', 200
    return 'OK'
