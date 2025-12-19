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
    MessagingApiBlob,  # <-- 新增：用來抓語音、圖片等二進位內容
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.messaging.models import AudioMessage as LineAudioMessage
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    AudioMessageContent,
)
import openai
import requests
from mutagen.mp3 import MP3

# =========================================================
# Flask / LINE / OpenAI 基本設定
# =========================================================

app = Flask(__name__)

# 從環境變數拿到 LINE 與 OpenAI 的金鑰
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
openai.api_key = os.environ.get('OPENAI_API_KEY')

# LINE SDK 設定物件
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 你的 Heroku 網址，拿來組語音檔的對外 URL
HEROKU_BASE_URL = os.environ.get("HEROKU_BASE_URL")
if not HEROKU_BASE_URL:
    raise RuntimeError("請在 Heroku Config Vars 設定 HEROKU_BASE_URL，範例：https://你的heroku-app.herokuapp.com")

# 語音播放速度（百分比轉成 0.x 倍速）
TTS_RATE_PERCENT = int(os.environ.get("TTS_RATE_PERCENT", "65"))

# 語音產生後是否使用 pydub 做後處理（例如 Normalize 音量）
TTS_POST_PROCESS = os.environ.get("TTS_POST_PROCESS", "").lower()  # 填 "pydub" 才會啟用

# 嘗試載入 pydub，沒裝也沒關係，只是不能後處理
try:
    from pydub import AudioSegment
    PydubAvailable = True
except Exception:
    PydubAvailable = False


# =========================================================
# 工具函式：語言偵測 / 空白標準化 / 標點檢查 / TTS 呼叫
# =========================================================

def detect_lang_by_gpt(text: str) -> str:
    """
    用 GPT 小模型判斷「這段文字是不是中文」。

    只分兩種：
      - 'zh'    : 中文（繁體 or 簡體）
      - 'other' : 其他語言（英文、日文、韓文、越南文... 全部算這一類）
    """
    if not text or not text.strip():
        return "other"

    try:
        resp = openai.chat.completions.create(
            model="gpt-4o-mini",
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
        app.logger.warning(f"[LANG_DETECT_ERROR] {e}, fallback to 'other'")
        return "other"


def clean_tts_text(text: str) -> str:
    """
    把要丟給 TTS 的文字稍微清理一下，避免朗讀起來太怪。
    """
    if not text:
        return text

    cleaned_text = text
    cleaned_text = re.sub(r'[{}\[\]<>]', ' ', cleaned_text)
    cleaned_text = re.sub(r'[!！]+', '！', cleaned_text)
    cleaned_text = re.sub(r'[?？]+', '？', cleaned_text)
    cleaned_text = re.sub(r'[，,]+', '，', cleaned_text)
    cleaned_text = re.sub(r'[。\.]+', '。', cleaned_text)
    cleaned_text = re.sub(r'\s+', ' ', cleaned_text).strip()
    return cleaned_text


def normalize_spaces(s: str) -> str:
    """
    只標準化「空白」，用來比較原文和修正後原文是否有實質差異。
    """
    if s is None:
        return ""
    s = s.strip()
    s = re.sub(r'\s+', ' ', s)
    return s


def missing_english_punctuation(s: str) -> bool:
    """
    看起來像英文句子，最後卻沒有 . ! ? 就回傳 True。
    """
    if not s:
        return False

    s = s.rstrip()
    has_alpha = re.search(r'[A-Za-z]', s) is not None
    if not has_alpha:
        return False

    last_char = s[-1]
    if last_char in ['.', '!', '?']:
        return False

    return True


def call_tts_with_text(input_text: str, voice: str):
    """
    呼叫 OpenAI TTS API，把文字轉語音。
    """
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
            app.logger.error(f"[TTS_API_ERROR] status={resp.status_code}, body={resp.text}")
        return resp
    except Exception as e:
        app.logger.warning(f"[TTS_REQUEST_EXCEPTION] {e}")
        raise


# =========================================================
# 核心：共用的「文字翻譯邏輯」
# =========================================================

def translate_text_with_logging(user_text: str):
    """
    核心翻譯流程（文字／語音共用）：
      1. 語言偵測（中文 or 其他）
      2. 根據語言選 prompt
      3. GPT 校正 + 翻譯（回 JSON）
      4. 判斷是否有實質修改 (changed)
      5. 組 display_text（顯示用文字）
      6. 準備 TTS 任務列表
    """
    app.logger.info(f"[USER_TEXT] {user_text!r}")

    # 1. 判斷中文 / 非中文
    lang = detect_lang_by_gpt(user_text)
    app.logger.info(f"[LANG_DETECTED] {lang}")

    # 2. system prompt
    if lang == "zh":
        system_prompt = """
你是一個專業的翻譯與校正助手。請依照以下格式回覆（非常重要，必須嚴格遵守）：

1. 使用者輸入是中文（繁體或簡體）時：
   - 先把它校正成文法正確、自然流暢的中文（修正錯字與不自然用語）。
   - 再把「校正後的中文」同時翻譯成自然、流暢且專業的英文與日文。

2. 回覆時一定要使用以下 JSON 格式，鍵名必須完全一致，不要多也不要少：
   {
     "corrected_source": "<校正後的中文句子>",
     "translation_en": "<對應的英文翻譯>",
     "translation_ja": "<對應的日文翻譯>"
   }

3. 不要在 JSON 外多加任何文字、說明或標註。
4. 專有名詞、商標和程式碼在合理情況下保留原樣。
5. 性相關或挑逗內容也要如實翻譯，但保持中性、自然的語氣。
6. 當你輸出英文翻譯時，如果是完整的句子，必須在句尾加上適當的標點符號（通常是句號 .，疑問句用 ?，感嘆句用 !）。
7. 當你輸出日文翻譯時，請使用自然的日文，句尾可以使用「。」也可以不用，但要保持自然。
"""
        target_lang = "en_ja"
    else:
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
6. 如果校正後的原文是一個「英文完整句子」，請在句尾加上適當的標點符號（通常是句號 .，疑問句用 ?，感嘆句用 !）。
"""
        target_lang = "zh"

    # 3. GPT 校正 + 翻譯
    response = openai.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
        temperature=0.3
    )
    raw_reply = response.choices[0].message.content.strip()
    app.logger.info(f"[GPT_RAW_REPLY] {raw_reply!r}")

    # 4. 解析 JSON
    import json
    corrected_source = user_text
    translation = raw_reply
    translation_en = None
    translation_ja = None

    try:
        data = json.loads(raw_reply)
        if isinstance(data, dict):
            corrected_source = data.get("corrected_source", corrected_source)
            if lang == "zh":
                translation_en = data.get("translation_en")
                translation_ja = data.get("translation_ja")
                translation = translation_en or translation_ja or translation
            else:
                translation = data.get("translation", translation)
    except Exception as e:
        app.logger.warning(f"[JSON_PARSE_ERROR] {e}")

    # log 校正後與翻譯
    if lang == "zh":
        app.logger.info(f"[CORRECTED_ZH] {corrected_source!r}")
        app.logger.info(f"[TRANSLATION_EN] {translation_en!r}")
        app.logger.info(f"[TRANSLATION_JA] {translation_ja!r}")
    else:
        app.logger.info(f"[CORRECTED_ORIG] {corrected_source!r}")
        app.logger.info(f"[TRANSLATION_ZH] {translation!r}")

    # 5. 判斷是否有實質修改
    base_changed = normalize_spaces(corrected_source) != normalize_spaces(user_text)
    punct_missing = False
    if lang == "other":
        letters = len(re.findall(r'[A-Za-z]', corrected_source))
        kana = len(re.findall(r'[ぁ-ゖァ-ヺ]', corrected_source))
        if letters > kana:
            punct_missing = missing_english_punctuation(corrected_source)
    changed = base_changed or punct_missing
    app.logger.info(f"[CHANGED_FLAG] changed={changed}, base={base_changed}, punct_missing={punct_missing}")

    # 6. 組 display_text
    if lang == "zh":
        lines = []
        if changed:
            lines.append(f"修正後原文 (中文)：{corrected_source}")
        if translation_en:
            lines.append(f"翻譯 (英文)：{translation_en}")
        if translation_ja:
            lines.append(f"翻譯 (日文)：{translation_ja}")
        if not lines:
            lines.append(f"翻譯 (英文)：{translation}")
        display_text = "\n".join(lines)
    else:
        if changed:
            display_text = (
                f"修正後原文 (原語言)：{corrected_source}\n"
                f"翻譯 (繁體中文)：{translation}"
            )
        else:
            display_text = f"翻譯 (繁體中文)：{translation}"

    app.logger.info(f"[DISPLAY_TEXT] {display_text!r}")

    # 7. 準備 TTS jobs
    tts_jobs = []
    if lang == "zh":
        if translation_en:
            tts_jobs.append((clean_tts_text(translation_en), "alloy", "en"))
        if translation_ja:
            tts_jobs.append((clean_tts_text(translation_ja), "alloy", "ja"))
    else:
        tts_jobs.append((clean_tts_text(translation), "alloy", "zh"))

    return display_text, tts_jobs


def run_tts_jobs(tts_jobs, reply_token):
    """
    執行一組 TTS job，回傳所有產好的 mp3 檔名＋長度。
    """
    audio_files = []

    for text_for_tts, voice, suffix in tts_jobs:
        if not text_for_tts:
            continue

        base_name = f"{reply_token}_{suffix}.mp3"
        audio_path = f"/tmp/{base_name}"

        tts_response = call_tts_with_text(text_for_tts, voice)
        if tts_response.status_code != 200:
            app.logger.error(f"[TTS_FAILED] suffix={suffix}, body={tts_response.text}")
            continue

        with open(audio_path, "wb") as f:
            f.write(tts_response.content)

        final_audio_path = audio_path

        if TTS_POST_PROCESS == "pydub" and PydubAvailable:
            try:
                sound = AudioSegment.from_file(final_audio_path, format="mp3")
                sound = sound.normalize()
                processed_path = f"/tmp/processed_{base_name}"
                sound.export(processed_path, format="mp3")
                final_audio_path = processed_path
            except Exception as e:
                app.logger.warning(f"[PYDUB_ERROR] suffix={suffix}, error={e}")

        try:
            audio_info = MP3(final_audio_path)
            duration = int(audio_info.info.length * 1000)
        except Exception as e:
            app.logger.warning(f"[MP3_DURATION_ERROR] suffix={suffix}, error={e}")
            duration = 3000

        if final_audio_path != audio_path:
            public_filename = os.path.basename(final_audio_path)
        else:
            public_filename = base_name

        audio_files.append((public_filename, duration))

    return audio_files


# =========================================================
# /callback 入口
# =========================================================

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("[REQUEST_BODY] " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'


# =========================================================
# 處理文字訊息
# =========================================================

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_message = event.message.text
    app.logger.info("### TEXT MESSAGE ###")
    app.logger.info(f"[USER_TEXT_RAW] {user_message!r}")

    try:
        display_text, tts_jobs = translate_text_with_logging(user_message)
        audio_files = run_tts_jobs(tts_jobs, event.reply_token)

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)

            messages = [TextMessage(text=display_text)]
            for public_filename, duration in audio_files:
                audio_url = f"{HEROKU_BASE_URL}/static/{public_filename}"
                messages.append(
                    LineAudioMessage(
                        original_content_url=audio_url,
                        duration=duration
                    )
                )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=messages
                )
            )
    except Exception as e:
        app.logger.exception(f"[HANDLE_TEXT_ERROR] {e}")
        try:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="抱歉，處理文字時發生錯誤，請稍後再試。")]
                    )
                )
        except Exception:
            pass


# =========================================================
# 處理語音訊息
# =========================================================

@handler.add(MessageEvent, message=AudioMessageContent)
def handle_audio_message(event):
    app.logger.info("### AUDIO MESSAGE ###")
    message_id = event.message.id
    app.logger.info(f"[USER_AUDIO_ID] {message_id}")

    # 1. 從 LINE 下載語音檔到 /tmp
    audio_path = f"/tmp/{message_id}.m4a"
    try:
        with ApiClient(configuration) as api_client:
            blob_api = MessagingApiBlob(api_client)  # <-- 用 Blob API 取二進位內容
            content_bytes = blob_api.get_message_content(message_id)  # 直接拿到 bytes

            with open(audio_path, 'wb') as fd:
                fd.write(content_bytes)
    except Exception as e:
        app.logger.exception(f"[DOWNLOAD_AUDIO_ERROR] {e}")
        try:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="抱歉，下載語音時發生錯誤，請稍後再試。")]
                    )
                )
        except Exception:
            pass
        return

    # 2. 語音轉文字 (Whisper)
    try:
        with open(audio_path, "rb") as f:
            transcript_resp = openai.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text"
            )
        transcript_text = transcript_resp.strip()
        app.logger.info(f"[AUDIO_TRANSCRIPT] {transcript_text!r}")
    except Exception as e:
        app.logger.exception(f"[ASR_ERROR] {e}")
        try:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="抱歉，語音辨識時發生錯誤，請稍後再試。")]
                    )
                )
        except Exception:
            pass
        return

    # 3. 把轉出來的文字丟進共用翻譯流程
    try:
        display_text, tts_jobs = translate_text_with_logging(transcript_text)
        audio_files = run_tts_jobs(tts_jobs, event.reply_token)

        final_display = f"語音辨識原文：{transcript_text}\n" + display_text

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)

            messages = [TextMessage(text=final_display)]
            for public_filename, duration in audio_files:
                audio_url = f"{HEROKU_BASE_URL}/static/{public_filename}"
                messages.append(
                    LineAudioMessage(
                        original_content_url=audio_url,
                        duration=duration
                    )
                )

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=messages
                )
            )
    except Exception as e:
        app.logger.exception(f"[HANDLE_AUDIO_ERROR] {e}")
        try:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="抱歉，處理語音時發生錯誤，請稍後再試。")]
                    )
                )
        except Exception:
            pass


# =========================================================
# 提供靜態音檔下載
# =========================================================

@app.route("/static/<filename>")
def serve_audio(filename):
    return send_from_directory("/tmp", filename)


# =========================================================
# 啟動
# =========================================================

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)