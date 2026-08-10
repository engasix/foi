import sys
import time
import requests
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import SEC_API_KEY, AGENT_POLL_INTERVAL
from database.connection import get_connection
from logger import get_logger

log = get_logger("discovery")

SEC_API_URL = "https://api.sec-api.io/form-adv/firm"
SEC_QUERIES = [
    'Info.BusNm:"family"',
    'FormInfo.Part1A.Item5C.Q5C2:[1 TO 5] AND FormInfo.Part1A.Item5D.Q5DB1:[1 TO 99999]',
]
PAGE_SIZE       = 50
SKIP_DOMAINS    = ("facebook.com", "youtube.com", "linkedin.com", "twitter.com", "instagram.com", "x.com", "tiktok.com")


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
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'sec-api.io', 1, 1)
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


def run_query(query):
    page     = 0
    inserted = 0
    log.info("Query: %s", query)

    while True:
        payload = {
            "query": query,
            "from":  str(page * PAGE_SIZE),
            "size":  str(PAGE_SIZE),
            "sort":  [{"Info.FirmCrdNb": {"order": "desc"}}]
        }
        try:
            resp = requests.post(
                SEC_API_URL,
                json=payload,
                headers={"Authorization": SEC_API_KEY},
                timeout=20
            )
            resp.raise_for_status()
            data    = resp.json()
            filings = data.get("filings", [])

            if not filings:
                log.info("No more results at page %d.", page)
                break

            for filing in filings:
                info       = filing.get("Info", {})
                addr       = filing.get("MainAddr", {})
                fo_name    = info.get("BusNm", "").strip()
                fo_website = extract_website(filing)

                if not fo_name or not fo_website:
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

            log.info("Page %d done | inserted this query: %d", page, inserted)
            page += 1

            if len(filings) < PAGE_SIZE:
                log.info("Last page reached.")
                break

        except requests.RequestException as e:
            log.error("SEC API error: %s", e)
            break

    return inserted


def run_discovery():
    total = 0
    for query in SEC_QUERIES:
        inserted = run_query(query)
        total   += inserted
        log.info("Query done. Inserted: %d | Grand total: %d", inserted, total)
    log.info("Discovery done. Total inserted: %d", total)


def main():
    log.info("Discovery agent started.")
    run_discovery()
    log.info("Discovery agent finished. Exiting.")


if __name__ == "__main__":
    main()
