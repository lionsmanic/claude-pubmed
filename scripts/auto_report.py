"""
GynOnc Auto Literature Report
==============================
由 GitHub Actions 定時執行。

三種模式（main() 自動判斷）：
  1. 購物車模式 : 有傳入 PMIDS（從網頁購物車） -> 只寄這些指定文章
  2. 本月熱門模式: REPORT_MODE=trending          -> 過去30天 gyn-cancer 文章，
                                                    依 Altmetric 關注度排序取前 N 篇
  3. 週報模式   : 預設                            -> 關鍵字搜尋最近 DAYS_BACK 天

環境變數（GitHub Secrets / workflow env）：
  - GEMINI_API_KEY   : Google AI Studio 取得
  - EMAIL_ADDRESS    : Gmail 寄件帳號 (e.g. you@gmail.com)
  - EMAIL_PASSWORD   : Gmail App Password (非一般密碼)
  - PMIDS            : 購物車指定 PMID（逗號分隔），有填則走購物車模式
  - REPORT_MODE      : weekly | trending（預設 weekly）
  - DAYS_BACK        : 週報搜尋天數（預設 7）
  - TREND_DAYS       : 熱門模式回溯天數（預設 30）
  - MAX_RESULTS      : 最終寄出篇數上限（預設 10）
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
PMIDS          = os.environ.get("PMIDS", "").strip()           # 購物車指定 PMID
REPORT_MODE    = os.environ.get("REPORT_MODE", "weekly").strip().lower()
DAYS_BACK      = int(os.environ.get("DAYS_BACK", "7"))
TREND_DAYS     = int(os.environ.get("TREND_DAYS", "30"))
MAX_RESULTS    = int(os.environ.get("MAX_RESULTS", "10"))

Entrez.email = EMAIL_ADDRESS or "gynonc@report.com"

# ── 搜尋設定 ───────────────────────────────────────────
SEARCH_CONFIG = {
    # 週報用的廣泛主題（含你自己的次專科）
    "topics": [
        "cervical cancer",
        "ovarian cancer",
        "endometrial cancer",
        "immunotherapy gynecologic oncology",
        "robotic surgery gynecology",
        "single-port gynecologic surgery",
        "HIFU uterine",
        "neoadjuvant chemotherapy cervical",
        "HIPEC ovarian",
    ],
    # 擴充後的期刊清單（核心婦科 + OBGYN + 高影響腫瘤 + 你的次專科期刊）
    "journals": [
        # ── 核心婦科腫瘤 / 婦產 ──
        "Gynecologic Oncology",
        "International Journal of Gynecological Cancer",
        "Journal of Gynecologic Oncology",
        "Gynecologic Oncology Reports",
        "American Journal of Obstetrics and Gynecology",
        "Obstetrics and Gynecology",
        "BJOG",
        "Ultrasound in Obstetrics & Gynecology",
        # ── 你的次專科（微創/機器人、HIFU/熱治療）──
        "Journal of Minimally Invasive Gynecology",
        "International Journal of Hyperthermia",
        # ── 高影響泛腫瘤 / 綜合 ──
        "New England Journal of Medicine",
        "The Lancet",
        "The Lancet Oncology",
        "Journal of Clinical Oncology",
        "JAMA",
        "JAMA Oncology",
        "Nature Medicine",
        "Nature Cancer",
        "Nature Reviews. Clinical Oncology",
        "Annals of Oncology",
        "ESMO Open",
        "Cancer",
        "Clinical Cancer Research",
        "Cancer Research",
        "European Journal of Cancer",
        "Journal of the National Cancer Institute",
        "International Journal of Cancer",
        "Journal of the National Comprehensive Cancer Network",
        "Modern Pathology",
        "npj Precision Oncology",
    ],
    "limit_to_journals": True,  # False = 不限定期刊
}

# 熱門模式專用：聚焦「婦癌相關」，避免拉進非癌症文章
TRENDING_TOPICS = [
    "cervical cancer",
    "ovarian cancer",
    "endometrial cancer",
    "uterine cancer",
    "vulvar cancer",
    "vaginal cancer",
    "gynecologic cancer",
    "gynecologic oncology",
]

# ── Gemini 模型 ─────────────────────────────────────────
GEMINI_MODEL = "gemini-2.5-flash"


def build_query(topics=None) -> str:
    """組合 PubMed Boolean 查詢字串"""
    keywords = topics if topics is not None else SEARCH_CONFIG["topics"]
    term_q = "(" + " OR ".join(f'"{k}"[Title/Abstract]' for k in keywords) + ")"

    if SEARCH_CONFIG["limit_to_journals"]:
        journals = SEARCH_CONFIG["journals"]
        journal_q = "(" + " OR ".join(f'"{j}"[Journal]' for j in journals) + ")"
        return f"{term_q} AND {journal_q}"

    return term_q


def fetch_by_pmids(pmid_list: list) -> list:
    """直接用 PMID 清單抓取文章（購物車模式）"""
    print(f"[PubMed] Fetching {len(pmid_list)} specific articles by PMID...")
    handle = Entrez.efetch(db="pubmed", id=pmid_list, retmode="xml")
    articles = Entrez.read(handle)
    return _parse_pubmed(articles)


def fetch_articles(query: str, reldate: int = None, retmax: int = None) -> list:
    """從 PubMed 抓取文章（可指定回溯天數與篇數，預設用全域變數）"""
    reldate = DAYS_BACK if reldate is None else reldate
    retmax = MAX_RESULTS if retmax is None else retmax
    print(f"[PubMed] Searching: reldate={reldate}, retmax={retmax}")

    handle = Entrez.esearch(
        db="pubmed", term=query,
        reldate=reldate, retmax=retmax, sort="date"
    )
    record = Entrez.read(handle)
    id_list = record["IdList"]

    if not id_list:
        print("[PubMed] No articles found.")
        return []

    print(f"[PubMed] Found {len(id_list)} IDs.")
    handle = Entrez.efetch(db="pubmed", id=id_list, retmode="xml")
    articles = Entrez.read(handle)
    return _parse_pubmed(articles)


def _parse_pubmed(articles) -> list:
    """共用的 PubMed XML 解析"""
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


# ── 熱門度指標 ─────────────────────────────────────────
def get_altmetric(doi: str, pmid: str) -> dict:
    """
    回傳 Altmetric 資料：{score, mendeley}。
    免金鑰、限速 1 req/sec。無關注度的文章會回 404 -> score 0。
    """
    urls = []
    if doi:
        urls.append(f"https://api.altmetric.com/v1/doi/{doi}")
    if pmid:
        urls.append(f"https://api.altmetric.com/v1/pmid/{pmid}")

    for url in urls:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 429:           # 觸發限速 -> 等一下重試
                print("[Altmetric] 429 rate-limited, sleeping 5s...")
                time.sleep(5)
                r = requests.get(url, timeout=20)
            if r.status_code == 200:
                d = r.json()
                readers = (d.get("readers") or {})
                return {
                    "score": float(d.get("score", 0) or 0),
                    "mendeley": int(readers.get("mendeley", 0) or 0),
                }
            # 404 = 此文章目前沒有任何關注度紀錄，換下一個 identifier
        except requests.RequestException as e:
            print(f"[Altmetric] error: {e}")
    return {"score": 0.0, "mendeley": 0}


def get_icite_metrics(pmids: list) -> dict:
    """批次查 NIH iCite 引用數（一次 request）。回傳 {pmid: {citations, rcr}}"""
    out = {}
    if not pmids:
        return out
    try:
        r = requests.get(
            "https://icite.od.nih.gov/api/pubs",
            params={"pmids": ",".join(pmids), "format": "json"},
            timeout=40,
        )
        if r.status_code == 200:
            for rec in r.json().get("data", []):
                out[str(rec.get("pmid"))] = {
                    "citations": int(rec.get("citation_count") or 0),
                    "rcr": rec.get("relative_citation_ratio"),
                }
    except requests.RequestException as e:
        print(f"[iCite] error: {e}")
    return out


def rank_by_popularity(candidates: list) -> list:
    """為候選文章加上熱門度指標並排序（高 -> 低）"""
    print(f"[Rank] Scoring {len(candidates)} candidates by attention...")

    # iCite 一次批次查
    icite = get_icite_metrics([c["pmid"] for c in candidates if c["pmid"]])

    for i, c in enumerate(candidates):
        am = get_altmetric(c["doi"], c["pmid"])
        ic = icite.get(str(c["pmid"]), {})
        c["altmetric"] = am["score"]
        c["mendeley"]  = am["mendeley"]
        c["citations"] = ic.get("citations", 0)
        c["rcr"]       = ic.get("rcr")
        # 綜合熱門度：Altmetric 主導，Mendeley/引用加權
        c["popularity"] = c["altmetric"] + 0.2 * c["mendeley"] + c["citations"]
        print(f"  [{i+1}/{len(candidates)}] Altmetric={c['altmetric']:.0f} "
              f"Mendeley={c['mendeley']} Cite={c['citations']} :: {c['title'][:50]}")
        time.sleep(1)   # Altmetric 免金鑰限速：1 req/sec

    # 穩定排序：popularity 高者在前；全 0 時保留原本（日期）順序
    candidates.sort(key=lambda x: x["popularity"], reverse=True)
    return candidates


def analyze_with_gemini(art: dict) -> str:
    """呼叫 Gemini API 生成繁體中文評讀"""
    if not GEMINI_API_KEY:
        return "（未設定 GEMINI_API_KEY，跳過 AI 分析）"

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )

    prompt = f"""你是一位在台灣執業多年的婦科腫瘤科醫師，擅長閱讀英文醫學文獻並消化轉譯給同儕。

請完整閱讀以下論文摘要，用繁體中文（台灣用語）寫出一份「臨床評讀摘要」。
目標讀者是忙碌的婦癌醫師，希望在 2 分鐘內掌握這篇文章的精華與臨床意義。

標題：{art['title']}
期刊：{art['journal']}
摘要原文：{art['abstract']}

請依以下四個區塊輸出 HTML，直接使用 <h4>, <p>, <ul>, <li>, <strong> 標籤，不要輸出 ```html：

<h4>🔬 這篇在研究什麼？</h4>
<p>（2–3 句流暢說明：研究背景、設計類型、樣本規模、主要研究問題）</p>

<h4>📊 關鍵數據與結果</h4>
<ul><li>（3–5 條最重要數據，含具體數字、p值、HR、OS、PFS，完整呈現）</li></ul>

<h4>🏥 對臨床實務的影響</h4>
<p>（2–3 句：哪些病人受益？是否改變治療決策？與 NCCN/ESMO 指引的關係？）</p>

<h4>⭐ 一句話總結</h4>
<p><strong>（最多 50 字，總結最值得婦癌醫師記住的事）</strong></p>"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingBudget": 0}
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return text.replace("```html", "").replace("```", "").strip()
    except requests.RequestException as e:
        return f"<p style='color:red'>Gemini API 錯誤：{e}</p>"


def build_email_html(articles: list, analyses: list,
                     mode_label: str = "本週最新", is_trending: bool = False) -> str:
    """組合成完整的 HTML 電子郵件"""
    today = datetime.now().strftime("%Y年%m月%d日")
    count = len(articles)

    header_icon  = "🔥" if is_trending else "🧬"
    header_sub   = (f"過去 {TREND_DAYS} 天最受關注的婦癌文獻 · {today} · 依 Altmetric 排序"
                    if is_trending else
                    f"婦科腫瘤最新研究 · {today} · 自動產生")
    summary_text = (f"本月精選 <strong>{count}</strong> 篇<strong>關注度最高</strong>的婦癌文獻，"
                    f"依 Altmetric Attention Score 排序，由 Gemini AI 進行中文深度評讀。"
                    if is_trending else
                    f"本週彙整 <strong>{count}</strong> 篇最新文獻，涵蓋婦癌核心期刊，由 Gemini AI 進行中文深度評讀。")

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
  .article-journal {{ font-size:12px; color:#8a9ab5; font-family:monospace; margin-bottom:10px; }}
  .metrics-row {{ margin-bottom:14px; }}
  .metric-badge {{ display:inline-block; background:#fff1e8; color:#c0603a; font-size:10px; padding:2px 8px; border-radius:4px; font-family:monospace; margin-right:5px; font-weight:600; }}
  .rank-medal {{ font-size:15px; margin-right:6px; }}
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
    <h1>{header_icon} GynOnc {mode_label}文獻報告</h1>
    <p>{header_sub}</p>
  </div>
  <div class="summary-bar">
    {summary_text}
  </div>
"""

    for i, (art, analysis) in enumerate(zip(articles, analyses)):
        doi_badge = f'<span class="doi-badge">DOI: {art["doi"]}</span>' if art["doi"] else ""

        metrics_html = ""
        if is_trending:
            medal = {0: "🥇", 1: "🥈", 2: "🥉"}.get(i, "")
            badges = f'<span class="metric-badge">🔥 Altmetric {art.get("altmetric", 0):.0f}</span>'
            if art.get("mendeley"):
                badges += f'<span class="metric-badge">📚 Mendeley {art["mendeley"]}</span>'
            if art.get("citations"):
                badges += f'<span class="metric-badge">📈 引用 {art["citations"]}</span>'
            if art.get("rcr"):
                badges += f'<span class="metric-badge">RCR {art["rcr"]}</span>'
            metrics_html = f'<div class="metrics-row"><span class="rank-medal">{medal}</span>{badges}</div>'

        html += f"""
  <div class="article-block">
    <div class="article-num">文獻 {str(i+1).zfill(2)} / {str(count).zfill(2)}</div>
    <a class="article-title" href="{art['link']}">{art['title']}</a>
    <div class="article-journal">📖 {art['journal']} {art['date']}　{doi_badge}</div>
    {metrics_html}
    <div class="analysis-box">{analysis}</div>
  </div>
"""

    html += f"""
  <div class="footer">
    本報告由 GitHub Actions 自動排程產生。熱門度為 Altmetric / Mendeley / iCite 公開指標（非 PubMed 官方點擊量）。<br>
    AI 評讀由 Google Gemini 生成，僅供參考，臨床決策請以原始文獻為準。<br>
    GynOnc Literature Monitor · {today}
  </div>
</div>
</body>
</html>"""

    return html


def send_email(html_content: str, subject_label: str) -> bool:
    """透過 Gmail SMTP 寄送 HTML 報告"""
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print("[Email] EMAIL_ADDRESS or EMAIL_PASSWORD not set. Skipping send.")
        return False

    today = datetime.now().strftime("%m/%d")
    icon = "🔥" if "熱門" in subject_label else "🧬"
    msg = MIMEMultipart("alternative")
    msg["From"]    = EMAIL_ADDRESS
    msg["To"]      = EMAIL_ADDRESS
    msg["Subject"] = f"{icon} GynOnc {subject_label}文獻報告 {today}"
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.send_message(msg)
        print(f"[Email] OK Report sent to {EMAIL_ADDRESS}")
        return True
    except Exception as e:
        print(f"[Email] FAILED: {e}")
        return False


def main():
    print("=" * 60)
    print(f"GynOnc Auto Report  |  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    is_trending = False

    # ── 模式判斷 ──────────────────────────────────────────
    if PMIDS:
        # 1. 購物車模式：直接抓指定 PMID
        pmid_list = [p.strip() for p in PMIDS.split(",") if p.strip()]
        print(f"[Mode] 購物車模式：指定 {len(pmid_list)} 篇文章")
        articles = fetch_by_pmids(pmid_list)
        mode_label = "購物車精選"

    elif REPORT_MODE == "trending":
        # 2. 本月熱門模式：多抓候選 -> 依關注度排序 -> 取前 N 篇
        print(f"[Mode] 本月熱門模式：過去 {TREND_DAYS} 天，依 Altmetric 排序取前 {MAX_RESULTS} 篇")
        query = build_query(topics=TRENDING_TOPICS)
        pool_size = min(max(MAX_RESULTS * 6, 60), 150)   # 抓較多候選再排序
        candidates = fetch_articles(query, reldate=TREND_DAYS, retmax=pool_size)
        if candidates:
            candidates = rank_by_popularity(candidates)
            articles = candidates[:MAX_RESULTS]           # 只有前 N 篇才送 Gemini，省 token
        else:
            articles = []
        mode_label = "本月熱門"
        is_trending = True

    else:
        # 3. 週報模式：關鍵字搜尋最新
        print(f"[Mode] 週報模式：關鍵字搜尋最近 {DAYS_BACK} 天")
        query = build_query()
        articles = fetch_articles(query, reldate=DAYS_BACK, retmax=MAX_RESULTS)
        mode_label = "本週最新"

    if not articles:
        print("No articles found. Exiting.")
        with open("report_output.html", "w", encoding="utf-8") as f:
            f.write("<p>無符合條件的文獻。</p>")
        return

    # ── AI 分析（只分析最終要寄出的文章）─────────────────────
    analyses = []
    for i, art in enumerate(articles):
        print(f"[Gemini] Analyzing {i+1}/{len(articles)}: {art['title'][:60]}...")
        analyses.append(analyze_with_gemini(art))
        time.sleep(1.5)

    # ── 組合 HTML & 寄出 ──────────────────────────────────
    html_content = build_email_html(articles, analyses,
                                    mode_label=mode_label, is_trending=is_trending)
    with open("report_output.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("[File] report_output.html saved.")

    send_email(html_content, mode_label)

    print("=" * 60)
    print("Done.")


if __name__ == "__main__":
    main()
