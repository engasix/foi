import sys
import json
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
from openai import OpenAI

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import SERPER_API_KEY, OPENAI_API_KEY, AGENT_POLL_INTERVAL
from database.connection import get_connection
from logger import get_logger

log    = get_logger("signals")
client = OpenAI(api_key=OPENAI_API_KEY)

SERPER_NEWS_URL = "https://google.serper.dev/news"
SERPER_URL      = "https://google.serper.dev/search"
NEWS_DAYS       = 365   # 1 year window (was 90 in Stage 1)
MIN_SIGNALS     = 3     # skip Serper if already have this many from website

SIGNAL_TYPE_MAP = {
    "investment":      "investment",
    "fund_commitment": "fund_commitment",
    "key_hire":        "key_hire",
    "news":            "news",
    "fund_launch":     "fund_commitment",
    "leadership":      "key_hire",
    "award":           "news",
    "other":           "news",
}

FINANCIAL_SITES = "site:bloomberg.com OR site:forbes.com OR site:wsj.com OR site:axios.com OR site:pehub.com"


# ── database ──────────────────────────────────────────────────────────────────

def claim_record():
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("START TRANSACTION")
        cur.execute("""
            SELECT id, fo_name, principal_name FROM records
            WHERE web_enrichment_check = 1 AND signals_check IS NULL
            LIMIT 1 FOR UPDATE
        """)
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None
        conn.commit()
        return row
    except Exception as e:
        conn.rollback()
        log.error("Claim failed: %s", e)
        return None
    finally:
        cur.close()
        conn.close()


def get_existing_signals(record_id):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT headline, source_url FROM signals WHERE record_id = %s
        """, (record_id,))
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def save_signals(record_id, signals):
    if not signals:
        return 0
    conn = get_connection()
    cur  = conn.cursor()
    saved = 0
    try:
        for s in signals:
            cur.execute("""
                INSERT IGNORE INTO signals
                    (record_id, headline, source, signal_type,
                     description, signal_date, source_url, image_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                record_id,
                (s.get("headline") or "")[:500],
                s.get("source"),
                s.get("signal_type"),
                s.get("description"),
                s.get("signal_date"),
                s.get("source_url"),
                s.get("image_url"),
            ))
            saved += 1
        conn.commit()
        return saved
    except Exception as e:
        conn.rollback()
        log.error("Save signals failed: %s", e)
        return 0
    finally:
        cur.close()
        conn.close()


def mark_done(record_id, flag):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute(
            "UPDATE records SET signals_check = %s WHERE id = %s",
            (flag, record_id)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ── helpers ───────────────────────────────────────────────────────────────────

def fetch_og_image(url):
    if not url:
        return None
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        tag  = soup.find("meta", property="og:image")
        return tag["content"] if tag and tag.get("content") else None
    except Exception:
        return None


def parse_date(date_str):
    if not date_str:
        return None
    s   = date_str.strip().lower()
    now = datetime.now()
    try:
        if "hour" in s or "minute" in s:
            return now.date()
        if "day" in s:
            return (now - timedelta(days=int(s.split()[0]))).date()
        if "week" in s:
            return (now - timedelta(weeks=int(s.split()[0]))).date()
        if "month" in s:
            return (now - timedelta(days=int(s.split()[0]) * 30)).date()
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
    except Exception:
        pass
    return None


def is_recent(date_obj):
    if not date_obj:
        return True
    cutoff = (datetime.now() - timedelta(days=NEWS_DAYS)).date()
    return date_obj >= cutoff


def is_duplicate(headline, source_url, existing):
    headline_lower = (headline or "").lower()
    for ex_headline, ex_url in existing:
        if ex_headline and ex_headline.lower() == headline_lower:
            return True
        if source_url and ex_url and ex_url == source_url:
            return True
    return False


# ── serper searches ───────────────────────────────────────────────────────────

def search_news(query):
    try:
        resp = requests.post(
            SERPER_NEWS_URL,
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": 10},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("news", [])
    except Exception as e:
        log.warning("Serper news failed for '%s': %s", query, e)
        return []


def search_organic(query):
    try:
        resp = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": 5},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("organic", [])
    except Exception as e:
        log.warning("Serper organic failed for '%s': %s", query, e)
        return []


# ── LLM classification ────────────────────────────────────────────────────────

CLASSIFY_PROMPT = """You are an analyst reviewing news items about a family office.

For each item classify and return a JSON array. Each element:
- "headline": copy the headline exactly
- "signal_type": investment / fund_commitment / key_hire / news
- "description": one sentence - what this means for the firm specifically
- "relevant": true if genuinely about THIS firm, false if just a passing mention

Return ONLY a JSON array. Empty array if nothing relevant."""


def classify_with_llm(fo_name, items):
    if not items:
        return []
    items_text = "\n\n".join(
        f"[{i+1}] {r.get('title', r.get('headline',''))}\nSource: {r.get('source','')}\nSnippet: {r.get('snippet','')}"
        for i, r in enumerate(items)
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": CLASSIFY_PROMPT},
                {"role": "user",   "content": f"Family office: {fo_name}\n\n{items_text}"}
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=600
        )
        parsed = json.loads(resp.choices[0].message.content)
        if isinstance(parsed, dict):
            parsed = next(iter(parsed.values()), [])
        return parsed if isinstance(parsed, list) else []
    except Exception as e:
        log.warning("LLM classify failed: %s", e)
        return []


# ── process one record ────────────────────────────────────────────────────────

def process(record_id, fo_name, principal_name):
    existing = get_existing_signals(record_id)
    log.info("Processing [%d]: %s | existing signals: %d", record_id, fo_name, len(existing))

    if len(existing) >= MIN_SIGNALS:
        log.info("  Enough signals from website (%d). Skipping Serper.", len(existing))
        mark_done(record_id, 1)
        return

    # build search queries
    queries_news = [
        f'"{fo_name}"',
        f'"{fo_name}" investment deal acquisition fund',
    ]
    if principal_name:
        queries_news.append(f'"{principal_name}" "{fo_name}"')

    queries_organic = [
        f'"{fo_name}" {FINANCIAL_SITES}',
    ]

    # collect all raw results
    all_raw = []
    for q in queries_news:
        results = search_news(q)
        log.info("  News '%s': %d results", q[:50], len(results))
        all_raw.extend(results)

    for q in queries_organic:
        results = search_organic(q)
        log.info("  Organic '%s': %d results", q[:50], len(results))
        all_raw.extend(results)

    # filter recent + deduplicate raw results by URL
    seen_urls = set()
    recent    = []
    for r in all_raw:
        url      = r.get("link") or r.get("url", "")
        date_obj = parse_date(r.get("date", ""))
        if not is_recent(date_obj):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        r["_parsed_date"] = date_obj
        recent.append(r)

    log.info("  Recent unique results: %d", len(recent))

    if not recent:
        log.info("  No recent results found.")
        mark_done(record_id, 1 if existing else 0)
        return

    # LLM classify
    classified = classify_with_llm(fo_name, recent)
    log.info("  LLM classified: %d items", len(classified))

    # build signal rows - skip duplicates vs website signals
    new_signals = []
    for i, item in enumerate(classified):
        if not item.get("relevant", True):
            continue
        raw         = recent[i] if i < len(recent) else {}
        headline    = item.get("headline") or raw.get("title") or raw.get("headline", "")
        source_url  = raw.get("link") or raw.get("url")

        if is_duplicate(headline, source_url, existing):
            log.info("  Duplicate skipped: %s", headline[:60])
            continue

        signal_type = SIGNAL_TYPE_MAP.get(
            str(item.get("signal_type", "news")).lower(), "news"
        )
        image_url = fetch_og_image(source_url)

        new_signals.append({
            "headline":    headline,
            "source":      raw.get("source"),
            "signal_type": signal_type,
            "description": item.get("description", ""),
            "signal_date": raw.get("_parsed_date"),
            "source_url":  source_url,
            "image_url":   image_url,
        })
        log.info("  [%s] %s", signal_type, headline[:60])

    saved = save_signals(record_id, new_signals)
    log.info("  New signals saved: %d | Total: %d", saved, len(existing) + saved)
    mark_done(record_id, 1 if (existing or new_signals) else 0)


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("Signals agent started.")
    while True:
        row = claim_record()
        if not row:
            log.info("No pending records. Sleeping %ds...", AGENT_POLL_INTERVAL)
            time.sleep(AGENT_POLL_INTERVAL)
            continue
        try:
            process(row[0], row[1], row[2])
        except Exception as e:
            log.error("Error for record %d: %s", row[0], e)
            mark_done(row[0], 0)
        time.sleep(AGENT_POLL_INTERVAL)


if __name__ == "__main__":
    main()
