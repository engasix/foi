"""
Wipes everything so you can start from a completely clean slate:
  1. Truncates all MySQL tables (records, scrape_cache, signals, validation_chain)
  2. Deletes and recreates the ChromaDB collection (app/rag/chroma_db/chroma.sqlite3)
  3. Clears logs/pipeline.log

This is destructive and irreversible - there's no undo built into this script.
If you want a safety net first:
    uv run python database/backup.py

Usage:
    uv run python scripts/full_reset.py          # asks for confirmation
    uv run python scripts/full_reset.py --yes    # skips the confirmation prompt
"""
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from database.truncate import truncate_all
from app.rag.reset_chroma import reset_chromadb

LOG_FILE = ROOT / "logs" / "pipeline.log"


def clear_logs():
    if LOG_FILE.exists():
        LOG_FILE.write_text("")
        print(f"Cleared {LOG_FILE.relative_to(ROOT)}")
    else:
        print(f"No log file found at {LOG_FILE.relative_to(ROOT)} - nothing to clear.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = parser.parse_args()

    print("This will PERMANENTLY delete:")
    print("  - Every row in records, scrape_cache, signals, validation_chain (MySQL)")
    print("  - The entire ChromaDB vector index (app/rag/chroma_db)")
    print("  - All contents of logs/pipeline.log")
    print()
    print("There is no undo. For a safety net first, run: uv run python database/backup.py")
    print()

    if not args.yes:
        confirm = input("Type 'yes' to proceed: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return

    print("\n[1/3] Truncating MySQL tables...")
    truncate_all()

    print("\n[2/3] Resetting ChromaDB...")
    reset_chromadb()

    print("\n[3/3] Clearing logs...")
    clear_logs()

    print("\nDone. Database, vector index, and logs are all empty.")


if __name__ == "__main__":
    main()
