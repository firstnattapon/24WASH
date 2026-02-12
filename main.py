import os
import re
import time
import logging
import requests
import json
import io
import firebase_admin
import google.generativeai as genai
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
# LINE & SlipOK Config
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_LINE_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', 'YOUR_LINE_SECRET')
SLIPOK_BRANCH_ID = os.environ.get('SLIPOK_BRANCH_ID', 'YOUR_SLIPOK_ID')
SLIPOK_API_KEY = os.environ.get('SLIPOK_API_KEY', 'YOUR_SLIPOK_KEY')
FIREBASE_DB_URL = os.environ.get('FIREBASE_DB_URL', 'YOUR_DB_URL')

# Gemini AI Config (NEW!)
GENAI_API_KEY = os.environ.get('GENAI_API_KEY', 'YOUR_GEMINI_API_KEY')
if GENAI_API_KEY:
    genai.configure(api_key=GENAI_API_KEY)

# ตั้งค่า Model Gemini Flash ให้ตอบเป็น JSON
MODEL_CONFIG = {
    "temperature": 0.0, # ให้นิ่งที่สุด ไม่ต้องครีเอทีฟ
    "response_mime_type": "application/json",
}
try:
    model = genai.GenerativeModel("gemini-1.5-flash", generation_config=MODEL_CONFIG)
except Exception as e:
    logging.error(f"Failed to initialize Gemini model: {e}")
    model = None

# --- Machine Mapping ---
MACHINE_MAPPING_SLIP = {
    "20.0":  "20",
    "30.01": "301",
    "40.0":  "40",
    "50.0":  "50",
    "20":    "20",
    "40":    "40",
    "50":    "50",
}

MACHINE_PATH_MAP_COUPON = {
    "1": "20/payment_commands",
    "2": "302/payment_commands",
    "3": "301/payment_commands",
    "4": "payment_commands",
}

DEFAULT_PATH = "payment_commands"
SLIPOK_BYPASS_CODES = {1009, 1010} # ธนาคารล่ม/ช้า

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
    """เลือก Firebase path จากยอดเงิน"""
    if amount is None:
        return None  # เปลี่ยนเป็น None เพื่อให้รู้ว่าหายอดไม่เจอจริงๆ

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

    return None # ถ้าไม่ตรงกับราคาเครื่องเลย ให้ส่งคืน None

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
            except: pass
        return True, coupon_value
    return False, 0

def delete_coupon(code):
    try:
        db.reference(f'coupons/{code}').delete()
    except Exception as e:
        logger.error(f"Coupon delete error: {e}")

def check_slip_with_slipok(image_binary):
    """ตรวจสอบสลิปกับ SlipOK"""
    url = f"https://api.slipok.com/api/line/apikey/{SLIPOK_BRANCH_ID}"
    headers = {"x-authorization": SLIPOK_API_KEY}
    files = {"files": ("slip.jpg", image_binary, "image/jpeg")}
    data = {"log": "true"}

    try:
        response = requests.post(url, headers=headers, files=files, data=data, timeout=10)
        res_json = response.json()

        if response.status_code == 200 and res_json.get('success'):
            return True, res_json.get('data')

        error_code = res_json.get('code')
        # ถ้าเป็น Error 1009/1010 (ธนาคารช้า) ให้ถือว่า Valid แต่ data=None
        if error_code in SLIPOK_BYPASS_CODES:
            logger.warning(f"SlipOK Delayed: {error_code} - Switching to AI")
            return True, None

        return False, None

    except Exception as e:
        logger.error(f"SlipOK error: {e}")
        return False, None

def optimize_image_for_gemini(image_binary):
    """ย่อรูปและลดคุณภาพเพื่อความเร็วในการส่งให้ AI"""
    try:
        image = Image.open(io.BytesIO(image_binary))
        
        # 1. Resize: ถ้าด้านยาวเกิน 1024px ให้ย่อลงมา
        max_size = 1024
        if max(image.size) > max_size:
            ratio = max_size / max(image.size)
            new_size = (int(image.width * ratio), int(image.height * ratio))
            image = image.resize(new_size, Image.Resampling.LANCZOS)
            
        # 2. Convert to RGB (เผื่อเป็น PNG)
        if image.mode != 'RGB':
            image = image.convert('RGB')
            
        # 3. Save to Bytes (JPEG Quality 85)
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=85)
        return img_byte_arr.getvalue()
        
    except Exception as e:
        logger.error(f"Image Optimization Error: {e}")
        return image_binary # ถ้า error ให้คืนค่าเดิมกลับไป

def check_slip_with_gemini(image_binary):
    """ใช้ Gemini Flash อ่านสลิปเมื่อธนาคารล่ม"""
    if not model:
        logger.error("Gemini model not initialized")
        return None, None

    try:
        # ✅ Optimize รูปก่อนส่ง
        optimized_image_binary = optimize_image_for_gemini(image_binary)
        
        # เปิดรูปด้วย PIL จาก optimized binary
        image = Image.open(io.BytesIO(optimized_image_binary))

        prompt = """
        You are a system to extract data from Thai bank slips.
        Analyze this image.
        1. "amount": The transfer amount (number only, float). Ignore balance available.
        2. "trans_ref": The transaction reference number.
        
        Return strictly JSON: {"amount": float, "trans_ref": string}
        """

        response = model.generate_content([prompt, image])
        result = json.loads(response.text)
        
        logger.info(f"Gemini Analysis: {result}")
        
        return result.get("amount"), result.get("trans_ref")
        
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
# 4. LINE EVENT HANDLERS
# ==========================================

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    text = event.message.text.strip()
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        if text.upper() == "KEY":
            safe_reply(line_bot_api, event.reply_token, "🔑 พิมพ์รหัสตามด้วยหมายเลขเครื่อง\nเช่น 12345-1")
            return

        match_machine = re.match(r'^(\d{5})[- ]?0?([1-9])$', text)
        
        if match_machine:
            code, machine_num = match_machine.groups()
            exists, _ = check_and_redeem_coupon(code)

            if exists:
                timestamp = int(time.time() * 1000)
                target_path = MACHINE_PATH_MAP_COUPON.get(machine_num, DEFAULT_PATH)
                command_data = {
                    "status": "work", "method": "coupon", "code": code,
                    "selected_machine": machine_num, "transRef": f"coupon-{code}-{timestamp}",
                    "timestamp": timestamp
                }
                if push_command_to_firebase(command_data, target_path):
                    delete_coupon(code)
                    safe_reply(line_bot_api, event.reply_token, f"✅ รหัสถูกต้อง!\nสั่งงานเครื่องที่ {machine_num} เรียบร้อย")
                else:
                    safe_reply(line_bot_api, event.reply_token, "❌ ระบบขัดข้อง กรุณาลองใหม่")
            else:
                safe_reply(line_bot_api, event.reply_token, "❌ รหัสไม่ถูกต้อง")

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    message_id = event.message.id

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob = MessagingApiBlob(api_client)

        # 1. ดึงรูปภาพ
        message_content = line_bot_blob.get_message_content(message_id)

        # 2. เช็ค SlipOK (ด่านแรก)
        is_valid, slip_data = check_slip_with_slipok(message_content)

        if not is_valid:
            safe_reply(line_bot_api, event.reply_token, "❌ สลิปไม่ถูกต้อง/ซ้ำ/ยอดเงินไม่ตรง")
            return

        # เตรียมตัวแปร
        amount = None
        trans_ref = None
        method = "slip"
        timestamp = int(time.time() * 1000)

        # 3. แยกเคส: ปกติ vs ธนาคารดีเลย์
        if slip_data:
            # ✅ เคสปกติ: ได้ข้อมูลครบจาก SlipOK
            amount = slip_data.get('amount')
            trans_ref = slip_data.get('transRef')
        else:
            # ⚠️ เคสดีเลย์ (1009/1010): ให้ AI ช่วยอ่าน
            # logger.info("Bank Delay -> Using Gemini AI Fallback")
            ai_amount, ai_ref = check_slip_with_gemini(message_content)
            
            if ai_amount:
                amount = ai_amount
                trans_ref = ai_ref or f"ai-{timestamp}"
                method = "ai_fallback"
                logger.info(f"AI Found amount: {amount}")
            else:
                # AI อ่านไม่ออกจริงๆ
                safe_reply(line_bot_api, event.reply_token, 
                           "⚠️ ธนาคารขัดข้องและระบบอ่านยอดเงินไม่ได้\nกรุณาติดต่อแอดมิน")
                return

        # 4. หา Path และส่งคำสั่ง
        target_path = get_target_path_from_amount(amount)
        
        if target_path:
            command_data = {
                "status": "work",
                "method": method,
                "amount": amount,
                "transRef": trans_ref,
                "timestamp": timestamp
            }
            if push_command_to_firebase(command_data, target_path):
                msg_prefix = "✅" if method == "slip" else "🤖(AI)"
                safe_reply(line_bot_api, event.reply_token, f"{msg_prefix} ได้รับยอด {amount} บาท\n*******เริ่มทำงาน*******")
        else:
            # ยอดเงินไม่ตรงกับราคาเครื่อง (เช่น โอนมา 21 บาท แต่เครื่องรับ 20, 30, 40)
            safe_reply(line_bot_api, event.reply_token, 
                       f"⚠️ ยอดเงิน {amount} บาท ไม่ตรงกับราคาเครื่อง\nกรุณาติดต่อแอดมิน")

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
        return 'Error', 200
    return 'OK'
