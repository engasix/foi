"""
reset_chroma.py

Clears all ChromaDB data and resets index_check in MySQL so the
indexer_agent re-indexes everything from scratch on next run.

Run this before starting agents in a fresh production session:
    uv run python app/rag/reset_chroma.py
"""

import sys
import chromadb
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from database.connection import get_connection
from logger import get_logger

log = get_logger("reset_chroma")

CHROMA_PATH     = Path(__file__).parent / "chroma_db"
COLLECTION_NAME = "family_offices"


def reset_chromadb():
    log.info("Connecting to ChromaDB at %s", CHROMA_PATH)
    db = chromadb.PersistentClient(path=str(CHROMA_PATH))

    try:
        col   = db.get_collection(COLLECTION_NAME)
        count = col.count()
        log.info("Collection '%s' found. %d chunks present.", COLLECTION_NAME, count)
        db.delete_collection(COLLECTION_NAME)
        log.info("Collection deleted.")
    except Exception:
        log.info("Collection '%s' does not exist. Nothing to delete.", COLLECTION_NAME)

    db.create_collection(COLLECTION_NAME)
    log.info("Collection '%s' recreated fresh.", COLLECTION_NAME)


def reset_mysql_index():
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("UPDATE records SET index_check = NULL WHERE index_check = 1")
        updated = cur.rowcount
        conn.commit()
        log.info("Reset index_check to NULL on %d records. Indexer will re-index them.", updated)
    except Exception as e:
        conn.rollback()
        log.error("MySQL reset failed: %s", e)
        raise
    finally:
        cur.close()
        conn.close()


def main():
    log.info("=== ChromaDB Reset ===")
    reset_chromadb()
    reset_mysql_index()
    log.info("=== Reset complete. Safe to start all agents. ===")


if __name__ == "__main__":
    main()
