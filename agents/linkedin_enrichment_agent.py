import sys
import re
import time
import requests
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import SERPER_API_KEY, AGENT_POLL_INTERVAL
from database.connection import get_connection
from logger import get_logger

log = get_logger("linkedin_enrichment")

SERPER_URL = "https://google.serper.dev/search"

# words too generic to use as a name-match signal on their own
GENERIC_NAME_WORDS = {
    "family", "office", "offices", "llc", "lp", "llp", "inc", "ltd", "co",
    "group", "capital", "partners", "holdings", "management", "investments",
    "advisors", "advisers", "the", "and", "of", "a",
}


# ── database ──────────────────────────────────────────────────────────────────

def claim_record():
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("START TRANSACTION")
        cur.execute("""
            SELECT id, fo_name, principal_name FROM records
            WHERE search_enrichment_check = 1 AND linkedin_enrichment_check IS NULL
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


def save_result(record_id, company_url, principal_url, flag):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records SET
                fo_corporate_linkedin  = COALESCE(fo_corporate_linkedin, %s),
                principal_linkedin     = COALESCE(principal_linkedin, %s),
                linkedin_enrichment_check = %s
            WHERE id = %s
        """, (company_url, principal_url, flag, record_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error("Save failed for record %s: %s", record_id, e)
    finally:
        cur.close()
        conn.close()


def mark_failed(record_id, reason):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records SET linkedin_enrichment_check = 0, error_message = %s WHERE id = %s
        """, (reason[:500], record_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ── serper search ─────────────────────────────────────────────────────────────

def serper_search(query):
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
        log.warning("Serper search failed for '%s': %s", query, e)
        return []


# ── name matching (no live-page visit - LinkedIn authwalls anonymous/automated ──
# traffic, so we trust Serper's own indexed title/snippet instead of trying to
# load linkedin.com directly)

def significant_tokens(name):
    words = re.findall(r"[a-z0-9]+", name.lower())
    return [w for w in words if w not in GENERIC_NAME_WORDS and len(w) > 2]


def name_matches(name, title, snippet):
    text = f"{title} {snippet}".lower()
    if name.lower() in text:
        return True
    tokens = significant_tokens(name)
    if not tokens:
        return False
    return all(t in text for t in tokens)


# ── find company linkedin ─────────────────────────────────────────────────────

def find_company_linkedin(fo_name):
    results = serper_search(f'"{fo_name}" site:linkedin.com/company')
    for r in results:
        url = r.get("link", "")
        if "linkedin.com/company/" not in url:
            continue
        if name_matches(fo_name, r.get("title", ""), r.get("snippet", "")):
            log.info("  Matched (Serper title/snippet): %s", url)
            return url
    return None


# ── find principal linkedin ───────────────────────────────────────────────────

def find_principal_linkedin(fo_name, principal_name):
    if not principal_name:
        return None
    results = serper_search(f'"{principal_name}" "{fo_name}" site:linkedin.com/in')
    for r in results:
        url = r.get("link", "")
        if "linkedin.com/in/" not in url:
            continue
        title, snippet = r.get("title", ""), r.get("snippet", "")
        if name_matches(principal_name, title, snippet) and name_matches(fo_name, title, snippet):
            log.info("  Matched (Serper title/snippet): %s", url)
            return url
    return None


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("LinkedIn enrichment agent started (Serper-only, no live-page verification).")

    while True:
        record = claim_record()
        if not record:
            log.info("No pending records. Sleeping %ds...", AGENT_POLL_INTERVAL)
            time.sleep(AGENT_POLL_INTERVAL)
            continue

        record_id, fo_name, principal_name = record
        log.info("Processing [%d]: %s | Principal: %s", record_id, fo_name, principal_name or "unknown")

        try:
            company_url   = find_company_linkedin(fo_name)
            principal_url = find_principal_linkedin(fo_name, principal_name)

            log.info("  Company LinkedIn:   %s", company_url or "not found")
            log.info("  Principal LinkedIn: %s", principal_url or "not found")

            flag = 1 if (company_url or principal_url) else 0
            save_result(record_id, company_url, principal_url, flag)

        except Exception as e:
            log.error("Error for record %d: %s", record_id, e)
            mark_failed(record_id, str(e))

        time.sleep(AGENT_POLL_INTERVAL)


if __name__ == "__main__":
    main()
