import os
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, abort, send_from_directory
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.messaging.models import AudioMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import openai
import requests
from mutagen.mp3 import MP3

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
openai.api_key = os.environ.get('OPENAI_API_KEY')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

HEROKU_BASE_URL = os.environ.get("HEROKU_BASE_URL")
if not HEROKU_BASE_URL:
    raise RuntimeError("請在 Heroku Config Vars 設定 HEROKU_BASE_URL，範例：https://你的heroku-app.herokuapp.com")

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text
    try:
        # 1. GPT 回覆
        response = openai.chat.completions.create(
    model="gpt-4.1",
    messages=[
        {"role": "system", "content": """
        你是一個專業且中立的多語言語言助手（Professional Language Assistant）。請嚴格遵守下列規則，回覆風格務必專業、簡潔：

        1) 中↔越 自動翻譯規則（唯一自動雙向翻譯情境）
        - 若使用者輸入為中文（繁體或簡體）：僅回傳該中文的越南文翻譯。
        - 若使用者輸入為越南文：僅回傳該越南文的中文翻譯。
        - 自動翻譯時回傳內容只能是翻譯文本本身，不可加註、解釋、範例、語言標籤、引號或其他多餘文字或格式。
        - 若輸入同時包含中文與越南文且無明確翻譯目標：以使用者原語言簡短詢問確認，例如「請問您要把哪一段翻譯為哪種語言？」；在收到確認前不執行翻譯。
        - 中文字體變體處理：若使用者先前明確指定繁簡偏好，依該偏好；無明確指定時預設輸出繁體中文。

        2) 其他語言或非翻譯請求
        - 若使用者以非中文且非越南語言輸入，或明確提出非翻譯類型的問題（例如請求意見、程式碼、教學等），請以該語言用專業且完整的答案回覆，不自動執行中/越互譯。

        3) 翻譯品質與處理原則
        - 翻譯應準確、自然，保留原文意涵與語氣（以專業、中性為主）。
        - 專有名詞、商標、數字、程式碼或其他明顯不應翻譯的項目應保留原樣；如須音譯，選最常用形式，但在自動翻譯情境中不得附加說明。
        - 嚴格只輸出翻譯文字，不包含前綴或後續說明。

        4) 不確定或偵測失敗
        - 若無法可靠判斷使用者語言或偵測有歧義，先以使用者原語言作簡短提問確認，再依確認結果執行。
         
        5) 性相關
        - 性相關的言論也要照實翻譯,比如挑情或是性挑逗的言論。

        請依上述規則執行，僅修改回覆行為，其他情境維持專業且中立的助理角色。
        """}, 
                {"role": "user", "content": user_message}
            ]
        )
        ai_reply = response.choices[0].message.content.strip()

        # 2. TTS 合成語音（OpenAI TTS API）
        tts_response = requests.post(
            "https://api.openai.com/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {openai.api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": "tts-1",
                "input": ai_reply,
                "voice": "nova"
            }
        )
        # 儲存臨時語音檔
        audio_filename = f"{event.reply_token}.mp3"
        audio_path = f"/tmp/{audio_filename}"
        with open(audio_path, "wb") as f:
            f.write(tts_response.content)

        # 3. 用 mutagen 取得 mp3 長度（秒），轉成毫秒
        audio_info = MP3(audio_path)
        duration = int(audio_info.info.length * 1000)

        # 4. 公開語音檔案的網址（Heroku Flask 路由）
        audio_url = f"{HEROKU_BASE_URL}/static/{audio_filename}"

        # 5. 回覆 LINE 使用者（文字+語音）
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text=ai_reply),
                        AudioMessage(
                            original_content_url=audio_url,
                            duration=duration
                        )
                    ]
                )
            )
    except Exception as e:
        print(f"An error occurred: {e}")

# 讓 Heroku 可下載 .mp3 語音檔案
@app.route("/static/<filename>")
def serve_audio(filename):
    return send_from_directory("/tmp", filename)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)