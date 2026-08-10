"""
Interactive stats breakdown for validated records at or above a confidence
score you choose.

Usage:
    uv run python scripts/validation_stats.py
"""
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from database.connection import get_connection

# Same 7 fields validation_agent.py's field-completeness score checks
CORE_FIELDS = [
    "fo_type", "fo_city", "fo_state", "fo_aum",
    "fo_background", "fo_investing_thesis", "fo_investing_mandates",
]


def prompt_score():
    while True:
        raw = input("Enter minimum validation score to filter by (0-100): ").strip()
        try:
            score = int(raw)
        except ValueError:
            print("Please enter a whole number.")
            continue
        if 0 <= score <= 100:
            return score
        print("Please enter a number between 0 and 100.")


def count(cur, score, extra_where=""):
    cur.execute(f"""
        SELECT COUNT(*) FROM records
        WHERE validation_check = 1 AND confidence_score >= %s {extra_where}
    """, (score,))
    return cur.fetchone()[0]


def main():
    score = prompt_score()

    conn = get_connection()
    cur  = conn.cursor()

    total = count(cur, score)
    if total == 0:
        print(f"\nNo validated records with a score >= {score}.")
        cur.close()
        conn.close()
        return

    fully_populated = count(cur, score, "AND " + " AND ".join(f"{f} IS NOT NULL" for f in CORE_FIELDS))
    has_email       = count(cur, score, "AND principal_email IS NOT NULL")
    both_linkedin   = count(cur, score, "AND fo_corporate_linkedin IS NOT NULL AND principal_linkedin IS NOT NULL")
    only_corp       = count(cur, score, "AND fo_corporate_linkedin IS NOT NULL AND principal_linkedin IS NULL")
    only_principal  = count(cur, score, "AND fo_corporate_linkedin IS NULL AND principal_linkedin IS NOT NULL")
    no_linkedin     = count(cur, score, "AND fo_corporate_linkedin IS NULL AND principal_linkedin IS NULL")
    sfo             = count(cur, score, "AND fo_type = 'SFO'")
    mfo             = count(cur, score, "AND fo_type = 'MFO'")
    indexed         = count(cur, score, "AND index_check = 1")
    has_signals     = count(cur, score, "AND id IN (SELECT DISTINCT record_id FROM signals)")

    cur.execute("""
        SELECT AVG(confidence_score) FROM records
        WHERE validation_check = 1 AND confidence_score >= %s
    """, (score,))
    avg_score = cur.fetchone()[0]

    cur.close()
    conn.close()

    print()
    print(f"Total records with score >= {score}:       {total}")
    print(f"Fully populated (all core fields filled): {fully_populated}")
    print(f"Has email address:                        {has_email}")
    print(f"Has both corporate + principal LinkedIn:   {both_linkedin}")
    print(f"Has corporate LinkedIn only:                {only_corp}")
    print(f"Has principal LinkedIn only:                 {only_principal}")
    print(f"Has neither LinkedIn:                       {no_linkedin}")
    print(f"Has at least one signal:                   {has_signals}")
    print(f"Currently indexed:                         {indexed}")
    print(f"SFO / MFO split:                           {sfo} / {mfo}")
    print(f"Average score in this set:                 {round(avg_score, 1) if avg_score else 'n/a'}")


if __name__ == "__main__":
    main()
