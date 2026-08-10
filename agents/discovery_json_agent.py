import sys
import json
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import AGENT_POLL_INTERVAL
from database.connection import get_connection
from logger import get_logger

log = get_logger("discovery_json")

JSON_FILE       = Path(__file__).resolve().parent.parent / "database" / "records.json"
SKIP_DOMAINS    = ("facebook.com", "youtube.com", "linkedin.com", "twitter.com",
                   "instagram.com", "x.com", "tiktok.com")


def extract_website(filing):
    try:
        web_addrs = (filing.get("FormInfo", {})
                           .get("Part1A", {})
                           .get("Item1", {})
                           .get("WebAddrs", {})
                           .get("WebAddrs", []))
        for url in web_addrs:
            if url and not any(d in url.lower() for d in SKIP_DOMAINS):
                return url.strip().rstrip("/")
        single = (filing.get("FormInfo", {})
                        .get("Part1A", {})
                        .get("Item1", {})
                        .get("WebAddrs", {})
                        .get("WebAddr", ""))
        if single and not any(d in single.lower() for d in SKIP_DOMAINS):
            return single.strip().rstrip("/")
    except Exception:
        pass
    return None


def insert_record(fo_name, fo_website, city, state, country, phone, crd_number):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT IGNORE INTO records
                (fo_name, fo_website, fo_city, fo_state, fo_country,
                 fo_phone, crd_number, discovery_source, discovery_check, run_cycle)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'local-json', 1, 1)
        """, (fo_name, fo_website, city, state, country, phone, crd_number))
        inserted = cur.rowcount
        conn.commit()
        return inserted
    except Exception as e:
        conn.rollback()
        log.error("Insert failed for %s: %s", fo_name, e)
        return 0
    finally:
        cur.close()
        conn.close()


def run_import():
    if not JSON_FILE.exists():
        log.error("records.json not found at %s", JSON_FILE)
        return

    with open(JSON_FILE) as f:
        data = json.load(f)

    log.info("Loaded %d records from records.json", len(data))

    inserted           = 0
    skipped_no_name    = 0
    skipped_no_website = 0
    skipped_duplicate  = 0

    for i, filing in enumerate(data, 1):
        info       = filing.get("Info", {})
        addr       = filing.get("MainAddr", {})
        fo_name    = info.get("BusNm", "").strip()
        fo_website = extract_website(filing)

        if not fo_name:
            skipped_no_name += 1
            continue

        if not fo_website:
            skipped_no_website += 1
            continue

        result = insert_record(
            fo_name    = fo_name,
            fo_website = fo_website,
            city       = addr.get("City"),
            state      = addr.get("State"),
            country    = addr.get("Cntry"),
            phone      = addr.get("PhNb"),
            crd_number = str(info.get("FirmCrdNb", ""))
        )

        if result:
            inserted += 1
            log.info("+ [%d] %s | %s", inserted, fo_name, fo_website)
        else:
            skipped_duplicate += 1

        if i % 100 == 0:
            log.info("Progress: %d/%d processed | %d inserted so far", i, len(data), inserted)

    log.info("---")
    log.info("Total processed:       %d", len(data))
    log.info("Inserted into DB:      %d", inserted)
    log.info("Skipped (no name):     %d", skipped_no_name)
    log.info("Skipped (no website):  %d", skipped_no_website)
    log.info("Skipped (duplicate):   %d", skipped_duplicate)


def main():
    log.info("Discovery JSON agent started.")
    run_import()
    log.info("Discovery JSON agent finished. Exiting.")


if __name__ == "__main__":
    main()
