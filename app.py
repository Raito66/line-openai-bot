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

    這邊不用自己寫正則判斷語言，完全交給 GPT 小模型處理。
    """
    if not text or not text.strip():
        # 空字串 → 直接當作 non-zh 處理
        return "other"

    try:
        resp = openai.chat.completions.create(
            model="gpt-4o-mini",  # 小模型，便宜又快
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
        # 偵測失敗，就保守一點當成 "other"（翻成中文那條）
        app.logger.warning(f"[LANG_DETECT_ERROR] {e}, fallback to 'other'")
        return "other"


def clean_tts_text(text: str) -> str:
    """
    把要丟給 TTS 的文字稍微清理一下，避免朗讀起來太怪。

    做的事情：
      - 拿掉一些括號類符號
      - 把一串 !!! / ??? / ...... 合併成一個
      - 清掉多餘空白

    不會動到真正的內容，只是讓朗讀時的停頓比較自然。
    """
    if not text:
        return text

    cleaned_text = text

    # 去掉容易讓 TTS 唸出奇怪停頓的括號
    cleaned_text = re.sub(r'[{}\[\]<>]', ' ', cleaned_text)

    # 合併多個驚嘆號 / 問號 / 逗號 / 句號
    cleaned_text = re.sub(r'[!！]+', '！', cleaned_text)
    cleaned_text = re.sub(r'[?？]+', '？', cleaned_text)
    cleaned_text = re.sub(r'[，,]+', '，', cleaned_text)
    cleaned_text = re.sub(r'[。\.]+', '。', cleaned_text)

    # 多餘空白 → 單一空白
    cleaned_text = re.sub(r'\s+', ' ', cleaned_text).strip()

    return cleaned_text


def normalize_spaces(s: str) -> str:
    """
    用來比較「原文」與「修正後原文」的版本。

    我們只想「忽略空白差異」，所以做：
      1. 去掉頭尾空白
      2. 把中間連續多個空白，縮成一個空白

    不會改變：
      - 標點
      - 整個字串內容
      - 大小寫

    這樣只要標點或字不同，就會被視為「真的有修改」。
    """
    if s is None:
        return ""
    s = s.strip()
    s = re.sub(r'\s+', ' ', s)
    return s


def missing_english_punctuation(s: str) -> bool:
    """
    檢查一個字串「如果看起來像英文句子」，最後面有沒有句尾標點。

    規則：
      - 若字串中含有英文字母 (A-Z / a-z)
      - 且最後一個非空白字元不是 . 或 ! 或 ?
      → 回傳 True（代表缺少句尾標點）

    只在「原文是英文」時使用，用來嚴格要求英文完整句必須有標點。
    """
    if not s:
        return False

    s = s.rstrip()

    # 先檢查裡面有沒有英文字母
    has_alpha = re.search(r'[A-Za-z]', s) is not None
    if not has_alpha:
        # 沒有英文字，就不當英文句處理
        return False

    last_char = s[-1]
    if last_char in ['.', '!', '?']:
        # 已經有適當句尾標點
        return False

    # 沒有句尾標點
    return True


def call_tts_with_text(input_text: str, voice: str):
    """
    呼叫 OpenAI TTS API，把文字轉語音。

    參數：
      - input_text : 要念的文字
      - voice      : 使用哪一個 voice 名稱（例如 'alloy'）

    回傳：
      - requests.Response 物件（裡面的 content 就是 mp3）
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
    給一段「純文字」，走完整的「偵測語言 → 校正 → 翻譯 → 判斷 changed → 組 display_text → 準備 TTS」流程。

    這個函式給兩個地方共用：
      1. 使用者輸入文字的時候
      2. 使用者輸入語音 → 先轉文字 → 再丟進來

    回傳：
      - display_text : 要回給使用者的整段文字（沒有加「語音辨識原文」那一行，語音那邊會自己加）
      - tts_jobs     : 給 TTS 用的任務列表，每一個元素長這樣：
                       (text_for_tts, voice, suffix)
    """
    app.logger.info(f"[USER_TEXT] {user_text!r}")

    # 1. 判斷這段文字是中文還是其他語言
    lang = detect_lang_by_gpt(user_text)
    app.logger.info(f"[LANG_DETECTED] {lang}")

    # 2. 根據語言決定 system prompt（決定要生成什麼 JSON 結構）
    if lang == "zh":
        # 中文 → 修正中文 → 同時翻譯成英文 + 日文
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
        target_lang = "en_ja"  # 自訂標記：代表這次會有英文 + 日文翻譯
    else:
        # 非中文 → 修正原文 → 翻譯成繁體中文
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

    # 3. 呼叫 OpenAI：用 gpt-4 做「校正 + 翻譯」
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

    # 4. 嘗試把 GPT 回傳內容當作 JSON 解析
    import json
    corrected_source = user_text     # 預設：假裝沒修正
    translation = raw_reply         # 預設翻譯：整段回傳（避免 JSON 解析失敗時沒東西）
    translation_en = None           # 中文情況下的英文翻譯
    translation_ja = None           # 中文情況下的日文翻譯

    try:
        data = json.loads(raw_reply)
        if isinstance(data, dict):
            corrected_source = data.get("corrected_source", corrected_source)
            if lang == "zh":
                translation_en = data.get("translation_en")
                translation_ja = data.get("translation_ja")
                # 若英文/日文其中一個缺，就用另一個當 fallback
                translation = translation_en or translation_ja or translation
            else:
                translation = data.get("translation", translation)
    except Exception as e:
        app.logger.warning(f"[JSON_PARSE_ERROR] {e}")

    # 5. 把修正後原文跟翻譯內容寫進 log，方便除錯
    if lang == "zh":
        app.logger.info(f"[CORRECTED_ZH] {corrected_source!r}")
        app.logger.info(f"[TRANSLATION_EN] {translation_en!r}")
        app.logger.info(f"[TRANSLATION_JA] {translation_ja!r}")
    else:
        app.logger.info(f"[CORRECTED_ORIG] {corrected_source!r}")
        app.logger.info(f"[TRANSLATION_ZH] {translation!r}")

    # 6. 判斷這次有沒有「實質修改」
    #    只忽略空白差異，標點/文字差異都算
    base_changed = normalize_spaces(corrected_source) != normalize_spaces(user_text)

    # 若原文是英文，再加一條規則：沒有句尾標點也當作有問題
    punct_missing = False
    if lang == "other":
        # 粗略判斷是不是英文：英文字母多過日文假名，就當英文
        letters = len(re.findall(r'[A-Za-z]', corrected_source))
        kana = len(re.findall(r'[ぁ-ゖァ-ヺ]', corrected_source))
        if letters > kana:
            punct_missing = missing_english_punctuation(corrected_source)

    changed = base_changed or punct_missing
    app.logger.info(f"[CHANGED_FLAG] changed={changed}, base={base_changed}, punct_missing={punct_missing}")

    # 7. 組成要顯示給使用者看的文字 display_text
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
            # 理論上不會發生（至少會有英文或日文），這裡只是保底
            lines.append(f"翻譯 (英文)：{translation}")

        display_text = "\n".join(lines)
    else:
        # 非中文 → 繁體中文
        if changed:
            display_text = (
                f"修正後原文 (原語言)：{corrected_source}\n"
                f"翻譯 (繁體中文)：{translation}"
            )
        else:
            display_text = f"翻譯 (繁體中文)：{translation}"

    app.logger.info(f"[DISPLAY_TEXT] {display_text!r}")

    # 8. 準備 TTS 任務列表 tts_jobs
    #    每一項格式：(要念的文字, voice 名稱, 檔名後綴 suffix)
    tts_jobs = []
    if lang == "zh":
        # 中文：同時產英文＋日文兩段語音
        if translation_en:
            tts_jobs.append((clean_tts_text(translation_en), "alloy", "en"))
        if translation_ja:
            tts_jobs.append((clean_tts_text(translation_ja), "alloy", "ja"))
    else:
        # 非中文：只產一段「繁體中文翻譯」的語音
        tts_jobs.append((clean_tts_text(translation), "alloy", "zh"))

    # 把 display_text + tts_jobs 傳回給呼叫者（文字處理/語音處理共用）
    return display_text, tts_jobs


def run_tts_jobs(tts_jobs, reply_token):
    """
    根據 tts_jobs 清單，一個一個呼叫 TTS API 產 mp3 檔。

    參數：
      - tts_jobs   : [(text_for_tts, voice, suffix), ...]
      - reply_token: 用來組出獨特檔名，避免不同訊息衝突

    回傳：
      - audio_files: [(public_filename, duration_ms), ...]
                     供後面組合 LINE 的 AudioMessage 使用
    """
    audio_files = []

    for text_for_tts, voice, suffix in tts_jobs:
        if not text_for_tts:
            continue

        base_name = f"{reply_token}_{suffix}.mp3"
        audio_path = f"/tmp/{base_name}"

        # 呼叫 TTS API
        tts_response = call_tts_with_text(text_for_tts, voice)
        if tts_response.status_code != 200:
            app.logger.error(f"[TTS_FAILED] suffix={suffix}, body={tts_response.text}")
            continue

        # 把回傳的 mp3 內容寫到 /tmp
        with open(audio_path, "wb") as f:
            f.write(tts_response.content)

        final_audio_path = audio_path

        # 若有啟用 pydub，做個 Normalize 等後處理
        if TTS_POST_PROCESS == "pydub" and PydubAvailable:
            try:
                sound = AudioSegment.from_file(final_audio_path, format="mp3")
                sound = sound.normalize()
                processed_path = f"/tmp/processed_{base_name}"
                sound.export(processed_path, format="mp3")
                final_audio_path = processed_path
            except Exception as e:
                app.logger.warning(f"[PYDUB_ERROR] suffix={suffix}, error={e}")

        # 讀取音檔長度（毫秒），LINE 的 AudioMessage 需要這個欄位
        try:
            audio_info = MP3(final_audio_path)
            duration = int(audio_info.info.length * 1000)
        except Exception as e:
            app.logger.warning(f"[MP3_DURATION_ERROR] suffix={suffix}, error={e}")
            duration = 3000  # 若讀取失敗，就先給 3 秒當預設

        # 若有產生 processed_ 開頭的檔案，要用那個檔名
        if final_audio_path != audio_path:
            public_filename = os.path.basename(final_audio_path)
        else:
            public_filename = base_name

        audio_files.append((public_filename, duration))

    return audio_files


# =========================================================
# Flask / LINE Webhook 入口
# =========================================================

@app.route("/callback", methods=['POST'])
def callback():
    """
    LINE 平台會把所有訊息 POST 到這個 /callback。
    我們只做驗證簽名，然後交給 handler。
    """
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
# 文字訊息處理：使用者打字訊息
# =========================================================

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    """
    收到「文字訊息」時會進到這裡。

    步驟：
      1. 取出文字
      2. 丟給 translate_text_with_logging 做翻譯
      3. 執行 TTS，產生語音檔
      4. 回覆：文字 + 語音
    """
    user_message = event.message.text
    app.logger.info("### TEXT MESSAGE ###")
    app.logger.info(f"[USER_TEXT_RAW] {user_message!r}")

    try:
        # 共用核心：校正 + 翻譯 + 組 display + 準備 TTS 任務
        display_text, tts_jobs = translate_text_with_logging(user_message)

        # 實際呼叫 TTS，產生 mp3 檔資訊
        audio_files = run_tts_jobs(tts_jobs, event.reply_token)

        # 組合要回給 LINE 的訊息
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
            # 如果連回覆都失敗，只能放棄（可能 reply_token 過期）
            pass


# =========================================================
# 語音訊息處理：使用者錄音
# =========================================================

@handler.add(MessageEvent, message=AudioMessageContent)
def handle_audio_message(event):
    """
    收到「語音訊息」時會進到這裡。

    流程：
      1. 從 LINE 把語音檔下載到 /tmp
      2. 呼叫 OpenAI Whisper 做語音轉文字（ASR）
      3. 把轉出來的文字丟進 translate_text_with_logging（跟文字流程一模一樣）
      4. 執行 TTS，產生語音檔
      5. 回覆：顯示「語音辨識原文 + 翻譯結果」＋ 語音
    """
    app.logger.info("### AUDIO MESSAGE ###")
    message_id = event.message.id
    app.logger.info(f"[USER_AUDIO_ID] {message_id}")

    # 1. 下載 LINE 語音檔到 /tmp
    audio_path = f"/tmp/{message_id}.m4a"  # LINE 語音預設是 m4a
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            # 下載語音內容（串流）
            content = line_bot_api.get_message_content(message_id)
            with open(audio_path, 'wb') as fd:
                for chunk in content.iter_content():
                    fd.write(chunk)
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

    # 2. 使用 OpenAI Whisper 把語音轉成文字
    try:
        with open(audio_path, "rb") as f:
            transcript_resp = openai.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                # response_format="text"：只要純文字就好
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

    # 3. 把轉出來的文字丟進共用翻譯函式
    try:
        display_text, tts_jobs = translate_text_with_logging(transcript_text)
        audio_files = run_tts_jobs(tts_jobs, event.reply_token)

        # 顯示文字最上面先加一行「語音辨識原文」
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
# 提供靜態音檔下載的路由（給 LINE 播放用）
# =========================================================

@app.route("/static/<filename>")
def serve_audio(filename):
    """
    透過這個路由讓 LINE 能下載我們存在 /tmp 的 mp3 檔。
    """
    return send_from_directory("/tmp", filename)


# =========================================================
# 本機 / Heroku 啟動入口
# =========================================================

if __name__ == "__main__":
    import logging
    # 把 log level 設成 INFO，這樣前面 app.logger.info 的東西都會出現
    logging.basicConfig(level=logging.INFO)

    port = int(os.environ.get("PORT", 5000))
    # 0.0.0.0 讓 Heroku / Docker 都可以訪問
    app.run(host="0.0.0.0", port=port)