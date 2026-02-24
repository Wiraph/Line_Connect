import os
import sys
import logging
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import (
    WebhookHandler
)
from linebot.v3.exceptions import (
    InvalidSignatureError
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
    VideoMessageContent,
    AudioMessageContent
)
import requests
from dotenv import load_dotenv

# โหลดค่าจาก .env
load_dotenv()

# ตั้งค่า Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# 1. ตรวจสอบและโหลดค่า Configuration
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_API_URL = os.getenv("DIFY_API_URL")

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY, DIFY_API_URL]):
    logger.error("Missing environment variables. Please check your .env file.")
    sys.exit(1)

# 2. ตั้งค่า Line Bot SDK v3
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 3. ตัวแปรเก็บประวัติการคุย (Memory)
user_sessions = {}

@app.get("/")
async def root():
    return {"status": "ok", "message": "Line-Dify Middleware (v3) is running"}

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get('X-Line-Signature', '')
    body = await request.body()
    body_str = body.decode('utf-8')

    try:
        handler.handle(body_str, signature)
    except InvalidSignatureError:
        logger.warning("Invalid signature sample string")
        raise HTTPException(status_code=400, detail="Invalid signature")

    return 'OK'

# 4. ฟังก์ชันจัดการเมื่อได้รับข้อความ (Text)
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    user_message = event.message.text
    reply_token = event.reply_token

    conversation_id = user_sessions.get(user_id, "")
    logger.info(f"User: {user_id} | Message: {user_message}")

    process_and_reply(reply_token, user_message, user_id, conversation_id)

# 5. ฟังก์ชันจัดการเมื่อได้รับไฟล์ (Image, Video, Audio)
@handler.add(MessageEvent, message=[ImageMessageContent, VideoMessageContent, AudioMessageContent])
def handle_file_message(event):
    user_id = event.source.user_id
    reply_token = event.reply_token
    message_id = event.message.id
    message_type = event.message.type # image, video, audio

    conversation_id = user_sessions.get(user_id, "")
    logger.info(f"User: {user_id} | File Type: {message_type} | ID: {message_id}")

    try:
        # 1. Download file content from LINE
        with ApiClient(configuration) as api_client:
            line_bot_blob_api = MessagingApiBlob(api_client)
            file_content = line_bot_blob_api.get_message_content(message_id)

        # 2. Upload file to Dify
        # ตั้งชื่อไฟล์ชั่วคราวตาม ID
        extension_map = {"image": "jpg", "video": "mp4", "audio": "m4a"}
        extension = extension_map.get(message_type, "bin")
        filename = f"{message_id}.{extension}"
        
        dify_file_id = upload_file_to_dify(file_content, filename, user_id)

        if dify_file_id:
            logger.info(f"File uploaded to Dify. ID: {dify_file_id}")
            # 3. ส่งข้อความไปหา Dify พร้อมไฟล์ (ใช้ prompt ว่า [ไฟล์แนบ])
            process_and_reply(reply_token, f"[{message_type} uploaded]", user_id, conversation_id, dify_file_id)
        else:
            reply_text(reply_token, "ขออภัย ไม่สามารถอัปโหลดไฟล์ไปยัง AI ได้")

    except Exception as e:
        logger.error(f"Error handling file: {e}")
        reply_text(reply_token, "ขออภัย เกิดข้อผิดพลาดในการประมวลผลไฟล์")

def process_and_reply(reply_token, query, user_id, conversation_id, file_id=None):
    try:
        dify_response, new_conversation_id = call_dify_api(query, user_id, conversation_id, file_id)
        
        if new_conversation_id:
            user_sessions[user_id] = new_conversation_id

        reply_text(reply_token, dify_response)
            
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        reply_text(reply_token, "ขออภัย ระบบขัดข้องชั่วคราว")

def reply_text(reply_token, text):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                replyToken=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

def upload_file_to_dify(file_content, filename, user_id):
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}"
    }
    
    # Multipart upload
    files = {
        'file': (filename, file_content, 'application/octet-stream')
    }
    data = {
        'user': user_id
    }
    
    try:
        api_url = f"{DIFY_API_URL.rstrip('/')}/files/upload"
        response = requests.post(api_url, headers=headers, files=files, data=data)
        response.raise_for_status()
        
        return response.json().get('id')
    except Exception as e:
        logger.error(f"Error uploading to Dify: {e}")
        return None

def call_dify_api(query, user_id, conversation_id="", file_id=None):
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "inputs": {}, 
        "query": query,
        "response_mode": "blocking",
        "user": user_id,
        "files": []
    }

    if conversation_id:
        payload["conversation_id"] = conversation_id
        
    # ถ้ามีไฟล์แนบ ให้เพิ่มใน payload
    if file_id:
        payload["files"] = [
            {
                "type": "image", # Dify API อาจจะต้องการ type เป็น image สำหรับ Vision model
                "transfer_method": "local_file",
                "upload_file_id": file_id
            }
        ]
    
    try:
        api_url = f"{DIFY_API_URL.rstrip('/')}/chat-messages"
        response = requests.post(api_url, json=payload, headers=headers)
        response.raise_for_status()
        
        result = response.json()
        
        answer = result.get("answer", "ขออภัย ระบบขัดข้องชั่วคราว")
        new_conversation_id = result.get("conversation_id", "")

        import re
        answer = re.sub(r'<think>.*?</think>', '', answer, flags=re.DOTALL).strip()
        
        return answer, new_conversation_id
        
    except Exception as e:
        logger.error(f"Error calling Dify: {e}")
        if 'response' in locals():
             logger.error(f"Dify Response: {response.text}")
        raise e

def start_ngrok():
    from pyngrok import ngrok
    ngrok_auth = os.getenv("NGROK_AUTHTOKEN")
    if ngrok_auth:
        ngrok.set_auth_token(ngrok_auth)
    public_url = ngrok.connect(8000).public_url
    print(f"\n✅ Public URL ของคุณคือ: {public_url}")
    print(f"👉 ใส่ใน LINE Developers Webhook URL เป็น: {public_url}/callback\n")

if __name__ == "__main__":
    import uvicorn
    try:
        start_ngrok()
    except Exception as e:
        print(f"⚠️ ไม่สามารถรัน ngrok ได้: {e}")
        print("รัน Server ต่อโดยไม่มี Public URL...")
    
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)