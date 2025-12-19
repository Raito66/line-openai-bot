import os
import re
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

# 語速：百分比（0.5~2.0 倍之間的比例，這邊用 65%）
TTS_RATE_PERCENT = int(os.environ.get("TTS_RATE_PERCENT", "65"))
TTS_POST_PROCESS = os.environ.get("TTS_POST_PROCESS", "").lower()  # "pydub" to enable

# 可選後處理：pydub
try:
    from pydub import AudioSegment
    PydubAvailable = True
except Exception:
    PydubAvailable = False


# ---------- 語言偵測與文字清理 ----------

def detect_is_chinese(text: str) -> bool:
    """
    判斷輸入是否「主要是中文」：
    - 若包含足量 CJK 字元則視為中文。
    """
    if not text:
        return False
    # 若有不少於 2 個 CJK 字元，視為中文
    cjk_chars = re.findall(r'[\u4e00-\u9fff]', text)
    return len(cjk_chars) >= 2


def sanitize_translation(reply_text: str):
    """
    清理 GPT 回覆：避免前綴標籤、引號等多餘內容。
    由於系統 prompt 會要求只輸出翻譯文字，
    這裡主要作保險性處理。
    """
    if not reply_text:
        return reply_text

    s = reply_text.strip()

    # 常見前綴標籤
    patterns_to_remove = [
        r'^\s*(?:翻譯|Translation|譯文|中文翻譯|英文翻譯)[:：\-\s]*',
        r'^\s*\[?(Chinese|English|中文|英文)\]?\s*[:：\-\s]*',
    ]
    for pattern in patterns_to_remove:
        s = re.sub(pattern, '', s, flags=re.I)

    # 去掉首尾成對引號
    if (s.startswith('"') and s.endswith('"')) or \
       (s.startswith('「') and s.endswith('」')) or \
       (s.startswith('『') and s.endswith('』')):
        s = s[1:-1].strip()

    # 多餘空白
    s = re.sub(r'\s+', ' ', s).strip()
    return s or reply_text.strip()


def clean_tts_text(text: str):
    """
    TTS 專用清理：避免一些括號、過多標點造成怪異讀法。
    這裡不做重度改字，只做基本格式清理，以保留原意與語氣。
    """
    if not text:
        return text

    cleaned_text = text

    # 移除括號類符號（避免讀出）
    cleaned_text = re.sub(r'[{}\[\]<>]', ' ', cleaned_text)

    # 把多個標點做一點規整，避免連續奇怪停頓
    cleaned_text = re.sub(r'[!！]+', '！', cleaned_text)
    cleaned_text = re.sub(r'[?？]+', '？', cleaned_text)
    cleaned_text = re.sub(r'[，,]+', '，', cleaned_text)
    cleaned_text = re.sub(r'[。\.]+', '。', cleaned_text)

    # 多餘空白
    cleaned_text = re.sub(r'\s+', ' ', cleaned_text).strip()

    return cleaned_text


# ---------- Flask + LINE webhook ----------

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
        # 1. 判斷輸入語言：是否為中文
        is_chinese_input = detect_is_chinese(user_message)

        # 根據輸入語言決定翻譯方向
        # 中文 -> 英文；非中文 -> 中文
        if is_chinese_input:
            system_prompt = """
你是一個專業的翻譯助手。規則：

- 使用者輸入是中文（繁體或簡體），你只需要把它翻譯成自然、流暢且專業的英文。
- 僅輸出英文翻譯句子本身，不要任何多餘說明、標籤、引號、語言名稱或括號。
- 專有名詞、商標和程式碼請在合理情況下保留原樣。
- 性相關或挑逗內容也要如實翻譯，但保持中性、自然的語氣。
"""
            target_lang = "en"
        else:
            system_prompt = """
你是一個專業的翻譯助手。規則：

- 使用者輸入不是中文時，請判斷原文語言，並將其翻譯成自然、流暢且專業的繁體中文。
- 僅輸出繁體中文翻譯句子本身，不要任何多餘說明、標籤、引號、語言名稱或括號。
- 專有名詞、商標和程式碼請在合理情況下保留原樣。
- 性相關或挑逗內容也要如實翻譯，但保持中性、自然的語氣。
"""
            target_lang = "zh"

        # 2. GPT 翻譯
        response = openai.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.3
        )
        ai_reply = response.choices[0].message.content.strip()
        app.logger.info(f"GPT raw reply: {ai_reply!r}")

        # 3. 清理翻譯文字
        sanitized = sanitize_translation(ai_reply)
        if not sanitized:
            sanitized = ai_reply.strip()

        # 4. TTS 文字清理
        tts_text = clean_tts_text(sanitized)
        app.logger.info(f"Final TTS text: {tts_text!r}")

        # 5. 根據「輸出語言」選擇 voice
        #   - 翻成英文 -> 英文 voice 比較自然：nova
        #   - 翻成中文 -> 中文表現較好的：alloy
        if target_lang == "zh":
            tts_voice = "alloy"
        else:
            tts_voice = "nova"

        audio_filename = f"{event.reply_token}.mp3"
        audio_path = f"/tmp/{audio_filename}"

        def call_tts_with_text(input_text, voice):
            """呼叫 TTS API，使用純文字輸入"""
            try:
                resp = requests.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers={
                        "Authorization": f"Bearer {openai.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "tts-1",
                        "voice": voice,
                        "input": input_text,
                        "speed": TTS_RATE_PERCENT / 100.0
                    },
                    timeout=30
                )
                if resp.status_code != 200:
                    app.logger.error(f"TTS API error: {resp.status_code} - {resp.text}")
                return resp
            except Exception as e:
                app.logger.warning(f"TTS request exception: {e}")
                raise

        # 6. 呼叫 TTS
        tts_response = call_tts_with_text(tts_text, tts_voice)

        if tts_response.status_code != 200:
            app.logger.error(f"TTS failed: {tts_response.text}")
            # TTS 失敗 → 只回文字
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=sanitized)]
                    )
                )
            return

        # 儲存 mp3
        with open(audio_path, "wb") as f:
            f.write(tts_response.content)

        final_audio_path = audio_path

        # 7. pydub 後處理（選用）
        if TTS_POST_PROCESS == "pydub" and PydubAvailable:
            try:
                sound = AudioSegment.from_file(final_audio_path, format="mp3")
                sound = sound.normalize()
                processed_path = f"/tmp/processed_{audio_filename}"
                sound.export(processed_path, format="mp3")
                final_audio_path = processed_path
            except Exception as e:
                app.logger.warning(f"pydub processing failed: {e}")

        # 8. 取得音檔長度
        try:
            audio_info = MP3(final_audio_path)
            duration = int(audio_info.info.length * 1000)
        except Exception as e:
            app.logger.warning(f"Failed to get audio duration: {e}")
            duration = 3000  # fallback

        # 9. 對外網址
        if final_audio_path != audio_path:
            public_filename = os.path.basename(final_audio_path)
        else:
            public_filename = audio_filename

        audio_url = f"{HEROKU_BASE_URL}/static/{public_filename}"

        # 10. 回覆 LINE 使用者（文字 + 語音）
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text=sanitized),
                        AudioMessage(
                            original_content_url=audio_url,
                            duration=duration
                        )
                    ]
                )
            )

    except Exception as e:
        app.logger.exception(f"An error occurred: {e}")
        # 錯誤時至少回傳文字訊息
        try:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="抱歉，處理時發生錯誤，請稍後再試。")]
                    )
                )
        except Exception:
            pass


# 讓 Heroku 可下載 .mp3 語音檔案
@app.route("/static/<filename>")
def serve_audio(filename):
    return send_from_directory("/tmp", filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)