# LINE Bot + OpenAI (Flask) — 專案說明與部署指南

這個專案是一個使用 Flask 建立的 LINE 聊天機器人範例，整合 OpenAI API（如 GPT-4、GPT-4o、GPT-3.5-turbo 等），提供自動化回覆、語言偵測、多平台部署範例與營運建議。

---

目標讀者：想要快速部署一個整合 OpenAI 的 LINE 聊天機器人，並在雲端上穩定運行、管理流量與成本的開發者或產品負責人。

快速索引
- 專案摘要
- 專案結構
- 主要商業/營運邏輯
- 環境變數（必要金鑰）
- 本機開發與測試
- 部署（Heroku 為主）
- 可擴充性與運維建議
- 常見問題

---

專案摘要

此專案將 LINE Messaging API 作為前端訊息來源，接收使用者訊息後，把訊息送到 OpenAI API 取得回覆，然後回傳給使用者。它適合用作智能客服、問答助理或簡易對話代理（assistant）。

主要特性
- LINE 訊息接收與回覆
- 透過 OpenAI 生成回覆（可支援不同模型）
- 多語言回覆（依輸入語言判斷）
- 可部署於 Heroku（或其他雲平台，請參考下方 Heroku 範例）

專案結構（目前）

```
line-openai-bot/
├── app.py               # 主程式：Flask 應用、LINE webhook 與 OpenAI 呼叫
├── env.example          # 環境變數範例
├── requirements.txt     # Python 相依套件
├── Procfile             #（Heroku）啟動指令
├── README.md            #（本檔）專案說明
├── .gitignore           # 忽略規則（含帳密.txt 與 .idea/）
├── LICENSE
```

主要檔案說明
- `app.py`：接收 LINE webhook 請求，驗證簽章，處理事件（文字訊息、其他事件可擴充），向 OpenAI 發送 prompt，並把回覆透過 LINE 傳回使用者。也可放錯誤處理、日誌記錄與基本速率限制。
- `env.example`：環境變數範例，包含必要的金鑰欄位。
- `requirements.txt`：列出 Python 套件（Flask、line-bot-sdk、openai 等）。
- `帳密.txt`：請確認是否包含敏感資訊；若是，務必將其從 Repo 中刪除並改用環境變數或安全金鑰管理。已更新 `.gitignore` 忽略 `帳密.txt` 與 `.idea/`，但如果該檔案已推送到遠端，請使用 `git filter-repo` 或 BFG 移除歷史紀錄（如需我可以提供指令）。

環境變數（必要）

請在部署的平台或本機的 `.env`（不要提交）中設定以下變數：

- LINE_CHANNEL_SECRET=你的 LINE channel secret
- LINE_CHANNEL_ACCESS_TOKEN=你的 LINE channel access token
- OPENAI_API_KEY=你的 OpenAI API key
- (選用) WEBHOOK_URL=你的應用公開 URL（用於 LINE Developers 裡的設定）

範例：把 `.env.example` 複製為 `.env` 並填入值（建議放在 `.env`，千萬不要提交到版本控制）：

```text
# .env 範例（不要上傳到公開 repo）
LINE_CHANNEL_ACCESS_TOKEN=YOUR_LINE_CHANNEL_ACCESS_TOKEN
LINE_CHANNEL_SECRET=YOUR_LINE_CHANNEL_SECRET
OPENAI_API_KEY=YOUR_OPENAI_API_KEY
```

本機開發與測試

以下示範把專案拉到本機後的常見步驟：一句話說明後放可複製的命令（每行一個指令），可直接貼到對應的終端機執行。

1) 建立並啟動虛擬環境

Windows PowerShell（複製貼上到 PowerShell）

```text
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS / Linux（複製貼上到 bash / zsh）

```bash
python -m venv .venv
source .venv/bin/activate
```

2) 安裝相依套件（在已啟動的虛擬環境中執行）

```bash
pip install -r requirements.txt
```

3) 設定 `.env`（參考 `.env.example`），然後啟動 Flask

```bash
python app.py
```

4) 測試 webhook（選用）
- 使用 ngrok 或類似工具把本機端口公開，將產生的 https URL 填到 LINE Developers 的 webhook URL（例如 `https://xxxx.ngrok.io/callback`）。

部署指南（Heroku 為主）

本專案已針對 Heroku 可直接部署；若你已啟用自動部署（從 GitHub），下面是最小且實用的設定、檢查與測試步驟，讓你能快速確認服務可用。

短前檢查清單（請先確認）
- 在 LINE Developers：已建立 channel，並取得 `LINE_CHANNEL_ACCESS_TOKEN` 與 `LINE_CHANNEL_SECRET`，且 webhook 已啟用。
- Heroku 上已建立 app，且自動部署已開啟（或你知道如何手動推送）。
- `Procfile` 與 `requirements.txt` 已存在於 repo（本專案已包含）。
- 確認 `HEROKU_BASE_URL`（包含 https://）會指向你的 Heroku app 網址。

必要 Config Vars（至少設定這些）
- LINE_CHANNEL_ACCESS_TOKEN  
- LINE_CHANNEL_SECRET
- OPENAI_API_KEY
- HEROKU_BASE_URL（例如 `https://your-app-name.herokuapp.com`）

建議的選填 Config Vars
- TTS_RATE_PERCENT=65
- TTS_POST_PROCESS=pydub  # 若你安裝 pydub 並想要音量 Normalization
- WEB_CONCURRENCY=2       # 如果使用 gunicorn，可根據 dyno 大小調整

常用 Heroku CLI 指令（快速）
- 檢查 Config Vars：

```bash
heroku config -a your-app-name
```

- 設定 Config Vars（一次設多個）：

```bash
heroku config:set LINE_CHANNEL_ACCESS_TOKEN=xxx LINE_CHANNEL_SECRET=yyy OPENAI_API_KEY=zzz HEROKU_BASE_URL=https://your-app-name.herokuapp.com -a your-app-name
```

- 查看即時日誌（可用於 debug）：

```bash
heroku logs --tail -a your-app-name
```

Procfile / Gunicorn（建議）
- 開發時 `web: python app.py` 可行，但在 Heroku 生產環境建議使用 gunicorn：

```text
# 將 gunicorn 加到 requirements.txt，再把 Procfile 改為：
web: gunicorn app:app --log-file -
```

快速開始（Heroku）

如果你已經在 Heroku 啟用了自動部署並且程式可正常執行，這個最小三步驟可以快速驗證服務：

1) 在 Heroku 設定必要環境變數（至少包含下列）：

```text
LINE_CHANNEL_ACCESS_TOKEN=（你的值）
LINE_CHANNEL_SECRET=（你的值）
OPENAI_API_KEY=（你的值）
HEROKU_BASE_URL=https://your-app-name.herokuapp.com
```

2) 檢查 Heroku 日誌是否有收到 /callback 的請求（當有人傳訊息給 bot 時）：

```bash
heroku logs --tail -a your-app-name
```

3) 在 LINE Developers 設定 webhook 為：

```
https://your-app-name.herokuapp.com/callback
```

完成上述步驟後，傳一則訊息到 LINE bot 檢查是否有回覆。

常見問題與排查要點
- 程式啟動失敗（Crash）：請先檢查 `heroku logs`，通常是缺少必要的 Config Vars（特別是 `HEROKU_BASE_URL`）。
- 無法收到訊息：確認 LINE webhook 是否啟用、Webhook URL 是否正確、且 Heroku 日誌是否收到 /callback 的請求。
- 語音 / TTS 問題：本專案暫時把產生的音檔放在 `/tmp`，Heroku 上為 ephemeral 檔案系統，若需要長期保存請改接 S3 等外部儲存。

若你只使用 Heroku 並已啟用自動部署，以上步驟通常就是全部；其餘平台的說明保留為參考。

主要商業/營運邏輯（Business Logic）

1. 使用者訊息流程（簡化版）
   - LINE -> Webhook 接收 -> 驗證 -> 送進處理 pipeline
   - 處理 pipeline：訊息預處理（去除特殊字、判斷語言、意圖）-> 權重化/上下文合併 -> 呼叫 OpenAI -> 後處理（替換敏感詞、格式化）-> 回覆 LINE

2. 成本與頻率管理
   - OpenAI API 為按次/Token 計費，必須實施速率限制（rate limit）與最大回覆長度上限。
   - 建議設定：每使用者/每分鐘限制、全域併發限制、以及每日上限提醒或代理方案。

3. 內容安全與合規
   - 必要的輸入過濾（例如濫用/仇恨言論/個資保護）可在送到 OpenAI 前進行預檢查。
   - 如果要保存對話，務必在隱私政策中告知使用者並確保金鑰與資料加密儲存。

4. 錯誤/流量保護
   - 當 OpenAI 或外部服務回傳錯誤時，實作重試策略（exponential backoff）並在必要時降級回覆（友善的錯誤提示）。
   - 實作 circuit breaker 機制來避免大量失敗呼叫導致服務耗盡資源。

可擴充性與運維建議
- 將 prompt 與系統參數抽離成設定檔或資料庫，方便調整與 AB 測試。
- 建議使用佇列（如 Redis + RQ / Celery）處理較重的生成任務，避免 webhook 連線超時。
- 建議上線後觀察 Token/Cost 使用情形，並設定告警。
- 若需要多實例或水平擴展，務必確保 session/context 的存放使用共享儲存（例如 Redis）。

安全與隱私
- 切勿把金鑰（OpenAI、LINE）提交到版本控制系統。
- 若 Repo 中存在 `帳密.txt` 或類似檔案，請立即確認內容並從遠端移除（使用 git filter-repo / BFG 移除敏感紀錄）。
- 在雲端環境使用平台提供的秘密管理（Secrets / Config Vars）功能。

測試建議
- 撰寫單元測試覆蓋關鍵的訊息處理邏輯（例如 prompt 組成、輸入過濾器、錯誤處理）。
- 在 CI 中執行 lint 與 unit tests，部署前確保通過。

常見問題（FAQ）
- Webhook 沒觸發？
  - 確認公開網址為 https 並且 webhook endpoint 正確無誤；檢查 LINE Developers 的 webhook 設定與平台日誌。
- 出現 ModuleNotFoundError？
  - 確認已在虛擬環境中執行 `pip install -r requirements.txt`。
- 金鑰洩漏怎麼辦？
  - 立即在 LINE / OpenAI 端重設金鑰，並更新雲端環境變數。若金鑰已被推到 git，請移除並重寫歷史紀錄。

範例快速測試
- 發送一則簡單文字訊息給 LINE bot，檢查 app.py 日誌是否有收到事件與回傳。

聯絡與授權
- 授權：MIT（請參考 LICENSE）
- 若你想擴充功能、提出改進或回報問題，歡迎開 issue 或 PR。

下一步建議（可選）
- 把 prompt 管理抽成檔案/介面，支援不同應用場景（客服、問答、摘要）。
- 增加對多媒體訊息（圖片、音訊）的支援，並串接相應的處理流程。

---

感謝使用本範例專案，若需要我幫你把 `app.py` 裡的流程註解化、或幫你新增 prompt 管理與速率限制程式碼，我可以直接修改程式檔案並執行測試。
