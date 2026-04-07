"""
GynOnc Auto Literature Report
==============================
由 GitHub Actions 每週定時執行。
環境變數需在 GitHub Secrets 設定：
  - GEMINI_API_KEY   : Google AI Studio 取得
  - EMAIL_ADDRESS    : Gmail 寄件帳號 (e.g. you@gmail.com)
  - EMAIL_PASSWORD   : Gmail App Password (非一般密碼)
  - DAYS_BACK        : 搜尋天數（可選，預設 7）
  - MAX_RESULTS      : 篇數上限（可選，預設 10）
"""

import os
import json
import time
import smtplib
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from Bio import Entrez

# ── 環境變數 ────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
EMAIL_ADDRESS  = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
DAYS_BACK      = int(os.environ.get("DAYS_BACK", "7"))
MAX_RESULTS    = int(os.environ.get("MAX_RESULTS", "10"))

Entrez.email = EMAIL_ADDRESS or "gynonc@report.com"

# ── 搜尋設定（依需求修改）──────────────────────────────
SEARCH_CONFIG = {
    "topics": [
        "cervical cancer",
        "ovarian cancer",
        "endometrial cancer",
        "immunotherapy gynecologic oncology",
        "robotic surgery gynecology",
        "HIFU uterine",
        "neoadjuvant chemotherapy cervical",
        "HIPEC ovarian",
    ],
    "journals": [
        "New England Journal of Medicine",
        "The Lancet Oncology",
        "Journal of Clinical Oncology",
        "Nature Medicine",
        "Gynecologic Oncology",
        "Journal of Gynecologic Oncology",
        "Annals of Oncology",
        "JAMA Oncology",
        "Cancer",
    ],
    "limit_to_journals": True,  # False = 不限定期刊
}

# ── Gemini 模型 ─────────────────────────────────────────
GEMINI_MODEL = "gemini-2.5-flash"


def build_query() -> str:
    """組合 PubMed Boolean 查詢字串"""
    keywords = SEARCH_CONFIG["topics"]
    term_q = "(" + " OR ".join(f'"{k}"[Title/Abstract]' for k in keywords) + ")"

    if SEARCH_CONFIG["limit_to_journals"]:
        journals = SEARCH_CONFIG["journals"]
        journal_q = "(" + " OR ".join(f'"{j}"[Journal]' for j in journals) + ")"
        return f"{term_q} AND {journal_q}"

    return term_q


def fetch_articles(query: str) -> list[dict]:
    """從 PubMed 抓取文章"""
    print(f"[PubMed] Searching: reldate={DAYS_BACK}, max={MAX_RESULTS}")

    handle = Entrez.esearch(
        db="pubmed", term=query,
        reldate=DAYS_BACK, retmax=MAX_RESULTS, sort="date"
    )
    record = Entrez.read(handle)
    id_list = record["IdList"]

    if not id_list:
        print("[PubMed] No articles found.")
        return []

    print(f"[PubMed] Found {len(id_list)} IDs: {', '.join(id_list)}")

    handle = Entrez.efetch(db="pubmed", id=id_list, retmode="xml")
    articles = Entrez.read(handle)

    parsed = []
    for art in articles.get("PubmedArticle", []):
        try:
            cit     = art["MedlineCitation"]
            article = cit["Article"]
            title   = str(article["ArticleTitle"])
            journal = str(article["Journal"]["Title"])

            if "Abstract" in article:
                abstract = " ".join(str(x) for x in article["Abstract"]["AbstractText"])
            else:
                abstract = "No abstract available."

            ids  = art["PubmedData"]["ArticleIdList"]
            doi  = next((str(x) for x in ids if x.attributes["IdType"] == "doi"), None)
            pmid = next((str(x) for x in ids if x.attributes["IdType"] == "pubmed"), "")
            link = f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

            pub_date = ""
            try:
                pub_date = str(article["Journal"]["JournalIssue"]["PubDate"].get("Year", ""))
            except Exception:
                pass

            parsed.append({
                "pmid": pmid, "title": title, "journal": journal,
                "abstract": abstract, "link": link, "doi": doi or "",
                "date": pub_date,
            })
        except Exception as e:
            print(f"[Parser] Skip article: {e}")

    print(f"[Parser] Parsed {len(parsed)} articles.")
    return parsed


def analyze_with_gemini(art: dict) -> str:
    """呼叫 Gemini API 生成繁體中文評讀"""
    if not GEMINI_API_KEY:
        return "（未設定 GEMINI_API_KEY，跳過 AI 分析）"

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )

    prompt = f"""
你是一位台灣頂尖的婦科腫瘤科醫師。請閱讀以下英文摘要，以繁體中文（台灣用語）撰寫深度評讀報告。

標題：{art['title']}
期刊：{art['journal']}
摘要：{art['abstract']}

請依照以下結構，以 HTML 格式輸出（使用 <h4>, <ul>, <li>, <p>, <b> 標籤）：

<h4>🧪 研究設計與方法</h4>
<h4>💡 研究動機與背景</h4>
<h4>📊 重要數據與結果</h4>
<h4>🏥 臨床實務應用</h4>
<h4>⭐ 對婦癌醫師的重要啟示</h4>

每節用 2-4 個 <li> 條列，語言專業精練。請直接輸出 HTML，不要包含 ```html 標記。
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1500},
    }

    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        # Clean markdown fences if model adds them
        return text.replace("```html", "").replace("```", "").strip()
    except requests.RequestException as e:
        return f"<p style='color:red'>Gemini API 錯誤：{e}</p>"


def build_email_html(articles: list[dict], analyses: list[str]) -> str:
    """組合成完整的 HTML 電子郵件"""
    today = datetime.now().strftime("%Y年%m月%d日")
    count = len(articles)

    # Header
    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="UTF-8">
<style>
  body {{ font-family: 'Helvetica Neue', Arial, sans-serif; background:#f0f4f8; margin:0; padding:20px; }}
  .container {{ max-width:760px; margin:0 auto; background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 4px 20px rgba(0,0,0,0.1); }}
  .header {{ background:linear-gradient(135deg,#1b2d42,#2a9d8f); color:#fff; padding:30px 36px; }}
  .header h1 {{ font-size:22px; font-weight:400; margin:0 0 6px; letter-spacing:0.04em; }}
  .header p {{ font-size:13px; opacity:0.75; margin:0; }}
  .summary-bar {{ background:#f8f4ee; border-left:4px solid #c8a96e; padding:14px 36px; font-size:13px; color:#5a4a30; }}
  .article-block {{ padding:28px 36px; border-bottom:1px solid #eef0f4; }}
  .article-num {{ font-family:monospace; font-size:11px; color:#c8a96e; font-weight:600; letter-spacing:0.1em; margin-bottom:8px; }}
  .article-title {{ font-size:16px; font-weight:600; color:#1b2d42; line-height:1.4; margin-bottom:4px; text-decoration:none; }}
  .article-journal {{ font-size:12px; color:#8a9ab5; font-family:monospace; margin-bottom:16px; }}
  .analysis-box {{ background:#f7f9fc; border-radius:8px; padding:16px 20px; font-size:13px; line-height:1.7; color:#374151; }}
  .analysis-box h4 {{ font-size:12px; color:#2a9d8f; text-transform:uppercase; letter-spacing:0.08em; margin:14px 0 6px; font-weight:600; }}
  .analysis-box h4:first-child {{ margin-top:0; }}
  .analysis-box ul {{ padding-left:18px; margin:0 0 8px; }}
  .analysis-box li {{ margin-bottom:4px; }}
  .footer {{ padding:20px 36px; text-align:center; font-size:11px; color:#aab; background:#f8fafc; }}
  .doi-badge {{ display:inline-block; background:#e8f4f0; color:#2a9d8f; font-size:10px; padding:2px 8px; border-radius:4px; font-family:monospace; margin-right:6px; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🧬 GynOnc 文獻週報</h1>
    <p>婦科腫瘤最新研究 · {today} · 自動產生</p>
  </div>
  <div class="summary-bar">
    本週彙整 <strong>{count}</strong> 篇最新文獻，涵蓋婦癌核心期刊，由 Gemini AI 進行中文深度評讀。
  </div>
"""

    for i, (art, analysis) in enumerate(zip(articles, analyses)):
        doi_badge = f'<span class="doi-badge">DOI: {art["doi"]}</span>' if art["doi"] else ""
        html += f"""
  <div class="article-block">
    <div class="article-num">文獻 {str(i+1).zfill(2)} / {str(count).zfill(2)}</div>
    <a class="article-title" href="{art['link']}">{art['title']}</a>
    <div class="article-journal">📖 {art['journal']} {art['date']}　{doi_badge}</div>
    <div class="analysis-box">{analysis}</div>
  </div>
"""

    html += f"""
  <div class="footer">
    本報告由 GitHub Actions 自動排程產生。AI 評讀由 Google Gemini 生成，僅供參考，臨床決策請以原始文獻為準。<br>
    GynOnc Literature Monitor · {today}
  </div>
</div>
</body>
</html>"""

    return html


def send_email(html_content: str) -> bool:
    """透過 Gmail SMTP 寄送 HTML 報告"""
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print("[Email] EMAIL_ADDRESS or EMAIL_PASSWORD not set. Skipping send.")
        # 仍儲存 HTML 檔案供 Artifact 下載
        return False

    today = datetime.now().strftime("%m/%d")
    msg = MIMEMultipart("alternative")
    msg["From"]    = EMAIL_ADDRESS
    msg["To"]      = EMAIL_ADDRESS
    msg["Subject"] = f"🧬 GynOnc 文獻週報 {today}"

    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.send_message(msg)
        print(f"[Email] ✅ Report sent to {EMAIL_ADDRESS}")
        return True
    except Exception as e:
        print(f"[Email] ❌ Failed: {e}")
        return False


def main():
    print("=" * 60)
    print(f"GynOnc Auto Report  |  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    # 1. Build query & fetch
    query    = build_query()
    articles = fetch_articles(query)

    if not articles:
        print("No articles found. Exiting.")
        # Save empty report file
        with open("report_output.html", "w") as f:
            f.write("<p>本週無符合條件的新文獻。</p>")
        return

    # 2. AI analysis (with rate limit)
    analyses = []
    for i, art in enumerate(articles):
        print(f"[Gemini] Analyzing {i+1}/{len(articles)}: {art['title'][:60]}...")
        analysis = analyze_with_gemini(art)
        analyses.append(analysis)
        time.sleep(1.5)  # Avoid rate limiting

    # 3. Build HTML
    html_content = build_email_html(articles, analyses)

    # 4. Save as file (always, as GitHub Actions artifact)
    with open("report_output.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("[File] report_output.html saved.")

    # 5. Send email
    send_email(html_content)

    print("=" * 60)
    print("Done.")


if __name__ == "__main__":
    main()
