import sys
import re
import json
import time
from pathlib import Path
import requests
from openai import OpenAI
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import SERPER_API_KEY, OPENAI_API_KEY, AGENT_POLL_INTERVAL
from database.connection import get_connection
from logger import get_logger

log    = get_logger("search_enrichment")
client = OpenAI(api_key=OPENAI_API_KEY)

SERPER_URL        = "https://google.serper.dev/search"
RESULTS_PER_PAGE  = 10
MAX_SERPER_PAGES  = 3     # go up to 3 pages of Google results per query, if available
MAX_URLS_TO_SCRAPE = 15   # safety cap across all queries combined, per record
PAGE_TIMEOUT        = 15_000
NETWORKIDLE_TIMEOUT = 5_000
MAX_CONTENT_LEN    = 12_000
SKIP_DOMAINS       = ("linkedin.com", "facebook.com", "youtube.com", "twitter.com",
                      "instagram.com", "x.com", "tiktok.com")

MIN_SIGNALS   = 3   # if a record already has this many signals, skip signal-hunting queries
SIGNAL_TYPES  = ("investment", "fund_commitment", "key_hire", "news")

# words too generic to use as a name-attribution signal on their own
GENERIC_NAME_WORDS = {
    "family", "office", "offices", "llc", "lp", "llp", "inc", "ltd", "co",
    "group", "capital", "partners", "holdings", "management", "investments",
    "advisors", "advisers", "the", "and", "of", "a",
}

TARGET_FIELDS = [
    "principal_name", "principal_title",
    "fo_investing_thesis", "fo_investing_mandates",
    "fo_background", "fo_aum", "fo_type",
]

FIELD_DESCRIPTIONS = {
    "principal_name":        "Full name of the founder, CIO, CEO, or managing principal",
    "principal_title":       "Their exact title (e.g. Founder & CIO, Managing Director)",
    "fo_investing_thesis":   "What they invest in - asset classes, sectors, strategies",
    "fo_investing_mandates": "Deal size, stage, geography, check size preferences",
    "fo_background":         "2-3 sentence summary of what this firm does",
    "fo_aum":                "Assets under management if mentioned (e.g. $500M, $2B)",
    "fo_type":                "'SFO' if it manages wealth for a single family, 'MFO' if for multiple families",
}


# ── database ──────────────────────────────────────────────────────────────────

def claim_record():
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("START TRANSACTION")
        cur.execute("""
            SELECT id, fo_name, fo_city, fo_state, fo_type,
                   principal_name, principal_title,
                   fo_investing_thesis, fo_investing_mandates,
                   fo_background, fo_aum
            FROM records
            WHERE web_enrichment_check = 1 AND search_enrichment_check IS NULL
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


def get_missing_fields(row):
    cols = ["id", "fo_name", "fo_city", "fo_state", "fo_type",
            "principal_name", "principal_title",
            "fo_investing_thesis", "fo_investing_mandates",
            "fo_background", "fo_aum"]
    data = dict(zip(cols, row))
    missing = []
    for f in TARGET_FIELDS:
        val = data.get(f)
        if f == "fo_type":
            if not val or val == "Unknown":
                missing.append(f)
        elif not val:
            missing.append(f)
    return missing


def save_result(record_id, extracted, flag):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        def flatten(v):
            if isinstance(v, dict):
                parts = [str(x) for x in v.values() if x is not None]
                return ", ".join(parts) if parts else None
            if isinstance(v, list):
                return ", ".join(str(x) for x in v if x is not None) or None
            return v

        writable = {
            k: flatten(v)
            for k, v in extracted.items()
            if k in TARGET_FIELDS
        }
        writable = {
            k: v for k, v in writable.items()
            if v and str(v).strip() not in ("", "null", "NULL", "None")
        }

        fo_type_val = writable.pop("fo_type", None)
        if fo_type_val:
            fo_type_val = fo_type_val.strip().upper()
            if fo_type_val not in ("SFO", "MFO"):
                fo_type_val = None

        set_parts  = []
        set_values = []
        for col, val in writable.items():
            set_parts.append(f"{col} = COALESCE({col}, %s)")
            set_values.append(val)
        if fo_type_val:
            set_parts.append("fo_type = CASE WHEN fo_type IS NULL OR fo_type = 'Unknown' THEN %s ELSE fo_type END")
            set_values.append(fo_type_val)

        if set_parts:
            set_values.append(flag)
            cur.execute(
                f"UPDATE records SET {', '.join(set_parts)}, search_enrichment_check = %s WHERE id = %s",
                (*set_values, record_id)
            )
        else:
            cur.execute(
                "UPDATE records SET search_enrichment_check = %s WHERE id = %s",
                (flag, record_id)
            )
        conn.commit()
        return len(writable) + (1 if fo_type_val else 0)
    except Exception as e:
        conn.rollback()
        log.error("Save failed for record %s: %s", record_id, e)
        return 0
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


def is_duplicate_signal(headline, source_url, existing):
    headline_lower = (headline or "").lower()
    for ex_headline, ex_url in existing:
        if ex_headline and ex_headline.lower() == headline_lower:
            return True
        if source_url and ex_url and ex_url == source_url:
            return True
    return False


def save_new_signals(record_id, fo_name, signals, existing):
    if not signals:
        return 0
    conn = get_connection()
    cur  = conn.cursor()
    saved = 0
    try:
        for s in signals:
            headline    = (s.get("headline") or "").strip()
            description = (s.get("description") or "").strip()
            source_url  = s.get("source_url") or None
            signal_type = str(s.get("signal_type", "news")).lower()
            signal_date = s.get("signal_date") or None

            if not headline or not description:
                continue
            if signal_type not in SIGNAL_TYPES:
                signal_type = "news"
            if not content_mentions_entity(fo_name, f"{headline} {description}"):
                continue
            if is_duplicate_signal(headline, source_url, existing):
                continue

            cur.execute("""
                INSERT INTO signals
                    (record_id, headline, source, signal_type, description, signal_date, source_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (record_id, headline[:500], "search", signal_type,
                  description, signal_date, source_url))
            existing.append((headline, source_url))
            saved += 1
        conn.commit()
        return saved
    except Exception as e:
        conn.rollback()
        log.error("Save signals failed for record %s: %s", record_id, e)
        return saved
    finally:
        cur.close()
        conn.close()


def mark_retry(record_id, reason):
    """Transient failure - leave search_enrichment_check NULL so it's retried."""
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


# ── serper search (multi-pattern, multi-page) ─────────────────────────────────

def serper_search_page(query, page):
    try:
        resp = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": RESULTS_PER_PAGE, "page": page},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("organic", [])
    except Exception as e:
        log.warning("Serper search failed for '%s' (page %d): %s", query, page, e)
        return []


def serper_search_all_pages(query):
    """Fetch up to MAX_SERPER_PAGES pages of results for one query."""
    results = []
    for page in range(1, MAX_SERPER_PAGES + 1):
        page_results = serper_search_page(query, page)
        if not page_results:
            break
        results.extend(page_results)
        if len(page_results) < RESULTS_PER_PAGE:
            break
    return results


def build_queries(fo_name, location, missing, hunt_signals):
    queries = [f'"{fo_name}"', fo_name]

    people_fields   = [f for f in missing if f in ("principal_name", "principal_title")]
    invest_fields   = [f for f in missing if f in ("fo_investing_thesis", "fo_investing_mandates")]
    profile_fields  = [f for f in missing if f in ("fo_background", "fo_aum")]

    if people_fields:
        queries.append(f'"{fo_name}" founder CEO principal managing director {location}'.strip())
    if invest_fields:
        queries.append(f'"{fo_name}" investment strategy portfolio focus {location}'.strip())
    if profile_fields:
        queries.append(f'"{fo_name}" assets under management AUM overview {location}'.strip())
    if "fo_type" in missing:
        queries.append(f'"{fo_name}" "single family office" OR "multi-family office"')
    if hunt_signals:
        queries.append(f'"{fo_name}" investment OR acquisition OR "fund close" OR hire OR news'.strip())

    return queries


# ── name attribution check ────────────────────────────────────────────────────

def significant_tokens(fo_name):
    words = re.findall(r"[a-z0-9]+", fo_name.lower())
    return [w for w in words if w not in GENERIC_NAME_WORDS and len(w) > 2]


def content_mentions_entity(fo_name, text):
    """
    Cheap pre-filter so we don't hand the LLM (and pay to have it read) content
    that isn't even nominally about this firm - a same-ish-named-but-different
    entity should get filtered here before it ever reaches extraction.
    """
    if not text:
        return False
    text_lower = text.lower()
    if fo_name.lower() in text_lower:
        return True
    tokens = significant_tokens(fo_name)
    if not tokens:
        return False
    return all(t in text_lower for t in tokens)


# ── real-browser scraping ─────────────────────────────────────────────────────

def scrape_url(url, browser):
    if any(d in url for d in SKIP_DOMAINS):
        return None
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    )
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT)
        except PlaywrightTimeout:
            pass
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
        text = " ".join((text or "").split())
        return text[:3000] if text else None
    except Exception as e:
        log.info("  Scrape failed: %s (%s)", url, e)
        return None
    finally:
        context.close()


# ── build combined content for LLM ────────────────────────────────────────────

def build_content(fo_name, results, browser):
    seen_urls = set()
    blocks    = [f"Search results for: {fo_name}\n"]

    # snippets from every organic result, deduped by url
    for r in results:
        url = r.get("link", "")
        if not url or url in seen_urls or any(d in url for d in SKIP_DOMAINS):
            continue
        seen_urls.add(url)
        snippet_block = f"[{r.get('title', '')}] {r.get('snippet', '')} ({url})"
        if content_mentions_entity(fo_name, snippet_block):
            blocks.append(snippet_block)

    # scrape actual pages for the deduped url set, capped for runtime/cost
    scraped = 0
    for url in list(seen_urls)[:MAX_URLS_TO_SCRAPE]:
        text = scrape_url(url, browser)
        if not text:
            continue
        if not content_mentions_entity(fo_name, text):
            log.info("  Skipped (name not confirmed in content): %s", url)
            continue
        blocks.append(f"\n--- {url} ---\n{text}")
        scraped += 1

    log.info("  Scraped %d/%d candidate URLs", scraped, min(len(seen_urls), MAX_URLS_TO_SCRAPE))
    return "\n".join(blocks)[:MAX_CONTENT_LEN]


# ── LLM extraction ────────────────────────────────────────────────────────────

SIGNALS_INSTRUCTION = """- signals: an array of recent, concrete news/events about this firm - investments made,
  fund commitments, key hires, notable news. Each item:
  {"headline": "...", "signal_type": "investment|fund_commitment|key_hire|news",
   "description": "one sentence on what happened and why it matters",
   "signal_date": "YYYY-MM-DD if a date is given, else null",
   "source_url": "the exact URL (from one of the --- url --- blocks above) this came from"}
  Only include concrete events, not general descriptions of the firm. Return an empty array if nothing found."""


def extract_with_llm(fo_name, missing_fields, content):
    field_instructions = "\n".join(
        f"- {f}: {FIELD_DESCRIPTIONS[f]}" for f in missing_fields
    ) or "(none - all core fields already filled, only look for signals below)"
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You are a research analyst extracting data about a specific family office "
                        f"named \"{fo_name}\" from web search results and scraped pages.\n\n"
                        f"CRITICAL: only use content that is clearly and specifically about \"{fo_name}\". "
                        f"If a passage appears to be about a different company, person, or family office that "
                        f"merely shares a similar or partial name, ignore it entirely - do not use it, and do "
                        f"not guess. Return JSON null (or an empty array for signals) for anything you cannot "
                        f"confidently attribute to this firm.\n\n"
                        f"Extract these fields:\n{field_instructions}\n\n"
                        f"Also extract:\n{SIGNALS_INSTRUCTION}"
                    )
                },
                {"role": "user", "content": f"Family office: {fo_name}\n\n{content}"}
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=900
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        log.error("LLM extraction error for %s: %s", fo_name, e)
        return None


# ── enrich one record ─────────────────────────────────────────────────────────

def enrich(row, browser):
    record_id = row[0]
    fo_name   = row[1]
    fo_city   = row[2]
    fo_state  = row[3]
    location  = f"{fo_city}, {fo_state}" if fo_city and fo_state else (fo_city or fo_state or "")

    missing          = get_missing_fields(row)
    existing_signals = get_existing_signals(record_id)
    hunt_signals     = len(existing_signals) < MIN_SIGNALS

    if not missing and not hunt_signals:
        log.info("  [%d] %s - all fields filled and enough signals, skipping.", record_id, fo_name)
        save_result(record_id, {}, flag=1)
        return

    log.info("Enriching [%d]: %s | missing: %s | existing signals: %d",
              record_id, fo_name, missing, len(existing_signals))

    queries = build_queries(fo_name, location, missing, hunt_signals)

    all_results = []
    for q in queries:
        results = serper_search_all_pages(q)
        log.info("  Query '%s': %d results across pages", q[:60], len(results))
        all_results.extend(results)

    if not all_results:
        save_result(record_id, {}, flag=1)
        return

    content = build_content(fo_name, all_results, browser)
    if not content.strip():
        save_result(record_id, {}, flag=1)
        return

    extracted = extract_with_llm(fo_name, missing, content)
    if extracted is None:
        mark_retry(record_id, "LLM extraction failed")
        return

    signals = extracted.pop("signals", []) if isinstance(extracted, dict) else []
    if not isinstance(signals, list):
        signals = []

    log.info("  Extracted: %s", {k: v for k, v in extracted.items() if v})
    written = save_result(record_id, extracted, flag=1)
    log.info("  Fields written: %d", written)

    signals_saved = save_new_signals(record_id, fo_name, signals, existing_signals)
    log.info("  Signals saved: %d", signals_saved)


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("Search enrichment agent started.")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        log.info("Browser ready (non-headless).")

        while True:
            row = claim_record()
            if not row:
                log.info("No pending records. Sleeping %ds...", AGENT_POLL_INTERVAL)
                time.sleep(AGENT_POLL_INTERVAL)
                continue
            try:
                enrich(row, browser)
            except Exception as e:
                log.error("Unexpected error for record %d: %s", row[0], e)
                mark_retry(row[0], str(e))
            time.sleep(AGENT_POLL_INTERVAL)

        browser.close()


if __name__ == "__main__":
    main()
