# LINE Bot + OpenAI (Flask) 部署專案

這是一個用 Flask 製作的 LINE 聊天機器人，整合 OpenAI GPT-4.1（可換成 GPT-4o、GPT-3.5-turbo 等）模型，能回覆各類訊息。支援 Railway、Render、Heroku 等雲端平台部署，也可本機開發測試。

---

## 功能簡介

- LINE Bot 文字訊息自動回覆，串接 OpenAI GPT API
- 支援多語言（自動偵測語言回覆）
- 部署簡單，支援多種雲端平台
- 環境變數管理金鑰，安全性高

---

## 專案結構

```text
your-bot-project/
├── app.py               # 主程式
├── requirements.txt     # Python 套件清單
├── Procfile             # 部署平台啟動指令
├── .env.example         # 環境變數範例
├── .env                 # 本地開發用環境變數（請勿上傳）
├── .gitignore           # 忽略 .env/__pycache__ 等
├── README.md            # 本說明文件
```

---

## 快速開始

### 1. 複製專案

```bash
git clone https://github.com/your-username/your-bot-project.git
cd your-bot-project
```

### 2. 安裝套件

```bash
pip install -r requirements.txt
```

### 3. 設定環境變數

建立 `.env` 檔案，內容如下（請填入你的金鑰）：

```env
LINE_CHANNEL_ACCESS_TOKEN=你的token
LINE_CHANNEL_SECRET=你的secret
OPENAI_API_KEY=你的openai_key
```

或直接參考 `.env.example`。

### 4. 本機啟動

```bash
python app.py
```

預設會在 http://localhost:5000 提供 webhook 服務。

---

## 部署到 Railway

1. 推送專案到 GitHub
2. 前往 [Railway](https://railway.app/)，選擇「New Project」→「Deploy from GitHub repo」
3. 授權並選擇此專案
4. 進入專案後，點選左側 `Variables` 設定環境變數  
   - `LINE_CHANNEL_ACCESS_TOKEN`
   - `LINE_CHANNEL_SECRET`
   - `OPENAI_API_KEY`
5. Railway 會自動給你一組公開網址，將其填入 LINE Developers Webhook URL（如 `https://xxx.up.railway.app/callback`）

---

## 部署到 Render

1. 推送專案到 GitHub
2. 前往 [Render](https://render.com/)，新增 Web Service，選擇你的 repo
3. 設定 Build Command: `pip install -r requirements.txt`
4. 設定 Start Command: `python app.py`
5. 設定環境變數（Variables）
6. 取得公開網址填入 LINE Webhook

---

## Heroku (已不再提供免費方案，流程類似)

---

## 重要注意事項

- `.env` 請勿上傳到 git（已在 `.gitignore` 設定）
- 金鑰請務必妥善保管
- 部署平台免費方案皆有資源限制，大流量需改用付費方案

---

## 參考套件

- [Flask](https://flask.palletsprojects.com/)
- [python-dotenv](https://pypi.org/project/python-dotenv/)
- [openai](https://pypi.org/project/openai/)
- [line-bot-sdk](https://github.com/line/line-bot-sdk-python)

---

## 常見問題

- **Webhook 無法觸發？**  
  請確認平台給的網址是 https 及 `/callback`，並填入 LINE Developers 後台。
- **金鑰洩漏怎麼辦？**  
  立即重設金鑰，並更新雲端環境變數。
- **出現 ModuleNotFoundError？**  
  請檢查 `requirements.txt` 是否已安裝所有必要套件。

---

## 聯絡/授權

歡迎自由 fork、研究、修改。如有問題可開 issue 或聯繫作者。
