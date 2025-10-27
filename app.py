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

# 可選：用於 SSML escape
from xml.sax.saxutils import escape as xml_escape

# 可選後處理：pydub（若要用，請在 Heroku 加入 ffmpeg buildpack 並將 TTS_POST_PROCESS=pydub）
try:
    from pydub import AudioSegment
    PydubAvailable = True
except Exception:
    PydubAvailable = False

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
openai.api_key = os.environ.get('OPENAI_API_KEY')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

HEROKU_BASE_URL = os.environ.get("HEROKU_BASE_URL")
if not HEROKU_BASE_URL:
    raise RuntimeError("請在 Heroku Config Vars 設定 HEROKU_BASE_URL，範例：https://你的heroku-app.herokuapp.com")

# 可透過環境變數設定語速（百分比，預設 85 -> 較慢）
TTS_RATE_PERCENT = int(os.environ.get("TTS_RATE_PERCENT", "85"))
# 關閉 SSML：為避免 TTS 把 SSML 標籤念出，暫時強制關閉 SSML（使用純文字 TTS）
TTS_USE_SSML = False
# 若 SSML 不生效，且想用 pydub 做後處理，設定 TTS_POST_PROCESS=pydub 並確保 pydub 與 ffmpeg 可用
TTS_POST_PROCESS = os.environ.get("TTS_POST_PROCESS", "").lower()  # "pydub" to enable

# ---------- 改進的語言偵測與 sanitize 函式 ----------
def detect_lang_simple(text: str):
    """簡單偵測使用者輸入是否為中文 (zh) 或越南文 (vi)。
    回傳 'zh'、'vi' 或 None。
    """
    if not text:
        return None
    
    text_clean = re.sub(r'[^\w\s\u4e00-\u9fffđĐăĂâÂêÊôÔơƠưƯ]', '', text)
    
    # 如果有 CJK 字元 -> 當作中文
    if re.search(r'[\u4e00-\u9fff]', text_clean):
        return 'zh'
    
    # 越南語常見字符和詞彙
    vi_patterns = [
        r'[đĐăĂâÂêÊôÔơƠưƯ]',
        r'\b(và|không|của|xin|chào|cám|cảm|ơn|tôi|bạn|anh|chị|em)\b',
        r'\b(có|không|phải|là|gì|nào|đâu|sao|bao|giờ)\b'
    ]
    
    vi_count = 0
    for pattern in vi_patterns:
        if re.search(pattern, text_clean, re.I):
            vi_count += 1
    
    if vi_count >= 2:  # 至少匹配兩個越南語特徵
        return 'vi'
    
    return None

def sanitize_translation(reply_text: str, target_lang: str):
    """針對中↔越自動翻譯情境，徹底清理 GPT 回覆，移除所有非翻譯內容。"""
    if not reply_text:
        return reply_text
    
    s = reply_text.strip()
    
    # 移除常見的語言標示前綴
    patterns_to_remove = [
        r'^\s*(?:\[*\s*(?:[Vv]ietnamese|Vietnamese|越南語|Chinese|中文|翻譯|Translation)\s*\]*[:：\-\s]*)',
        r'^\s*(?:Tiếng Việt|Tiếng Trung|中文|越南文)[:：\s]*',
        r'^["「『](.+)["」』]$',  # 移除引號包圍的內容但保留內容
    ]
    
    for pattern in patterns_to_remove:
        s = re.sub(pattern, '', s)
    
    # 移除引號但保留內容
    if (s.startswith('"') and s.endswith('"')) or (s.startswith('「') and s.endswith('」')) or (s.startswith('『') and s.endswith('』')):
        s = s[1:-1].strip()
    
    # 針對目標語言進一步清理
    if target_lang == 'zh':
        # 找到第一個中文字元開始
        m = re.search(r'[\u4e00-\u9fff]', s)
        if m:
            s = s[m.start():].strip()
        # 移除尾部的英文說明
        s = re.sub(r'[\(（].*?[\)）]', '', s)
        s = re.sub(r'[\s，。]*$', '', s)
    
    elif target_lang == 'vi':
        # 移除尾部的中文或英文說明
        s = re.sub(r'[\(（].*?[\)）]', '', s)
        s = re.sub(r'[\s,\.]*$', '', s)
    
    # 最終清理：移除多餘空格和特殊字符
    s = re.sub(r'\s+', ' ', s).strip()
    
    # 如果經過清理後變成空字串，fallback 回原始但再次清理
    if not s:
        s = re.sub(r'^\s*(?:翻譯|Translation|越南語|中文)[:：\s]*', '', reply_text.strip())
        s = re.sub(r'\s+', ' ', s).strip()
    
    return s

def clean_tts_text(text: str):
    """專門清理要送給 TTS 的文字，移除可能導致奇怪發音的內容"""
    if not text:
        return text
    
    # 移除或替換可能導致 TTS 讀出標點符號的字符
    cleaned = re.sub(r'[\[\]{}()<>]', ' ', text)  # 移除括號
    cleaned = re.sub(r'[:：]', '，', cleaned)      # 替換冒號為逗號
    cleaned = re.sub(r'[!！]', '。', cleaned)      # 替換驚嘆號為句號
    
    # 移除多餘空格
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    
    return cleaned

# ---------- end helper functions ----------

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
        # 1. GPT 回覆 - 修正模型名稱
        response = openai.chat.completions.create(
            model="gpt-4",  # 修正：使用正確的模型名稱
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
         
        5) 性相關內容處理
        - 性相關的言論也要照實翻譯，比如挑情或是性挑逗的言論，但保持翻譯的準確性。

        重要：回覆時絕對不要添加任何前綴、後綴、引號或說明文字，只輸出純粹的翻譯內容。
                """},
                {"role": "user", "content": user_message}
            ],
            temperature=0.3  # 降低隨機性，讓輸出更穩定
        )
        ai_reply = response.choices[0].message.content.strip()
        app.logger.info(f"GPT raw reply: {ai_reply!r}")

        # ---------- 使用改進的偵測和清理 ----------
        detected = detect_lang_simple(user_message)
        target = None
        if detected == 'zh':
            target = 'vi'
        elif detected == 'vi':
            target = 'zh'

        if target:
            sanitized = sanitize_translation(ai_reply, target)
            app.logger.info(f"Sanitized reply: {sanitized!r}")
        else:
            sanitized = ai_reply.strip()

        # 若 sanitize 失敗回傳空，fallback 回原 ai_reply
        if not sanitized:
            sanitized = ai_reply.strip()

        # 進一步清理 TTS 文字
        tts_text = clean_tts_text(sanitized)
        app.logger.info(f"Final TTS text: {tts_text!r}")

        # 2. TTS 合成語音（OpenAI TTS API）
        audio_filename = f"{event.reply_token}.mp3"
        audio_path = f"/tmp/{audio_filename}"

        def call_tts_with_text(input_text):
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
                        "voice": "nova",
                        "input": input_text,
                        "speed": TTS_RATE_PERCENT / 100.0  # 直接使用 speed 參數
                    },
                    timeout=30
                )
                if resp.status_code != 200:
                    app.logger.error(f"TTS API error: {resp.status_code} - {resp.text}")
                return resp
            except Exception as e:
                app.logger.warning(f"TTS request exception: {e}")
                raise

        # 使用清理後的文字進行 TTS
        tts_response = call_tts_with_text(tts_text)

        if tts_response.status_code != 200:
            app.logger.error(f"TTS failed: {tts_response.text}")
            # TTS 失敗時只回文字
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=sanitized)]
                    )
                )
            return

        # 儲存臨時語音檔
        with open(audio_path, "wb") as f:
            f.write(tts_response.content)

        final_audio_path = audio_path

        # 如果 pydub 可用且啟用，進行後處理（但現在我們直接使用 TTS 的 speed 參數）
        if TTS_POST_PROCESS == "pydub" and PydubAvailable:
            try:
                sound = AudioSegment.from_file(final_audio_path, format="mp3")
                # 可選的額外處理，如標準化音量
                sound = sound.normalize()
                processed_path = f"/tmp/processed_{audio_filename}"
                sound.export(processed_path, format="mp3")
                final_audio_path = processed_path
            except Exception as e:
                app.logger.warning(f"pydub processing failed: {e}")

        # 3. 用 mutagen 取得 mp3 長度（秒），轉成毫秒
        try:
            audio_info = MP3(final_audio_path)
            duration = int(audio_info.info.length * 1000)
        except Exception as e:
            app.logger.warning(f"Failed to get audio duration: {e}")
            duration = 3000  # 預設 3 秒

        # 4. 公開語音檔案的網址
        if final_audio_path != audio_path:
            public_filename = os.path.basename(final_audio_path)
        else:
            public_filename = audio_filename

        audio_url = f"{HEROKU_BASE_URL}/static/{public_filename}"

        # 5. 回覆 LINE 使用者（文字+語音）
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text=sanitized),  # 顯示清理後的回覆
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