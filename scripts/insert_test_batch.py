"""
Inserts a small, hand-picked batch of real firms (pulled from database/records.json)
directly into `records`, tagged with discovery_source='manual-test', so you can run
the pipeline agents against a controlled handful of records instead of replaying
discovery against the full 2853-record local dataset.

Picked for geographic/domain variety, not for any known bot-blocking behavior -
this is a general pipeline smoke test batch, not a guaranteed-blocked-site test.

Usage:
    uv run python test_pipeline/insert_test_batch.py            # insert the batch
    uv run python test_pipeline/insert_test_batch.py --reset    # wipe prior test batch, then re-insert
"""
import sys
import argparse
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from database.connection import get_connection

DISCOVERY_SOURCE = "manual-test"

TEST_FIRMS = [
    {
        "fo_name": "NOBLEHOUSE FAMILY OFFICE, LLC",
        "fo_website": "https://noblehousefamilyoffice.com",
        "fo_city": "PLANO", "fo_state": "TX", "fo_country": "United States",
        "fo_phone": "(972) 516-4252", "crd_number": "338855",
    },
    {
        "fo_name": "PULLIAM FAMILY OFFICE, LLC",
        "fo_website": "https://www.pulliamfamilyoffice.com",
        "fo_city": "CRESTED BUTTE", "fo_state": "CO", "fo_country": "United States",
        "fo_phone": "972-947-9400", "crd_number": "338839",
    },
    {
        "fo_name": "XCEPTION FAMILY OFFICE",
        "fo_website": "https://xceptionfamilyoffice.com",
        "fo_city": "ATLANTA", "fo_state": "GA", "fo_country": "United States",
        "fo_phone": "404-524-7900", "crd_number": "338122",
    },
    {
        "fo_name": "CALLAN FAMILY OFFICE, LLC",
        "fo_website": "https://www.callanfamilyoffice.com",
        "fo_city": "RADNOR", "fo_state": "PA", "fo_country": "United States",
        "fo_phone": "(877) 506-7990", "crd_number": "317446",
    },
    {
        "fo_name": "AURELIUS FAMILY OFFICE, LLC",
        "fo_website": "https://www.aurelius.net",
        "fo_city": "BEDFORD", "fo_state": "NH", "fo_country": "United States",
        "fo_phone": "603-413-6060", "crd_number": "323016",
    },
    {
        "fo_name": "TAYLOR FRIGON FAMILY OFFICE, LLC",
        "fo_website": "https://www.tffamilyoffice.com",
        "fo_city": "SCOTTSDALE", "fo_state": "AZ", "fo_country": "United States",
        "fo_phone": "805-226-0280", "crd_number": "323249",
    },
]


def reset_test_batch():
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM records WHERE discovery_source = %s", (DISCOVERY_SOURCE,))
        deleted = cur.rowcount
        conn.commit()
        print(f"Removed {deleted} previous test-batch record(s) (scrape_cache/signals/"
              f"validation_chain cascade-deleted with them).")
    finally:
        cur.close()
        conn.close()


def insert_test_batch():
    conn = get_connection()
    cur = conn.cursor()
    inserted = 0
    skipped = 0
    try:
        for firm in TEST_FIRMS:
            cur.execute("""
                INSERT IGNORE INTO records
                    (fo_name, fo_website, fo_city, fo_state, fo_country,
                     fo_phone, crd_number, discovery_source, discovery_check, run_cycle)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, 1)
            """, (
                firm["fo_name"], firm["fo_website"], firm["fo_city"], firm["fo_state"],
                firm["fo_country"], firm["fo_phone"], firm["crd_number"], DISCOVERY_SOURCE,
            ))
            if cur.rowcount:
                inserted += 1
                print(f"+ {firm['fo_name']} | {firm['fo_website']}")
            else:
                skipped += 1
                print(f"= already present, skipped: {firm['fo_name']}")
        conn.commit()
    finally:
        cur.close()
        conn.close()

    print(f"\nDone. Inserted: {inserted} | Already present: {skipped} | Total in batch: {len(TEST_FIRMS)}")
    print("Now run the agents in order, e.g.:")
    print("  uv run python agents/classifier_agent.py")
    print("  uv run python agents/web_enrichment_agent.py")
    print("  uv run python agents/search_enrichment_agent.py")
    print("  uv run python agents/linkedin_enrichment_agent.py")
    print("  uv run python agents/email_enrichment_agent.py")
    print("  uv run python agents/validation_agent.py")
    print("  uv run python agents/indexer_agent.py")
    print("\nCheck progress any time with, e.g.:")
    print("  SELECT id, fo_name, fo_type, classifier_check, web_enrichment_check,")
    print("         search_enrichment_check, linkedin_enrichment_check,")
    print("         email_enrichment_check, validation_check, index_check, error_message")
    print(f"  FROM records WHERE discovery_source = '{DISCOVERY_SOURCE}';")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Delete the prior test batch before inserting")
    args = parser.parse_args()

    if args.reset:
        reset_test_batch()

    insert_test_batch()
