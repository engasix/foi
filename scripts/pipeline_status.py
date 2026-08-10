"""
Quick pipeline status: total records, how many have been processed (reached
validation), how many were ruled out as not a family office (done, excluded -
not left, not left work to do), how many are still actually left to work on,
and how many are indexed.

Usage:
    uv run python scripts/pipeline_status.py
"""
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from database.connection import get_connection


def main():
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM records")
    total = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM records WHERE classifier_check = 0")
    not_fo = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM records WHERE validation_check = 1")
    processed = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM records WHERE index_check = 1")
    indexed = cur.fetchone()[0]

    cur.close()
    conn.close()

    left = total - not_fo - processed

    print(f"Total records:            {total}")
    print(f"Not a family office:      {not_fo}   (excluded, no work left on these)")
    print(f"Processed (validated):    {processed}")
    print(f"Left (still in progress): {left}")
    print(f"Indexed:                  {indexed}")


if __name__ == "__main__":
    main()
