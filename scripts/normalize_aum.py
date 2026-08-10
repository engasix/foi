"""
Normalizes the free-text fo_aum column into a consistent "$X.XM" / "$X.XB"
format. The field was populated by LLM extraction over time, so it currently
has 1000+ distinct formats: "$1,019,176,972", "$1.2 billion", "$103M",
"$1343MM", etc.

Deliberately leaves a record untouched (never guesses) when:
  - the value is "$0" (no real magnitude to format)
  - it's a "per client" figure, not total AUM (different metric entirely)
  - it's in a non-USD currency (converting without a real FX rate would
    misrepresent the value)
  - it's literal "null"/"Not specified..." text (should probably become a
    real SQL NULL, not a formatted string - not done here, flagged instead)
  - it's vague prose with no exact figure ("a few hundred million dollars")

Usage:
    uv run python scripts/normalize_aum.py              # dry run, shows the plan
    uv run python scripts/normalize_aum.py --apply       # actually updates the DB
"""
import re
import sys
import argparse
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from database.connection import get_connection

NON_DATA_STRINGS = ("null", "none", "n/a", "not specified in the available data.")


def parse_aum_to_number(raw):
    """Returns (amount_in_dollars, skip_reason). Exactly one is None."""
    if not raw:
        return None, "empty"
    s = raw.strip()

    if "per client" in s.lower():
        return None, "per-client metric, not total AUM"
    if "£" in s or "€" in s:
        return None, "non-USD currency, needs manual conversion"
    if s.lower() in NON_DATA_STRINGS:
        return None, "not real data (should probably be NULL)"
    if re.fullmatch(r"a few hundred million dollars", s, re.IGNORECASE):
        return None, "vague, no exact figure"

    m = re.search(r"\$?\s*([\d,]*\.?\d+)\s*(trillion|tn)\b", s, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", "")) * 1_000_000_000_000, None

    m = re.search(r"\$?\s*([\d,]*\.?\d+)\s*(billion|bn|b)\b", s, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", "")) * 1_000_000_000, None

    m = re.search(r"\$?\s*([\d,]*\.?\d+)\s*mm\b", s, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", "")) * 1_000_000, None

    m = re.search(r"\$?\s*([\d,]*\.?\d+)\s*(million|mn|m)\b", s, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", "")) * 1_000_000, None

    m = re.search(r"\$?\s*([\d,]*\.?\d+)\s*k\b", s, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", "")) * 1_000, None

    m = re.match(r"^\$?\s*([\d,]+\.?\d*)$", s)
    if m:
        try:
            return float(m.group(1).replace(",", "")), None
        except ValueError:
            pass

    return None, "unrecognized format"


def format_num(val):
    rounded = round(val, 1)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}"


def format_aum(amount):
    if amount >= 1_000_000_000:
        return f"${format_num(amount / 1_000_000_000)} B"
    if amount > 0:
        return f"${format_num(amount / 1_000_000)} M"
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually write changes (default is dry-run)")
    args = parser.parse_args()

    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT id, fo_aum FROM records WHERE fo_aum IS NOT NULL AND fo_aum != ''")
    rows = cur.fetchall()

    updates  = []   # (id, new_value)
    skipped  = {}   # reason -> [(id, raw)]
    unchanged = 0

    for record_id, raw in rows:
        amount, reason = parse_aum_to_number(raw)
        if amount is None:
            skipped.setdefault(reason, []).append((record_id, raw))
            continue
        formatted = format_aum(amount)
        if formatted is None:
            skipped.setdefault("zero/negative amount", []).append((record_id, raw))
            continue
        if formatted == raw:
            unchanged += 1
        else:
            updates.append((record_id, formatted))

    total_skipped = sum(len(v) for v in skipped.values())
    print(f"Total fo_aum rows: {len(rows)}")
    print(f"Will update:       {len(updates)}")
    print(f"Already correct:   {unchanged}")
    print(f"Skipped (manual):  {total_skipped}")

    if total_skipped:
        print("\nSkipped, by reason:")
        for reason, items in skipped.items():
            print(f"  {reason}: {len(items)}")
            for record_id, raw in items:
                print(f"    id={record_id}  {raw!r}")

    if not args.apply:
        print("\nDry run only - no changes written. Re-run with --apply to commit.")
        cur.close()
        conn.close()
        return

    for record_id, new_value in updates:
        cur.execute("UPDATE records SET fo_aum = %s WHERE id = %s", (new_value, record_id))
    conn.commit()
    print(f"\nApplied. {len(updates)} rows updated.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
