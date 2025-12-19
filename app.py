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
      - 'other': 其他語言（包含日文、英文、韓文、越南文…）
    偵測失敗時預設回 'other'。
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
        app.logger.warning(f"Language detect failed, fallback to 'other': {e}")
        return "other"


def clean_tts_text(text: str):
    """
    TTS 專用清理：避免一些括號、過多標點造成怪異讀法。
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
    比較用標準化：
    - 去掉首尾空白
    - 把多個空白壓成一個
    其餘（標點、大小寫）全部保留，用來判斷是否真的有修改。
    """
    if s is None:
        return ""
    s = s.strip()
    s = re.sub(r'\s+', ' ', s)
    return s


def missing_english_punctuation(s: str) -> bool:
    """
    檢查「看起來像英文句子」是否缺少結尾標點：
    - 若句子裡有英文字母，且最後一個非空白字元不是 . ! ? 則視為「缺標點」。
    只用在原文為英文時。
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
        app.logger.info("### Translator bot v8 (zh -> en+ja, other -> zh, dual TTS for zh) ###")
        app.logger.info(f"[USER] {user_message!r}")  # <-- 問句寫進 log

        # 1. 判斷中文 / 非中文
        lang = detect_lang_by_gpt(user_message)
        app.logger.info(f"Detected lang: {lang}")

        # 2. system prompt
        if lang == "zh":
            # 中文 → 修正中文 → 同時翻成英文 & 日文
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
            # 非中文（英文/日文/韓文/越南文等） → 修正原文 → 翻成繁中
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
                {"role": "user", "content": user_message}
            ],
            temperature=0.3
        )
        raw_reply = response.choices[0].message.content.strip()
        app.logger.info(f"GPT raw reply: {raw_reply!r}")

        # 4. 解析 JSON
        import json
        corrected_source = user_message
        translation = raw_reply  # fallback
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
            app.logger.warning(f"Failed to parse JSON from GPT reply: {e}")

        # 把修正後原文和翻譯都寫進 log 方便對照  # <-- 新增 log
        if lang == "zh":
            app.logger.info(f"[CORRECTED_ZH] {corrected_source!r}")
            app.logger.info(f"[TRANSLATION_EN] {translation_en!r}")
            app.logger.info(f"[TRANSLATION_JA] {translation_ja!r}")
        else:
            app.logger.info(f"[CORRECTED_ORIG] {corrected_source!r}")
            app.logger.info(f"[TRANSLATION_ZH] {translation!r}")

        # 5. 判斷有沒有「實質修改」
        base_changed = normalize_spaces(corrected_source) != normalize_spaces(user_message)

        punct_missing = False
        if lang == "other":
            letters = len(re.findall(r'[A-Za-z]', corrected_source))
            kana = len(re.findall(r'[ぁ-ゖァ-ヺ]', corrected_source))
            if letters > kana:
                punct_missing = missing_english_punctuation(corrected_source)

        changed = base_changed or punct_missing
        app.logger.info(f"changed={changed}, base_changed={base_changed}, punct_missing={punct_missing}")  # <-- 新增 log

        # 6. 組合顯示文字
        if lang == "zh":
            # 中文 → 英文 + 日文
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
            # 其他語言 → 繁體中文
            if changed:
                display_text = (
                    f"修正後原文 (原語言)：{corrected_source}\n"
                    f"翻譯 (繁體中文)：{translation}"
                )
            else:
                display_text = f"翻譯 (繁體中文)：{translation}"

        app.logger.info(f"[DISPLAY] {display_text!r}")  # <-- 回給使用者的整段訊息也寫進 log

        # 7. 準備 TTS：中文輸入 → 兩個語音 (英文 + 日文)，其他 → 一個中文語音
        tts_jobs = []

        if lang == "zh":
            if translation_en:
                tts_jobs.append((clean_tts_text(translation_en), "alloy", "en"))
            if translation_ja:
                tts_jobs.append((clean_tts_text(translation_ja), "alloy", "ja"))
        else:
            tts_jobs.append((clean_tts_text(translation), "alloy", "zh"))

        audio_files = []

        def call_tts_with_text(input_text, voice):
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

        # 8. 逐個產 TTS 檔案
        for text_for_tts, voice, suffix in tts_jobs:
            if not text_for_tts:
                continue

            base_name = f"{event.reply_token}_{suffix}.mp3"
            audio_path = f"/tmp/{base_name}"

            tts_response = call_tts_with_text(text_for_tts, voice)
            if tts_response.status_code != 200:
                app.logger.error(f"TTS failed for {suffix}: {tts_response.text}")
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
                    app.logger.warning(f"pydub processing failed ({suffix}): {e}")

            try:
                audio_info = MP3(final_audio_path)
                duration = int(audio_info.info.length * 1000)
            except Exception as e:
                app.logger.warning(f"Failed to get audio duration ({suffix}): {e}")
                duration = 3000

            if final_audio_path != audio_path:
                public_filename = os.path.basename(final_audio_path)
            else:
                public_filename = base_name

            audio_files.append((public_filename, duration))

        # 9. 回 LINE（文字 + 一或兩個語音）
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)

            messages = [TextMessage(text=display_text)]
            for public_filename, duration in audio_files:
                audio_url = f"{HEROKU_BASE_URL}/static/{public_filename}"
                messages.append(
                    AudioMessage(
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
        app.logger.exception(f"An error occurred: {e}")
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


@app.route("/static/<filename>")
def serve_audio(filename):
    return send_from_directory("/tmp", filename)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)  # 本機跑時開 INFO log
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)