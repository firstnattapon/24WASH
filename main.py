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

# [ADJUSTMENT] ใช้ String เป็น Key เพื่อความแม่นยำ 100% ในการเทียบ
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
    """ เลือก Path จากยอดเงิน (แปลงเป็น String ก่อนเทียบ) """
    try:
        # แปลงเป็น String เพื่อเทียบกับ Key ใน Dictionary ตรงๆ
        amt_str = str(amount)
        if amt_str in MACHINE_MAPPING_SLIP:
            prefix = MACHINE_MAPPING_SLIP[amt_str]
            return f"{prefix}/payment_commands"
        
        # กรณีทศนิยม .0 (เช่น API ส่งมา 20.0 แต่เราอยากเทียบกับ 20)
        # หรือถ้า API ส่งมา 20 แต่เราตั้ง 20.0 ไว้
        amt_float = float(amount)
        # ลองเทียบแบบปัดเศษถ้าจำเป็น (Logic เสริม)
        if amt_float.is_integer():
             amt_int_str = str(int(amt_float))
             if amt_int_str in MACHINE_MAPPING_SLIP:
                 prefix = MACHINE_MAPPING_SLIP[amt_int_str]
                 return f"{prefix}/payment_commands"

    except Exception as e:
        logger.error(f"Error parsing amount: {e}")
    
    return DEFAULT_PATH

def push_command_to_firebase(data, path=None):
    target_path = path if path else DEFAULT_PATH
    ref = db.reference(target_path)
    ref.push(data)
    logger.info(f"✅ Pushed to [{target_path}]: {data}")

def check_and_redeem_coupon(code):
    """ ตรวจสอบและลบคูปอง """
    ref = db.reference(f'coupons/{code}')
    snapshot = ref.get()
    
    if snapshot:
        coupon_value = 0
        if isinstance(snapshot, dict):
            coupon_value = float(snapshot.get('value', 0))
        elif isinstance(snapshot, (int, float, str)):
             try: coupon_value = float(snapshot)
             except: pass

        # ลบคูปองทันที
        ref.delete()
        return True, coupon_value
        
    return False, 0

def check_slip_with_slipok(image_binary):
    url = f"https://api.slipok.com/api/line/apikey/{SLIPOK_BRANCH_ID}"
    headers = {"x-authorization": SLIPOK_API_KEY}
    files = {"files": ("slip.jpg", image_binary, "image/jpeg")}
    
    # Note: SlipOK ไม่จำเป็นต้องส่ง log=true ก็ได้ถ้าไม่ได้ใช้ Debug ฝั่ง Dashboard
    data = {"log": "true"} 

    try:
        response = requests.post(url, headers=headers, files=files, data=data, timeout=10)
        
        # เช็ค Status Code ก่อนแปลง JSON
        if response.status_code == 200:
            res_json = response.json()
            if res_json.get('success'):
                return True, res_json.get('data')
        
        logger.warning(f"SlipOK Failed: {response.text}")
        return False, None
    except Exception as e:
        logger.error(f"SlipOK Connection Error: {e}")
        return False, None

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
            reply_text = "🔑 วิธีใช้คูปอง\nพิมพ์รหัสตามด้วยหมายเลขเครื่อง\nเช่น 12345-1 (ซ้ายไปขวา)"
            line_bot_api.reply_message(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
            )
            return

        # --- Coupon Logic ---
        # รองรับ: 12345-1, 12345 1, 1234501
        match_machine = re.match(r'^(\d{5})[- ]?0?([1-9])$', text)
        match_code_only = re.match(r'^(\d{5})$', text)

        if match_machine:
            code = match_machine.group(1)
            machine_num = match_machine.group(2)

            success, _ = check_and_redeem_coupon(code)

            if success:
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

                push_command_to_firebase(command_data, target_path)
                
                reply_msg = f"✅ รหัสถูกต้อง!\nสั่งงานเครื่องที่ {machine_num} เรียบร้อย"
                line_bot_api.reply_message(
                    ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_msg)])
                )
            else:
                line_bot_api.reply_message(
                    ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❌ รหัสไม่ถูกต้อง หรือถูกใช้ไปแล้ว")])
                )
            return

        elif match_code_only:
            line_bot_api.reply_message(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"⚠️ กรุณาระบุเลขเครื่อง\nพิมพ์เช่น: {text}-1")])
            )
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
            amount = slip_data.get('amount') # อาจเป็น int หรือ float
            
            target_path = get_target_path_from_amount(amount)
            
            command_data = {
                "status": "work",
                "method": "slip",
                "amount": amount,
                "transRef": slip_data.get('transRef'),
                "timestamp": timestamp
            }
            
            push_command_to_firebase(command_data, target_path)
            
            line_bot_api.reply_message(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="✅ ได้รับยอดเงินเรียบร้อย\n******เริ่มทำงาน******")])
            )
        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❌สลิปไม่ถูกต้องหรือซ้ำ\n*****โปรดลองใหม่*****")])
            )

# ==========================================
# 5. MAIN ENTRY POINT
# ==========================================
def line_webhook(request):
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    
    # logger.info(f"Body: {body}") # Uncomment if debug needed

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        logger.error(f"Global Error: {e}")
        return 'Error', 200 # Return 200 to stop LINE retries

    return 'OK'
