"""
Rebuilds the ChromaDB index around reachability: wipes the current index,
resets index_check to NULL on every record, then indexes ONLY validated
records that have a way to reach the principal (email or LinkedIn) -
regardless of confidence score. Everything else is left index_check=NULL
(never indexed), not 0 (explicitly excluded) - MySQL stays the source of
truth and this can always be re-derived from it.

Reuses the same chunk-building, metadata, and embedding logic as
agents/indexer_agent.py so records in the index look identical either way.

Usage:
    uv run python scripts/reindex_reachable.py           # asks for confirmation
    uv run python scripts/reindex_reachable.py --yes      # skips the prompt
"""
import sys
import argparse
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from database.connection import get_connection
from agents.indexer_agent import get_collection, index_record, CHROMA_PATH, COLLECTION_NAME

RECORD_COLUMNS = [
    "id", "fo_name", "fo_type", "fo_website",
    "fo_city", "fo_state", "fo_country",
    "fo_aum", "fo_background",
    "fo_investing_thesis", "fo_investing_mandates",
    "fo_corporate_linkedin", "fo_phone",
    "principal_name", "principal_title",
    "principal_linkedin", "principal_email",
    "confidence_score", "crd_number",
]


def wipe_chroma():
    import chromadb
    db = chromadb.PersistentClient(path=str(CHROMA_PATH))
    try:
        db.delete_collection(COLLECTION_NAME)
        print(f"Deleted existing '{COLLECTION_NAME}' collection.")
    except Exception:
        print(f"No existing '{COLLECTION_NAME}' collection to delete.")
    return get_collection()


def reset_index_check():
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("UPDATE records SET index_check = NULL")
        reset = cur.rowcount
        conn.commit()
        print(f"Reset index_check to NULL on {reset} records.")
    finally:
        cur.close()
        conn.close()


def get_reachable_records():
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cols = ", ".join(f"r.{c}" for c in RECORD_COLUMNS)
        cur.execute(f"""
            SELECT {cols}
            FROM records r
            WHERE r.validation_check = 1
              AND (r.principal_email IS NOT NULL OR r.principal_linkedin IS NOT NULL)
            ORDER BY r.id
        """)
        rows = cur.fetchall()
        return [dict(zip(RECORD_COLUMNS, row)) for row in rows]
    finally:
        cur.close()
        conn.close()


def get_signals(record_id):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT signal_type, headline, description, signal_date
            FROM signals WHERE record_id = %s
            ORDER BY signal_date DESC
        """, (record_id,))
        return [
            dict(zip(["signal_type", "headline", "description", "signal_date"], s))
            for s in cur.fetchall()
        ]
    finally:
        cur.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = parser.parse_args()

    records = get_reachable_records()
    print(f"This will wipe the current ChromaDB index and rebuild it with only")
    print(f"the {len(records)} validated records that have a principal email or LinkedIn.")
    print("Everything else drops out of the searchable index (MySQL data is untouched).")
    print()

    if not args.yes:
        confirm = input("Type 'yes' to proceed: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return

    collection = wipe_chroma()
    reset_index_check()

    print(f"\nIndexing {len(records)} reachable records...")
    for i, record in enumerate(records, 1):
        signals = get_signals(record["id"])
        try:
            index_record(record, signals, collection)
        except Exception as e:
            print(f"  [{record['id']}] FAILED: {e}")
        if i % 25 == 0 or i == len(records):
            print(f"  {i}/{len(records)} done")

    print(f"\nDone. ChromaDB collection size: {collection.count()} chunks "
          f"({collection.count() // 3} records).")


if __name__ == "__main__":
    main()
