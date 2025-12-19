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

# ---------------------------------------------------------
# Flask 主應用程式
# ---------------------------------------------------------
app = Flask(__name__)

# 從環境變數取得 LINE 與 OpenAI 設定
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
openai.api_key = os.environ.get('OPENAI_API_KEY')

# LINE SDK 設定
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Heroku 對外網址，用來組合音檔 URL
HEROKU_BASE_URL = os.environ.get("HEROKU_BASE_URL")
if not HEROKU_BASE_URL:
    raise RuntimeError("請在 Heroku Config Vars 設定 HEROKU_BASE_URL，範例：https://你的heroku-app.herokuapp.com")

# 語音速度：百分比（給 OpenAI TTS 的 speed 參數使用）
# 例如 65 代表 0.65 倍速
TTS_RATE_PERCENT = int(os.environ.get("TTS_RATE_PERCENT", "65"))

# 是否啟用 pydub 後處理（例如音量 normalize）
TTS_POST_PROCESS = os.environ.get("TTS_POST_PROCESS", "").lower()  # 設為 "pydub" 才會啟用

# 嘗試載入 pydub（可選，不裝也能跑）
try:
    from pydub import AudioSegment
    PydubAvailable = True
except Exception:
    PydubAvailable = False


# ---------------------------------------------------------
# 工具函式區：語言偵測 / 文字清理 / 比對邏輯
# ---------------------------------------------------------

def detect_lang_by_gpt(text: str) -> str:
    """
    用 GPT 小模型判斷使用者輸入是不是「中文」。

    回傳值只會是兩種：
        - 'zh'    : 中文（繁體或簡體）
        - 'other' : 其他語言（包含英文、日文、韓文、越南文...）

    這裡只用來決定「走哪一條翻譯流程」，不做太細的分類。
    """
    if not text or not text.strip():
        # 空字串直接當作 non-zh 處理
        return "other"

    try:
        resp = openai.chat.completions.create(
            model="gpt-4o-mini",  # 輕量模型就夠用
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
        # 偵測失敗，保守起見當成 non-zh，走「翻成中文」那條路
        app.logger.warning(f"[LANG_DETECT_ERROR] {e}, fallback to 'other'")
        return "other"


def clean_tts_text(text: str):
    """
    TTS 專用字串清理：
    - 避免括號、太多標點符號導致朗讀很奇怪。
    - 不做語意修改，只做簡單正規化。

    備註：
    - 這邊把英文的 '.' 也一起合併成 '。'，
      主要是針對中文語音，若你想保留英文句點，可再細分處理。
    """
    if not text:
        return text

    cleaned_text = text

    # 移除容易讓 TTS 念出奇怪停頓的括號
    cleaned_text = re.sub(r'[{}\[\]<>]', ' ', cleaned_text)

    # 合併多個驚嘆號 / 問號 / 逗號 / 句號
    cleaned_text = re.sub(r'[!！]+', '！', cleaned_text)
    cleaned_text = re.sub(r'[?？]+', '？', cleaned_text)
    cleaned_text = re.sub(r'[，,]+', '，', cleaned_text)
    cleaned_text = re.sub(r'[。\.]+', '。', cleaned_text)

    # 多餘空白合併
    cleaned_text = re.sub(r'\s+', ' ', cleaned_text).strip()

    return cleaned_text


def normalize_spaces(s: str) -> str:
    """
    用來「比對原文 vs 修正後原文」的標準化：

    做的事只有：
    - 去掉「字串開頭 / 結尾」的空白
    - 把「連續的空白（空格、換行、tab）」壓成單一空白

    不會動：
    - 標點符號
    - 大小寫
    - 任何實際的文字內容

    目的：
    - 忽略「純空白差異」，只要有標點或文字真正改動，就視為 changed = True。
    """
    if s is None:
        return ""
    s = s.strip()
    s = re.sub(r'\s+', ' ', s)
    return s


def missing_english_punctuation(s: str) -> bool:
    """
    判斷「看起來像英文的句子」是否缺少結尾標點。

    規則：
    - 若字串中有英文字母（A-Z / a-z）
    - 且最後一個非空白字元 不是 . / ! / ?
    → 視為「缺少句尾標點」。

    只在「原文是英文」時使用：
    - 幫你嚴格要求英文完整句子要有句尾標點。
    """
    if not s:
        return False

    s = s.rstrip()

    # 是否包含英文字母
    has_alpha = re.search(r'[A-Za-z]', s) is not None
    if not has_alpha:
        # 沒英文字，不當英文句處理（例如純日文、純韓文）
        return False

    last_char = s[-1]
    if last_char in ['.', '!', '?']:
        # 已經有適當句尾標點
        return False

    # 沒有句尾標點 → 視為缺標點
    return True


# ---------------------------------------------------------
# Flask + LINE Webhook 入口
# ---------------------------------------------------------

@app.route("/callback", methods=['POST'])
def callback():
    """
    LINE 平台會把所有訊息 POST 到這個 /callback 路徑。

    流程：
    1. 取得 X-Line-Signature 並驗證
    2. 交給 handler 處理（觸發 handle_message 等 callback）
    """
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("[REQUEST_BODY] " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        # 簽名不正確，通常是 Channel Secret / Access Token 沒對好
        print("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'


# ---------------------------------------------------------
# 主邏輯：處理文字訊息
# ---------------------------------------------------------

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """
    每當 LINE 收到「文字訊息」時，就會呼叫這個函式。

    這裡負責：
    - 判斷語言（中文 or 其他）
    - 呼叫 OpenAI 做「校正 + 翻譯」
    - 判斷是否有實質修改（changed）
    - 組出要回給使用者的文字訊息
    - 呼叫 TTS，產生語音檔並回傳給使用者
    """
    user_message = event.message.text

    try:
        app.logger.info("### Translator bot v8 (zh -> en+ja, other -> zh, dual TTS for zh) ###")
        app.logger.info(f"[USER] {user_message!r}")  # 問句寫進 log

        # -------------------------------------------------
        # 1. 用 GPT 判斷使用者輸入是否為中文
        # -------------------------------------------------
        lang = detect_lang_by_gpt(user_message)
        app.logger.info(f"[LANG_DETECTED] {lang}")

        # -------------------------------------------------
        # 2. 根據語言決定 system prompt
        #    zh  : 中文 → 修正中文 → 英文 + 日文
        #    other: 非中文 → 修正原文 → 繁體中文
        # -------------------------------------------------
        if lang == "zh":
            # 中文輸入：同時產英文、日文翻譯
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
            # 自訂：代表會產出英文 + 日文兩種翻譯
            target_lang = "en_ja"
        else:
            # 非中文輸入：原文維持原語言，翻譯成繁體中文
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

        # -------------------------------------------------
        # 3. 呼叫 OpenAI：校正 + 翻譯
        # -------------------------------------------------
        response = openai.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.3
        )
        raw_reply = response.choices[0].message.content.strip()
        app.logger.info(f"[GPT_RAW_REPLY] {raw_reply!r}")

        # -------------------------------------------------
        # 4. 解析 GPT 回傳的 JSON
        # -------------------------------------------------
        import json
        corrected_source = user_message      # 預設先當作「沒修正」
        translation = raw_reply              # 預設翻譯就是 raw（避免解析失敗時還有東西）
        translation_en = None                # 中文輸入時：英文翻譯
        translation_ja = None                # 中文輸入時：日文翻譯

        try:
            data = json.loads(raw_reply)
            if isinstance(data, dict):
                corrected_source = data.get("corrected_source", corrected_source)
                if lang == "zh":
                    # 中文：抓英文與日文兩種翻譯
                    translation_en = data.get("translation_en")
                    translation_ja = data.get("translation_ja")
                    # 給後面 TTS 做 fallback 用（優先使用英文）
                    translation = translation_en or translation_ja or translation
                else:
                    # 非中文：只有一個「翻成繁體中文」的 translation
                    translation = data.get("translation", translation)
        except Exception as e:
            # 若 GPT 沒完全照我們定的 JSON 格式回覆，就會進到這裡
            app.logger.warning(f"[JSON_PARSE_ERROR] {e}")

        # 把修正後原文與翻譯都寫入 log，方便除錯與對照
        if lang == "zh":
            app.logger.info(f"[CORRECTED_ZH] {corrected_source!r}")
            app.logger.info(f"[TRANSLATION_EN] {translation_en!r}")
            app.logger.info(f"[TRANSLATION_JA] {translation_ja!r}")
        else:
            app.logger.info(f"[CORRECTED_ORIG] {corrected_source!r}")
            app.logger.info(f"[TRANSLATION_ZH] {translation!r}")

        # -------------------------------------------------
        # 5. 判斷「是否有實質修改」(changed)
        #    - 只忽略多餘空白
        #    - 只要標點、文字有差，就算 changed = True
        #    - 若英文句子缺少句尾標點，也視為有問題
        # -------------------------------------------------
        # 基本條件：忽略空白後，內容是否相同
        base_changed = normalize_spaces(corrected_source) != normalize_spaces(user_message)

        # 追加條件：原文為英文時，是否缺少句尾標點
        punct_missing = False
        if lang == "other":
            # 粗略判斷：如果英文字母比日文假名多，就當英文處理
            letters = len(re.findall(r'[A-Za-z]', corrected_source))
            kana = len(re.findall(r'[ぁ-ゖァ-ヺ]', corrected_source))
            if letters > kana:
                punct_missing = missing_english_punctuation(corrected_source)

        changed = base_changed or punct_missing
        app.logger.info(f"[CHANGED_FLAG] changed={changed}, base={base_changed}, punct_missing={punct_missing}")

        # -------------------------------------------------
        # 6. 組合要回給使用者看的文字訊息
        # -------------------------------------------------
        if lang == "zh":
            # 中文 → 英文 + 日文
            lines = []
            if changed:
                # 有實質修改才顯示「修正後原文」
                lines.append(f"修正後原文 (中文)：{corrected_source}")
            if translation_en:
                lines.append(f"翻譯 (英文)：{translation_en}")
            if translation_ja:
                lines.append(f"翻譯 (日文)：{translation_ja}")

            if not lines:
                # 理論上不會發生（至少有一個翻譯），這裡只是保底
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

        # 把最後要顯示的內容也寫進 log，方便檢查
        app.logger.info(f"[DISPLAY_TEXT] {display_text!r}")

        # -------------------------------------------------
        # 7. 準備 TTS 工作：
        #    - 中文輸入：同時產英文 + 日文兩個語音
        #    - 非中文輸入：只產一個「繁體中文」語音
        # -------------------------------------------------
        # tts_jobs，每一項為 (要念的文字, 使用的 voice, 檔名後綴 suffix)
        tts_jobs = []

        if lang == "zh":
            # 中文輸入：英文 + 日文
            if translation_en:
                tts_jobs.append((clean_tts_text(translation_en), "alloy", "en"))
            if translation_ja:
                tts_jobs.append((clean_tts_text(translation_ja), "alloy", "ja"))
        else:
            # 非中文輸入：只念翻譯好的繁體中文
            tts_jobs.append((clean_tts_text(translation), "alloy", "zh"))

        audio_files = []  # 之後放 (public_filename, duration_ms)

        def call_tts_with_text(input_text, voice):
            """
            封裝 OpenAI TTS API 呼叫。
            input_text: 要轉成語音的文字
            voice     : 使用哪一個 voice（這裡統一用 'alloy'）
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

        # -------------------------------------------------
        # 8. 逐一執行 TTS，產生 mp3 檔案
        # -------------------------------------------------
        for text_for_tts, voice, suffix in tts_jobs:
            if not text_for_tts:
                # 避免空字串造成 TTS 失敗
                continue

            # 利用 reply_token 當成檔名前綴，避免衝突
            base_name = f"{event.reply_token}_{suffix}.mp3"
            audio_path = f"/tmp/{base_name}"

            tts_response = call_tts_with_text(text_for_tts, voice)
            if tts_response.status_code != 200:
                # 某一個語音失敗，就跳過那個，但不影響其他語音
                app.logger.error(f"[TTS_FAILED] suffix={suffix}, body={tts_response.text}")
                continue

            # 寫入 /tmp 底下的 mp3 檔
            with open(audio_path, "wb") as f:
                f.write(tts_response.content)

            final_audio_path = audio_path

            # 若有啟用 pydub，做後處理（例如 normalize 音量）
            if TTS_POST_PROCESS == "pydub" and PydubAvailable:
                try:
                    sound = AudioSegment.from_file(final_audio_path, format="mp3")
                    sound = sound.normalize()
                    processed_path = f"/tmp/processed_{base_name}"
                    sound.export(processed_path, format="mp3")
                    final_audio_path = processed_path
                except Exception as e:
                    app.logger.warning(f"[PYDUB_ERROR] suffix={suffix}, error={e}")

            # 讀取音檔長度（ms），LINE 需要這個資訊
            try:
                audio_info = MP3(final_audio_path)
                duration = int(audio_info.info.length * 1000)
            except Exception as e:
                app.logger.warning(f"[MP3_DURATION_ERROR] suffix={suffix}, error={e}")
                duration = 3000  # 失敗時給個預設 3 秒

            # 若有經過 pydub 處理，要用處理後的檔名
            if final_audio_path != audio_path:
                public_filename = os.path.basename(final_audio_path)
            else:
                public_filename = base_name

            audio_files.append((public_filename, duration))

        # -------------------------------------------------
        # 9. 回傳給 LINE 使用者：文字 + 1~2 個語音訊息
        # -------------------------------------------------
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)

            # 先放文字訊息
            messages = [TextMessage(text=display_text)]

            # 再附上所有產出的語音訊息
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
        # 任何沒預期的錯誤，都 log 下來，並回一則道歉訊息給使用者
        app.logger.exception(f"[HANDLE_MESSAGE_ERROR] {e}")
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
            # 若連 reply 都失敗，就忽略（通常是 reply_token 過期）
            pass


# ---------------------------------------------------------
# 提供靜態音檔下載（給 LINE 播放語音用）
# ---------------------------------------------------------

@app.route("/static/<filename>")
def serve_audio(filename):
    """
    讓 LINE 用這個 URL 下載我們剛剛存在 /tmp 的 mp3 檔。
    例如：
        https://你的app.herokuapp.com/static/xxxx_en.mp3
    """
    return send_from_directory("/tmp", filename)


# ---------------------------------------------------------
# 本機開發啟動點 (Heroku 也會從這裡啟動)
# ---------------------------------------------------------

if __name__ == "__main__":
    # 設定 log level：INFO 以上都會顯示
    import logging
    logging.basicConfig(level=logging.INFO)

    port = int(os.environ.get("PORT", 5000))
    # host 設為 0.0.0.0 方便在 Docker / Heroku 運行
    app.run(host="0.0.0.0", port=port)