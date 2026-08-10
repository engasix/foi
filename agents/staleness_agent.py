import sys
import time
import json
import requests
import chromadb
from pathlib import Path
from bs4 import BeautifulSoup
from openai import OpenAI

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import OPENAI_API_KEY, AGENT_POLL_INTERVAL
from database.connection import get_connection
from logger import get_logger
from agents.validation_agent import (
    check_iapd,
    check_principal_in_content,
    check_linkedin,
    calculate_score,
    get_scraped_content,
)

log    = get_logger("staleness")
client = OpenAI(api_key=OPENAI_API_KEY)

SCRAPE_TIMEOUT   = 10
CHROMA_PATH      = Path(__file__).parent.parent / "app" / "rag" / "chroma_db"
COLLECTION_NAME  = "family_offices"
SCORE_DRIFT_THRESHOLD = 10   # re-validate if score shifts by this many points


# ── database ──────────────────────────────────────────────────────────────────

def claim_record():
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("START TRANSACTION")
        cur.execute("""
            SELECT
                r.id, r.fo_name, r.fo_type, r.fo_website,
                r.fo_city, r.fo_state, r.fo_aum,
                r.fo_background, r.fo_investing_thesis, r.fo_investing_mandates,
                r.crd_number, r.discovery_source,
                r.fo_corporate_linkedin, r.principal_name, r.principal_linkedin,
                r.principal_email_source, r.principal_email,
                r.web_enrichment_check, r.search_enrichment_check,
                r.linkedin_enrichment_check, r.email_enrichment_check,
                r.signals_check, r.confidence_score
            FROM records r
            WHERE r.run_cycle = 2
              AND r.validation_check = 1
              AND r.staleness_check IS NULL
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


def get_stored_content(record_id):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT page_url, page_content FROM scrape_cache
            WHERE record_id = %s
            ORDER BY id ASC
            LIMIT 1
        """, (record_id,))
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()


def mark_dead(record_id, reason):
    """Site unreachable - indexer_agent will remove chunks from ChromaDB."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records
            SET staleness_check = 1, staleness_reason = %s
            WHERE id = %s
        """, (reason, record_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def mark_valid(record_id, reason):
    """Re-validation passed - record is current and accurate."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records
            SET staleness_check = 0, staleness_reason = %s
            WHERE id = %s
        """, (reason, record_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def mark_revalidation_needed(record_id, reason):
    """
    Re-validation found score/check drift. Remove from ChromaDB index now,
    reset pipeline so validation_agent re-scores with fresh data.
    """
    remove_from_chroma(record_id)

    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records SET
                staleness_check  = 0,
                staleness_reason = %s,
                validation_check = NULL,
                index_check      = NULL
            WHERE id = %s
        """, (reason, record_id))
        conn.commit()
        log.info("  Re-validation queued. Removed from index. validation_agent will re-score.")
    except Exception as e:
        conn.rollback()
        log.error("  mark_revalidation_needed failed: %s", e)
    finally:
        cur.close()
        conn.close()


def trigger_reenrichment(record_id, new_content, reason):
    """
    Content materially changed. Save fresh scrape, reset full enrichment pipeline
    so all agents re-process with updated data.
    """
    remove_from_chroma(record_id)

    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE scrape_cache
            SET page_content = %s, scraped_at = NOW()
            WHERE record_id = %s
            ORDER BY id ASC
            LIMIT 1
        """, (new_content, record_id))

        cur.execute("""
            UPDATE records SET
                web_enrichment_check      = NULL,
                search_enrichment_check   = NULL,
                linkedin_enrichment_check = NULL,
                email_enrichment_check    = NULL,
                signals_check             = NULL,
                validation_check          = NULL,
                index_check               = NULL,
                staleness_check           = 0,
                staleness_reason          = %s
            WHERE id = %s
        """, (f"Re-enrichment triggered: {reason}", record_id))

        conn.commit()
        log.info("  Re-enrichment triggered for [%d]. Pipeline reset from web_enrichment.", record_id)
    except Exception as e:
        conn.rollback()
        log.error("  Re-enrichment reset failed for [%d]: %s", record_id, e)
    finally:
        cur.close()
        conn.close()


# ── chromadb removal ──────────────────────────────────────────────────────────

def remove_from_chroma(record_id):
    """Delete all 3 chunks for this record from ChromaDB."""
    try:
        db  = chromadb.PersistentClient(path=str(CHROMA_PATH))
        col = db.get_collection(COLLECTION_NAME)
        chunk_ids = [f"{record_id}_a", f"{record_id}_b", f"{record_id}_c"]
        col.delete(ids=chunk_ids)
        log.info("  Removed chunks from ChromaDB: %s", chunk_ids)
    except Exception as e:
        log.warning("  ChromaDB removal failed for [%d]: %s", record_id, e)


# ── scraping ──────────────────────────────────────────────────────────────────

def scrape_page(url):
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=SCRAPE_TIMEOUT,
            allow_redirects=True
        )
        if resp.status_code == 403:
            return None, "HTTP 403"
        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}"
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        return text[:5000] if text else None, None
    except requests.exceptions.ConnectionError:
        return None, "Connection refused - site may be down"
    except requests.exceptions.Timeout:
        return None, "Request timed out"
    except Exception as e:
        return None, str(e)


# ── LLM content comparison ────────────────────────────────────────────────────

COMPARE_PROMPT = """You are checking if a family office website has materially changed since it was last recorded.

Held data about this firm:
- Name: {fo_name}
- Principal: {principal_name}
- Background: {fo_background}

Previously scraped content:
{old_content}

Newly scraped content:
{new_content}

Respond in JSON only:
{{
  "outcome": "dead" | "changed" | "valid",
  "reason": "one sentence of evidence"
}}

Outcomes:
- "dead": firm appears to no longer exist, shut down, or been fully acquired with no trace
- "changed": firm still exists but data has materially changed (principal changed, focus shifted, major restructure)
- "valid": no material changes detected

Do NOT use "changed" for minor wording updates, design refreshes, or new blog posts.
Use "changed" only when the underlying data we store (principal name, investment focus, firm identity) has actually shifted."""


def compare_content(fo_name, principal_name, fo_background, old_content, new_content):
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": COMPARE_PROMPT.format(
                    fo_name=fo_name,
                    principal_name=principal_name or "unknown",
                    fo_background=fo_background or "not recorded",
                    old_content=old_content[:2000],
                    new_content=new_content[:2000],
                )
            }],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=150
        )
        result  = json.loads(resp.choices[0].message.content)
        outcome = result.get("outcome", "valid")
        reason  = result.get("reason", "LLM returned no reason")
        return outcome, reason
    except Exception as e:
        log.warning("LLM compare failed: %s", e)
        return "valid", f"LLM comparison failed: {e}"


# ── re-validation ─────────────────────────────────────────────────────────────

def revalidate(row):
    """
    Re-run IAPD check + principal check + confidence score.
    Compare with stored score. If drift > threshold or critical check changed,
    remove from index and queue re-validation. Otherwise mark valid.
    """
    record_id      = row[0]
    fo_name        = row[1]
    fo_city        = row[4]
    fo_state       = row[5]
    crd_number     = row[10]
    principal_name = row[13]
    stored_score   = row[22] or 0

    log.info("  Re-validating [%d]: %s | stored score: %d", record_id, fo_name, stored_score)

    pages = get_scraped_content(record_id)

    iapd                = check_iapd(crd_number)
    iapd_name_match     = False
    iapd_location_match = False

    if iapd:
        iapd_name_match     = fo_name.upper() == iapd["firmName"].upper()
        our_city            = (fo_city or "").upper()
        our_state           = (fo_state or "").upper()
        iapd_location_match = (our_city == iapd["city"].upper() and our_state == iapd["state"].upper())
        log.info("  IAPD: %s | active: %s | name match: %s",
                 iapd["firmName"], iapd["active"], iapd_name_match)

        if not iapd["active"]:
            mark_revalidation_needed(
                record_id,
                f"IAPD registration no longer active for {fo_name}. Score reset for re-validation."
            )
            return
    else:
        log.info("  IAPD: no CRD or not found")

    principal_in_content = check_principal_in_content(principal_name, pages)
    log.info("  Principal in content: %s", principal_in_content)

    new_score = calculate_score(
        row[:22], iapd, iapd_name_match, iapd_location_match, principal_in_content
    )
    log.info("  New score: %d | Stored score: %d | Drift: %d", new_score, stored_score, abs(new_score - stored_score))

    if abs(new_score - stored_score) > SCORE_DRIFT_THRESHOLD:
        mark_revalidation_needed(
            record_id,
            f"Score drifted from {stored_score} to {new_score} in Run 2. Removed from index, queued for re-validation."
        )
    else:
        mark_valid(
            record_id,
            f"Re-validation passed. Score: {new_score}/100. No material drift from stored {stored_score}."
        )


# ── process one record ────────────────────────────────────────────────────────

def process(row):
    record_id      = row[0]
    fo_name        = row[1]
    fo_website     = row[3]
    principal_name = row[13]
    fo_background  = row[7]

    log.info("Staleness check [%d]: %s", record_id, fo_name)

    # Step 1: website liveness
    if not fo_website:
        mark_dead(record_id, reason="No website on record")
        return

    new_content, scrape_error = scrape_page(fo_website)

    if scrape_error == "HTTP 403":
        log.info("  Website blocked scraper (403). Skipping content compare, running re-validation.")
        revalidate(row)
        return

    if scrape_error:
        log.info("  Website unreachable: %s", scrape_error)
        mark_dead(record_id, reason=f"Website went dark: {scrape_error}")
        return

    log.info("  Website live. Scraped %d chars.", len(new_content or ""))

    # Step 2: get stored content for LLM comparison
    stored = get_stored_content(record_id)
    if not stored or not stored[1]:
        log.info("  No stored content to compare. Running re-validation only.")
        revalidate(row)
        return

    _, old_content = stored

    # Step 3: LLM content comparison - dead / changed / valid
    outcome, reason = compare_content(
        fo_name, principal_name, fo_background, old_content, new_content
    )
    log.info("  Content outcome: %s | %s", outcome, reason)

    if outcome == "dead":
        mark_dead(record_id, reason=reason)

    elif outcome == "changed":
        trigger_reenrichment(record_id, new_content, reason)

    else:
        # Site live, content unchanged - now re-validate stored data accuracy
        revalidate(row)


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("Staleness agent started. Waiting for run_cycle=2 records...")

    while True:
        row = claim_record()
        if not row:
            log.info("No run_cycle=2 records pending. Sleeping %ds...", AGENT_POLL_INTERVAL)
            time.sleep(AGENT_POLL_INTERVAL)
            continue
        try:
            process(row)
        except Exception as e:
            log.error("Error for record %d: %s", row[0], e)
            mark_valid(row[0], reason=f"Agent error - marked valid by default: {e}")
        time.sleep(AGENT_POLL_INTERVAL)


if __name__ == "__main__":
    main()
