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

# 語速：百分比（會傳給 OpenAI TTS 的 speed 參數）
TTS_RATE_PERCENT = int(os.environ.get("TTS_RATE_PERCENT", "65"))
TTS_POST_PROCESS = os.environ.get("TTS_POST_PROCESS", "").lower()  # "pydub" 啟用後處理

# 可選後處理：pydub
try:
    from pydub import AudioSegment
    PydubAvailable = True
except Exception:
    PydubAvailable = False


# ---------- 語言偵測、糾正與文字清理 ----------

def detect_lang_by_gpt(text: str) -> str:
    """
    用小模型請 GPT 幫忙判斷語言。
    回傳:
      - 'zh'   : 中文（繁體或簡體）
      - 'other': 其他語言（包含日文、英文、韓文等）
    偵測失敗時預設回 'other'（保險起見一律翻成中文）。
    """
    if not text or not text.strip():
        return "other"

    try:
        resp = openai.chat.completions.create(
            model="gpt-4o-mini",  # 只做語言分類，小模型足夠
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a language detector. "
                        "If the user's message is in Chinese (Traditional or Simplified), reply with exactly 'zh'. "
                        "If it is any other language (including Japanese, English, Korean, etc.), reply with exactly 'other'. "
                        "Do not add anything else."
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.0,
        )
        ans = resp.choices[0].message.content.strip().lower()
        if ans == "zh":
            return "zh"
        else:
            return "other"
    except Exception as e:
        app.logger.warning(f"Language detect failed, fallback to 'other': {e}")
        return "other"


def clean_tts_text(text: str):
    """
    TTS 專用清理：避免一些括號、過多標點造成怪異讀法。
    不做特定詞彙替換，只做基本格式處理。
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


def normalize_spaces(s: str) -> str:
    """
    比較用的標準化：
    - 去掉首尾空白
    - 把多個空白壓成一個
    其餘（標點、大小寫）都保留，算差異。
    """
    if s is None:
        return ""
    s = s.strip()
    s = re.sub(r'\s+', ' ', s)
    return s


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
        app.logger.info("### Translator bot v6: zh->en, other->zh, correction shown only if meaningfully changed ###")
        app.logger.info(f"User message: {user_message!r}")

        # 1. 用 GPT 判斷是否為中文
        lang = detect_lang_by_gpt(user_message)
        app.logger.info(f"Detected lang: {lang}")

        # 2. 根據語言決定「先糾正＋翻譯」的 system prompt
        if lang == "zh":
            # 中文 → 修正成正確中文 → 翻成英文
            system_prompt = """
你是一個專業的翻譯與校正助手。請依照以下格式回覆（非常重要，必須嚴格遵守）：

1. 使用者輸入是中文（繁體或簡體）時：
   - 先把它校正成文法正確、自然流暢的中文（修正錯字與不自然用語）。
   - 再把「校正後的中文」翻譯成自然、流暢且專業的英文。

2. 回覆時一定要使用以下 JSON 格式，鍵名必須完全一致，不要多也不要少：
   {
     "corrected_source": "<校正後的中文句子>",
     "translation": "<對應的英文翻譯>"
   }

3. 不要在 JSON 外多加任何文字、說明或標註。
4. 專有名詞、商標和程式碼在合理情況下保留原樣。
5. 性相關或挑逗內容也要如實翻譯，但保持中性、自然的語氣。
"""
            target_lang = "en"
        else:
            # 非中文（例如日文、英文） → 先在該語言中修正 → 再翻成繁體中文
            system_prompt = """
你是一個專業的翻譯與校正助手。請依照以下格式回覆（非常重要，必須嚴格遵守）：

1. 使用者輸入「不是中文」時，無論原文是日文、英文、韓文、越南文或其他語言：
   - 先把原文校正成該語言中「文法正確、拼字正確、自然流暢」的句子，
     例如：日文的助詞錯誤、英文拼字錯誤，都先修正。
   - 使用「校正後的原文」翻譯成自然、流暢且專業的「繁體中文」。

2. 回覆時一定要使用以下 JSON 格式，鍵名必須完全一致，不要多也不要少：
   {
     "corrected_source": "<校正後的原文句子（保持原語言）>",
     "translation": "<對應的繁體中文翻譯>"
   }

3. 不要在 JSON 外多加任何文字、說明或標註。
4. 專有名詞、商標和程式碼在合理情況下保留原樣。
5. 性相關或挑逗內容也要如實翻譯，但保持中性、自然的語氣。
"""
            target_lang = "zh"

        # 3. GPT 校正 + 翻譯（回傳 JSON）
        response = openai.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.3
        )
        raw_reply = response.choices[0].message.content.strip()
        app.logger.info(f"GPT raw reply: {raw_reply!r}")

        # 4. 嘗試解析 JSON
        import json
        corrected_source = user_message
        translation = raw_reply
        try:
            data = json.loads(raw_reply)
            if isinstance(data, dict):
                corrected_source = data.get("corrected_source", corrected_source)
                translation = data.get("translation", translation)
        except Exception as e:
            app.logger.warning(f"Failed to parse JSON from GPT reply: {e}")

        # 判斷有沒有真的「被修改」
        # 只忽略多餘空白，其餘（標點、大小寫）都算差異
        changed = normalize_spaces(corrected_source) != normalize_spaces(user_message)

        # 5. 組合給使用者看的文字
        if lang == "zh":
            if changed:
                display_text = (
                    f"修正後原文 (中文)：{corrected_source}\n"
                    f"翻譯 (英文)：{translation}"
                )
            else:
                display_text = f"翻譯 (英文)：{translation}"
        else:
            if changed:
                display_text = (
                    f"修正後原文 (原語言)：{corrected_source}\n"
                    f"翻譯 (繁體中文)：{translation}"
                )
            else:
                display_text = f"翻譯 (繁體中文)：{translation}"

        # 6. TTS 只念翻譯那一部分
        tts_text = clean_tts_text(translation)
        app.logger.info(f"TTS text: {tts_text!r}")

        # 7. 根據「輸出語言」選擇 voice
        if target_lang == "zh":
            tts_voice = "alloy"   # 中文表現較好
        else:
            tts_voice = "nova"    # 英文聲線較自然

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

        # 8. 呼叫 TTS
        tts_response = call_tts_with_text(tts_text, tts_voice)

        if tts_response.status_code != 200:
            app.logger.error(f"TTS failed: {tts_response.text}")
            # TTS 失敗 → 只回文字
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=display_text)]
                    )
                )
            return

        # 儲存 mp3
        with open(audio_path, "wb") as f:
            f.write(tts_response.content)

        final_audio_path = audio_path

        # 9. pydub 後處理（選用）
        if TTS_POST_PROCESS == "pydub" and PydubAvailable:
            try:
                sound = AudioSegment.from_file(final_audio_path, format="mp3")
                sound = sound.normalize()
                processed_path = f"/tmp/processed_" + audio_filename
                sound.export(processed_path, format="mp3")
                final_audio_path = processed_path
            except Exception as e:
                app.logger.warning(f"pydub processing failed: {e}")

        # 10. 取得音檔長度
        try:
            audio_info = MP3(final_audio_path)
            duration = int(audio_info.info.length * 1000)
        except Exception as e:
            app.logger.warning(f"Failed to get audio duration: {e}")
            duration = 3000  # fallback

        # 11. 對外網址
        if final_audio_path != audio_path:
            public_filename = os.path.basename(final_audio_path)
        else:
            public_filename = audio_filename

        audio_url = f"{HEROKU_BASE_URL}/static/{public_filename}"

        # 12. 回覆 LINE 使用者（文字 + 語音）
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text=display_text),
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