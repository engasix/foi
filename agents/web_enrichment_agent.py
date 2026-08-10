import sys
import time
import json
from pathlib import Path
from openai import OpenAI

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import OPENAI_API_KEY, AGENT_POLL_INTERVAL
from database.connection import get_connection
from logger import get_logger

log    = get_logger("web_enrichment")
client = OpenAI(api_key=OPENAI_API_KEY)


# ── database ──────────────────────────────────────────────────────────────────

def claim_record():
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("START TRANSACTION")
        cur.execute("""
            SELECT id, fo_name FROM records
            WHERE classifier_check = 1 AND web_enrichment_check IS NULL
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


def get_scraped_content(record_id):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT page_url, page_content FROM scrape_cache
            WHERE record_id = %s
        """, (record_id,))
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def save_fields(record_id, fields):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records SET
                fo_background         = COALESCE(fo_background, %s),
                fo_aum                = COALESCE(fo_aum, %s),
                fo_investing_thesis   = COALESCE(fo_investing_thesis, %s),
                fo_investing_mandates = COALESCE(fo_investing_mandates, %s),
                principal_name        = COALESCE(principal_name, %s),
                principal_title       = COALESCE(principal_title, %s),
                web_enrichment_check  = 1
            WHERE id = %s
        """, (
            fields.get("fo_background")         or None,
            fields.get("fo_aum")                or None,
            fields.get("fo_investing_thesis")   or None,
            fields.get("fo_investing_mandates") or None,
            fields.get("principal_name")        or None,
            fields.get("principal_title")       or None,
            record_id
        ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error("Save fields failed for record %s: %s", record_id, e)
    finally:
        cur.close()
        conn.close()


def save_signals(record_id, signals, source_url):
    if not signals:
        return 0
    conn = get_connection()
    cur  = conn.cursor()
    saved = 0
    try:
        for s in signals:
            headline     = (s.get("headline") or "").strip()
            signal_type  = (s.get("signal_type") or "").lower()
            description  = (s.get("description") or "").strip()
            signal_date  = s.get("signal_date") or None

            if not headline or not description:
                continue
            if signal_type not in ("investment", "fund_commitment", "key_hire", "news"):
                signal_type = "news"

            cur.execute("""
                INSERT IGNORE INTO signals
                    (record_id, headline, source, signal_type, description, signal_date, source_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (record_id, headline[:500], "website", signal_type,
                  description, signal_date, source_url))
            saved += 1

        conn.commit()
        return saved
    except Exception as e:
        conn.rollback()
        log.error("Save signals failed for record %s: %s", record_id, e)
        return 0
    finally:
        cur.close()
        conn.close()


def mark_no_content(record_id, reason):
    """
    No scraped content to work with (site was inaccessible upstream). Don't drop the
    record - pass it through so search_enrichment can still try to fill fields via search.
    """
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records SET web_enrichment_check = 1, error_message = %s WHERE id = %s
        """, (reason[:500], record_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def mark_retry(record_id, reason):
    """Transient failure (e.g. LLM API error) - leave web_enrichment_check NULL so it's retried."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records SET error_message = %s WHERE id = %s
        """, (reason[:500], record_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ── LLM extraction ────────────────────────────────────────────────────────────

EXTRACT_PROMPT = """You are extracting structured data from a family office website.

Extract the following from the content below. Return null for anything not found.

Respond in valid JSON only. No extra text.

{{
  "fo_background": "2-3 sentence summary of what this firm does",
  "fo_aum": "assets under management if mentioned (e.g. $500M, $2B)",
  "fo_investing_thesis": "their investment philosophy or approach",
  "fo_investing_mandates": "sectors, geographies, or asset classes they focus on",
  "principal_name": "name of the key decision maker or founder",
  "principal_title": "their title (CEO, CIO, Managing Director, Founder, etc.)",
  "signals": [
    {{
      "headline": "short headline of the news or event",
      "signal_type": "investment OR fund_commitment OR key_hire OR news",
      "description": "one sentence summary of what happened and why it matters",
      "signal_date": "YYYY-MM-DD if date is mentioned, else null"
    }}
  ]
}}

For signals: extract any news, investments made, fund commitments, key hires, or notable events
mentioned on the website. Only include concrete events, not general descriptions.
Return an empty array if nothing found.

Website content:
{content}"""


def extract_all(fo_name, pages):
    content      = ""
    news_pages   = []
    other_pages  = []

    # prioritize news/press/blog pages
    for page_url, page_content in pages:
        url_lower = (page_url or "").lower()
        if any(kw in url_lower for kw in ("news", "press", "media", "blog", "insights", "update")):
            news_pages.append((page_url, page_content))
        else:
            other_pages.append((page_url, page_content))

    for page_url, page_content in news_pages + other_pages:
        content += f"\n--- {page_url} ---\n{(page_content or '')[:1500]}\n"

    content = content[:7000]

    if not content.strip():
        return None, "No content in scrape_cache"

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": EXTRACT_PROMPT.format(content=content)
            }],
            temperature=0,
            max_tokens=800,
            response_format={"type": "json_object"}
        )
        result = json.loads(resp.choices[0].message.content.strip())
        return result, None

    except Exception as e:
        log.error("LLM extraction error for %s: %s", fo_name, e)
        return None, str(e)


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("Web enrichment agent started.")

    while True:
        record = claim_record()
        if not record:
            log.info("No pending records. Sleeping %ds...", AGENT_POLL_INTERVAL)
            time.sleep(AGENT_POLL_INTERVAL)
            continue

        record_id, fo_name = record
        log.info("Processing [%d]: %s", record_id, fo_name)

        pages = get_scraped_content(record_id)
        if not pages:
            mark_no_content(record_id, "No scraped content found in scrape_cache")
            time.sleep(AGENT_POLL_INTERVAL)
            continue

        result, error = extract_all(fo_name, pages)

        if error or not result:
            mark_retry(record_id, error or "LLM returned no result")
            time.sleep(AGENT_POLL_INTERVAL)
            continue

        save_fields(record_id, result)

        signals     = result.get("signals") or []
        news_source = next((p[0] for p in pages if any(
            kw in (p[0] or "").lower()
            for kw in ("news", "press", "media", "blog", "insights")
        )), pages[0][0] if pages else None)

        signals_saved = save_signals(record_id, signals, news_source)

        filled = [k for k, v in result.items() if v and k != "signals"]
        log.info("  Fields filled: %s", ", ".join(filled) if filled else "none")
        log.info("  Signals saved: %d", signals_saved)

        time.sleep(AGENT_POLL_INTERVAL)


if __name__ == "__main__":
    main()
