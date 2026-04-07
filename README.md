# GynOnc Literature Monitor

婦科腫瘤 PubMed 自動文獻監控系統。

## 🗂 檔案結構

```
/
├── index.html                          ← GitHub Pages 前端（手動搜尋）
├── scripts/
│   └── auto_report.py                  ← 自動排程腳本
└── .github/
    └── workflows/
        └── weekly_report.yml           ← GitHub Actions 排程設定
```

## 🚀 部署步驟

### 步驟 1：建立 GitHub Repository

1. 在 GitHub 建立新 Repo（Public 或 Private 皆可）
2. 把三個檔案上傳到對應位置

### 步驟 2：開啟 GitHub Pages

`Settings` → `Pages` → Source 選 `Deploy from a branch` → Branch: `main`, folder: `/ (root)`

→ 網址會是 `https://你的帳號.github.io/你的repo名/`

### 步驟 3：設定 Secrets（自動排程寄信用）

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

| Secret 名稱 | 說明 |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio 取得（免費） |
| `EMAIL_ADDRESS` | Gmail 地址（如 lionsmanic@gmail.com） |
| `EMAIL_PASSWORD` | **Gmail App Password**（非登入密碼）|

#### 如何取得 Gmail App Password：
1. Google 帳號 → 安全性 → 兩步驟驗證（需先開啟）
2. 搜尋「應用程式密碼」→ 選「郵件」→ 產生 16 位密碼
3. 貼到 `EMAIL_PASSWORD` Secret

### 步驟 4：手動觸發測試

`Actions` → `Weekly GynOnc Literature Report` → `Run workflow`

可以設定天數和篇數，點 Run 即可測試。

## ⚙️ 修改搜尋主題

編輯 `scripts/auto_report.py` 頂部的 `SEARCH_CONFIG`：

```python
SEARCH_CONFIG = {
    "topics": [
        "cervical cancer",
        "ovarian cancer",
        # 加入你的關鍵字...
    ],
    "journals": [
        "Gynecologic Oncology",
        # 加入你的期刊...
    ],
    "limit_to_journals": True,  # False = 不限期刊
}
```

## 🌐 前端 (index.html) 使用說明

1. 用瀏覽器開啟 GitHub Pages 網址
2. 左側輸入 Gemini API Key（設定會存在瀏覽器 localStorage）
3. 選擇主題、天數、篇數
4. 點「搜尋最新文獻」
5. 個別點「AI 評讀」或用「全部 AI 分析」
6. 分析完成的文章自動加入報告購物車
7. 設定 EmailJS 後可直接寄出報告

## 📅 排程時間

預設每週一 08:00 台灣時間（GitHub Actions cron: `0 0 * * 1` UTC）。

修改 `.github/workflows/weekly_report.yml` 中的 cron 表達式：
- 每週三：`0 0 * * 3`
- 每天早上：`0 0 * * *`
- 每週一三五：`0 0 * * 1,3,5`

## 📬 Email 報告

報告以 HTML 格式寄送，包含：
- 每篇文章完整資訊（標題、期刊、DOI）
- Gemini AI 中文深度評讀（5 個面向）
- 原文連結

即使 Email 發送失敗，HTML 報告仍會儲存在 GitHub Actions Artifacts（保留 30 天）。
