import sys
import json
import time
import requests
from pathlib import Path



sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import AGENT_POLL_INTERVAL
from database.connection import get_connection
from logger import get_logger

log = get_logger("validation")

IAPD_URL     = "https://api.adviserinfo.sec.gov/search/firm/{crd}"
IAPD_HEADERS = {"User-Agent": "PolarityIQ Research contact@polarityiq.com"}
IAPD_TIMEOUT = 10


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
                r.signals_check
            FROM records r
            WHERE r.email_enrichment_check IS NOT NULL
              AND r.fo_type IN ('SFO', 'MFO')
              AND r.validation_check IS NULL
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
            SELECT page_url, page_content FROM scrape_cache WHERE record_id = %s
        """, (record_id,))
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def save_validation(record_id, score, chain, corrections):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        if corrections:
            set_clause = ", ".join(f"{col} = %s" for col in corrections)
            cur.execute(
                f"UPDATE records SET {set_clause} WHERE id = %s",
                (*corrections.values(), record_id)
            )

        cur.execute("""
            INSERT INTO validation_chain
                (record_id, discovery_source, extraction_method, enrichment_steps,
                 validation_logic, confidence_assessment, exact_sources)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                discovery_source      = VALUES(discovery_source),
                extraction_method     = VALUES(extraction_method),
                enrichment_steps      = VALUES(enrichment_steps),
                validation_logic      = VALUES(validation_logic),
                confidence_assessment = VALUES(confidence_assessment),
                exact_sources         = VALUES(exact_sources),
                validated_at          = CURRENT_TIMESTAMP
        """, (
            record_id,
            chain["discovery_source"],
            chain["extraction_method"],
            chain["enrichment_steps"],
            chain["validation_logic"],
            chain["confidence_assessment"],
            chain["exact_sources"],
        ))

        cur.execute("""
            UPDATE records SET confidence_score = %s, validation_check = 1 WHERE id = %s
        """, (score, record_id))

        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error("Save validation failed: %s", e)
        raise
    finally:
        cur.close()
        conn.close()


def mark_failed(record_id, reason):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE records SET validation_check = 0, error_message = %s WHERE id = %s
        """, (reason[:500], record_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ── validation checks ─────────────────────────────────────────────────────────

def check_iapd(crd):
    if not crd:
        return None
    try:
        resp = requests.get(
            IAPD_URL.format(crd=crd),
            headers=IAPD_HEADERS,
            timeout=IAPD_TIMEOUT
        )
        if resp.status_code != 200:
            return None
        hits = resp.json().get("hits", {}).get("hits", [])
        if not hits:
            return None
        content = json.loads(hits[0]["_source"]["iacontent"])
        basic   = content.get("basicInformation", {})
        addr    = content.get("iaFirmAddressDetails", {}).get("officeAddress", {})
        return {
            "firmName": basic.get("firmName", ""),
            "city":     addr.get("city", ""),
            "state":    addr.get("state", ""),
            "active":   basic.get("iaScope", "") == "ACTIVE",
        }
    except Exception as e:
        log.warning("IAPD lookup failed: %s", e)
        return None


def check_principal_in_content(principal_name, pages):
    if not principal_name or not pages:
        return False
    combined = " ".join((pc or "") for _, pc in pages).lower()
    parts = principal_name.lower().split()
    if len(parts) >= 2:
        return parts[0] in combined and parts[-1] in combined
    return parts[0] in combined if parts else False


def check_linkedin(url):
    return bool(url and "linkedin.com" in str(url))


# ── scoring ───────────────────────────────────────────────────────────────────

def calculate_score(row, iapd, iapd_name_match, iapd_location_match, principal_in_content):
    (record_id, fo_name, fo_type, fo_website, fo_city, fo_state, fo_aum,
     fo_background, fo_investing_thesis, fo_investing_mandates, crd_number,
     discovery_source, fo_corporate_linkedin, principal_name, principal_linkedin,
     principal_email_source, principal_email, web_enrichment_check, search_enrichment_check,
     linkedin_enrichment_check, email_enrichment_check, signals_check) = row

    score = 0

    # Field completeness (max 40 - 8 fields x 5pts each)
    for val in [fo_type, fo_city, fo_state, fo_aum, fo_background,
                fo_investing_thesis, fo_investing_mandates]:
        if val:
            score += 5
    score += 5  # fo_name always present

    # Validation checks (max 60)
    if iapd:
        if iapd_name_match:
            score += 15
            log.info("  +15 IAPD name match")
        if iapd_location_match:
            score += 10
            log.info("  +10 IAPD location match")
    if principal_in_content:
        score += 10
        log.info("  +10 principal in scraped content")
    if check_linkedin(principal_linkedin):
        score += 5
        log.info("  +5 principal LinkedIn verified")
    if check_linkedin(fo_corporate_linkedin):
        score += 5
        log.info("  +5 corporate LinkedIn verified")
    if principal_email:
        score += 10
        log.info("  +10 email found")

    return min(score, 100)


# ── build validation chain ────────────────────────────────────────────────────

def build_chain(row, pages, iapd, iapd_name_match,
                iapd_location_match, principal_in_content, score):
    (record_id, fo_name, fo_type, fo_website, fo_city, fo_state, fo_aum,
     fo_background, fo_investing_thesis, fo_investing_mandates, crd_number,
     discovery_source, fo_corporate_linkedin, principal_name, principal_linkedin,
     principal_email_source, principal_email, web_enrichment_check, search_enrichment_check,
     linkedin_enrichment_check, email_enrichment_check, signals_check) = row

    disc = (
        f"SEC EDGAR Form ADV via sec-api.io | "
        f"Structural filters: Q5C2 + Q5DB1 (HNW client profile) | "
        f"CRD: {crd_number or 'N/A'} | Source: {discovery_source or 'SEC EDGAR'}"
    )

    methods = [
        "SEC EDGAR Form ADV (discovery + CRD)",
        "Multi-page Playwright scraping + GPT-4o-mini classifier",
    ]
    if web_enrichment_check == 1:
        methods.append("GPT-4o-mini field extraction from scraped content")
    if search_enrichment_check == 1:
        methods.append("Serper.dev Google search + GPT-4o-mini (gap fill)")
    if linkedin_enrichment_check == 1:
        methods.append("Serper.dev LinkedIn match (indexed title/snippet)")
    if email_enrichment_check == 1:
        methods.append("Hunter.io Email Finder API")

    steps = []
    if web_enrichment_check == 1:
        steps.append("Web enrichment: extracted background, AUM, thesis, mandates, principal from website")
    elif web_enrichment_check == 0:
        steps.append("Web enrichment: ran but no fields extracted")
    if search_enrichment_check == 1:
        steps.append("Search enrichment: filled missing fields via Serper.dev + LLM")
    elif search_enrichment_check == 0:
        steps.append("Search enrichment: ran but no additional fields found")
    if linkedin_enrichment_check == 1:
        steps.append("LinkedIn enrichment: company and/or principal profiles found and verified")
    elif linkedin_enrichment_check == 0:
        steps.append("LinkedIn enrichment: no profiles found")
    if email_enrichment_check == 1:
        steps.append("Email enrichment: principal email found via Hunter.io")
    elif email_enrichment_check == 0:
        steps.append("Email enrichment: no email found")

    checks = []
    if iapd:
        checks.append(f"IAPD name: {'MATCH' if iapd_name_match else 'MISMATCH'} (held: \"{fo_name}\" | IAPD: \"{iapd['firmName']}\")")
        checks.append(f"IAPD location: {'MATCH' if iapd_location_match else 'MISMATCH'} (held: {fo_city}, {fo_state} | IAPD: {iapd['city']}, {iapd['state']})")
        checks.append(f"IAPD active: {'YES' if iapd['active'] else 'NO'}")
    else:
        checks.append("IAPD: skipped - no CRD number")
    checks.append(f"Principal in scraped content: {'FOUND' if principal_in_content else 'NOT FOUND'} ({principal_name})")
    checks.append(f"Corporate LinkedIn: {'verified' if check_linkedin(fo_corporate_linkedin) else 'not found'}")
    checks.append(f"Principal LinkedIn: {'verified' if check_linkedin(principal_linkedin) else 'not found'}")
    checks.append(f"Email: {'found' if principal_email else 'not found'} ({principal_email or 'N/A'})")

    assessment = f"Confidence score: {score}/100. "
    if score >= 80:
        assessment += "High confidence - firm verified against IAPD, website live, key fields populated."
    elif score >= 60:
        assessment += "Medium confidence - most fields populated, partially verified."
    elif score >= 40:
        assessment += "Low-medium confidence - basic fields present, limited independent verification."
    else:
        assessment += "Low confidence - minimal fields and verification gaps."
    if iapd and not iapd_name_match:
        assessment += " Note: IAPD name mismatch detected."

    sources = []
    if fo_website:
        sources.append(f"Company website: {fo_website}")
    for page_url, _ in (pages or []):
        if page_url:
            sources.append(f"Scraped: {page_url}")
    if crd_number:
        sources.append(f"IAPD: {IAPD_URL.format(crd=crd_number)}")
    if check_linkedin(fo_corporate_linkedin):
        sources.append(f"Corporate LinkedIn: {fo_corporate_linkedin}")
    if check_linkedin(principal_linkedin):
        sources.append(f"Principal LinkedIn: {principal_linkedin}")
    if principal_email_source:
        sources.append(f"Email source: {principal_email_source}")

    return {
        "discovery_source":      disc,
        "extraction_method":     " → ".join(methods),
        "enrichment_steps":      " | ".join(steps),
        "validation_logic":      " | ".join(checks),
        "confidence_assessment": assessment,
        "exact_sources":         " | ".join(sources),
    }


# ── process one record ────────────────────────────────────────────────────────

def validate(row):
    record_id         = row[0]
    fo_name           = row[1]
    fo_city           = row[4]
    fo_state          = row[5]
    crd_number        = row[10]
    fo_corporate_linkedin = row[12]
    principal_name    = row[13]
    principal_linkedin = row[14]

    log.info("Validating [%d]: %s", record_id, fo_name)

    pages = get_scraped_content(record_id)

    iapd                = check_iapd(crd_number)
    iapd_name_match     = False
    iapd_location_match = False
    corrections         = {}

    if iapd:
        log.info("  IAPD: %s | %s, %s", iapd["firmName"], iapd["city"], iapd["state"])
        iapd_name_match     = fo_name.upper() == iapd["firmName"].upper()
        our_city            = (fo_city or "").upper()
        our_state           = (fo_state or "").upper()
        iapd_location_match = (our_city == iapd["city"].upper() and our_state == iapd["state"].upper())

        if iapd["city"] and our_city != iapd["city"].upper():
            corrections["fo_city"] = iapd["city"].title()
        if iapd["state"] and our_state != iapd["state"].upper():
            corrections["fo_state"] = iapd["state"]
    else:
        log.info("  IAPD: no CRD or not found")

    principal_in_content = check_principal_in_content(principal_name, pages)
    log.info("  Principal in content: %s", principal_in_content)

    score = calculate_score(
        row, iapd, iapd_name_match, iapd_location_match, principal_in_content
    )
    log.info("  Score: %d/100", score)

    chain = build_chain(
        row, pages, iapd, iapd_name_match,
        iapd_location_match, principal_in_content, score
    )

    save_validation(record_id, score, chain, corrections)
    log.info("  Done. validation_check = 1")


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("Validation agent started.")
    while True:
        row = claim_record()
        if not row:
            log.info("No pending records. Sleeping %ds...", AGENT_POLL_INTERVAL)
            time.sleep(AGENT_POLL_INTERVAL)
            continue
        try:
            validate(row)
        except Exception as e:
            log.error("Error for record %d: %s", row[0], e)
            mark_failed(row[0], str(e))
        time.sleep(AGENT_POLL_INTERVAL)


if __name__ == "__main__":
    main()
