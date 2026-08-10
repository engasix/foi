import sys
import time
import requests
from pathlib import Path
from urllib.parse import urlparse

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import HUNTER_API_KEY, AGENT_POLL_INTERVAL
from database.connection import get_connection
from logger import get_logger

log = get_logger("email_enrichment")

HUNTER_URL = "https://api.hunter.io/v2/email-finder"
ERROR_BACKOFF = 5   # small extra pause after an API error, on top of the normal poll interval


# ── database ──────────────────────────────────────────────────────────────────

def claim_record():
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("START TRANSACTION")
        cur.execute("""
            SELECT id, fo_name, principal_name, fo_website FROM records
            WHERE linkedin_enrichment_check IS NOT NULL
              AND email_enrichment_check IS NULL
            ORDER BY
                CASE WHEN principal_linkedin LIKE '%linkedin.com/in%' THEN 0 ELSE 1 END,
                id ASC
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


def save_result(record_id, email, status, source, flag, error_message=None):
    """
    error_message is only set when the lookup itself failed (API error, rate
    limit, etc) - kept NULL on a clean "no email found" result, so the two
    cases stay distinguishable later (e.g. to bulk-reset only the error ones
    for a retry) even though both land on email_enrichment_check=0.
    """
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records SET
                principal_email         = %s,
                principal_email_code    = %s,
                principal_email_source  = %s,
                email_enrichment_check  = %s,
                error_message           = %s
            WHERE id = %s
        """, (email, status, source, flag, error_message, record_id))
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
            UPDATE records SET email_enrichment_check = 0, error_message = %s WHERE id = %s
        """, (reason[:500], record_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def mark_no_lookup(record_id, reason):
    """Nothing to look up (no principal_name or no website) - pass through rather
    than leaving email_enrichment_check NULL forever and blocking validation."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records SET email_enrichment_check = 0, error_message = %s WHERE id = %s
        """, (reason[:500], record_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ── Hunter.io lookup ──────────────────────────────────────────────────────────

def lookup_email(fo_website, principal_name):
    """
    Returns (email, status, source, error_message).

    error_message is set whenever Hunter.io did NOT give us a real answer (rate
    limited, account/quota problem, server error, network error, etc) - the
    caller still records email_enrichment_check=0 so the pipeline keeps moving,
    but the error is preserved so it's identifiable and retriable later, unlike
    a genuine "no email found" result (error_message=None).
    """
    domain = urlparse(fo_website).netloc.lstrip("www.").lower()
    if not domain:
        return None, None, None, f"Could not parse domain from: {fo_website}"

    parts = principal_name.strip().split()
    if len(parts) < 2:
        return None, None, None, f"Principal name too short to split: {principal_name}"

    first = parts[0]
    last  = parts[-1]

    log.info("  Hunter.io: %s %s @ %s", first, last, domain)

    try:
        resp = requests.get(
            HUNTER_URL,
            params={
                "domain":     domain,
                "first_name": first,
                "last_name":  last,
                "api_key":    HUNTER_API_KEY
            },
            timeout=10
        )

        if resp.status_code == 429:
            log.warning("  Hunter.io rate limit hit (429).")
            return None, None, None, "Hunter.io error: 429 rate limited"

        if resp.status_code in (401, 403):
            log.error(
                "  Hunter.io returned %d (%s) - likely an invalid API key or your account's "
                "search/credit limit has been reached. Check https://hunter.io/api_keys and "
                "your usage dashboard.",
                resp.status_code, resp.text[:200]
            )
            return None, None, None, f"Hunter.io error: {resp.status_code} - check API key/account credit limit"

        if resp.status_code >= 500:
            log.warning("  Hunter.io server error (%d).", resp.status_code)
            return None, None, None, f"Hunter.io error: {resp.status_code} server error"

        resp.raise_for_status()
        data    = resp.json().get("data", {})
        email   = data.get("email")
        status  = data.get("verification", {}).get("status")
        sources = data.get("sources", [])
        source  = sources[0].get("uri") if sources else None

        if email:
            log.info("  Found: %s | status: %s", email, status)
        else:
            log.info("  Not found.")

        return email, status, source, None

    except Exception as e:
        log.warning("  Hunter.io error: %s", e)
        return None, None, None, f"Hunter.io error: {e}"


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("Email enrichment agent started.")

    while True:
        record = claim_record()
        if not record:
            log.info("No pending records. Sleeping %ds...", AGENT_POLL_INTERVAL)
            time.sleep(AGENT_POLL_INTERVAL)
            continue

        record_id, fo_name, principal_name, fo_website = record
        log.info("Processing [%d]: %s | %s", record_id, fo_name, principal_name)

        if not principal_name or not fo_website:
            missing = "principal_name" if not principal_name else "fo_website"
            log.info("  Skipping lookup - no %s on record.", missing)
            mark_no_lookup(record_id, f"No {missing} to look up an email with")
            time.sleep(AGENT_POLL_INTERVAL)
            continue

        try:
            email, status, source, error_message = lookup_email(fo_website, principal_name)
            flag = 1 if email else 0
            save_result(record_id, email, status, source, flag, error_message)

            if error_message:
                time.sleep(ERROR_BACKOFF)

        except Exception as e:
            log.error("Error for record %d: %s", record_id, e)
            mark_failed(record_id, str(e))

        time.sleep(AGENT_POLL_INTERVAL)


if __name__ == "__main__":
    main()
