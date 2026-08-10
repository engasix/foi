import sys
import csv
import argparse
from pathlib import Path
from datetime import datetime

sys.path.append(str(Path(__file__).resolve().parent.parent))
from database.connection import get_connection

EXPORTS_DIR = Path(__file__).parent / "exports"

# Column headers in required order matching FOI deliverable spec
HEADERS = [
    # Entity Attributes
    "Family Office Name",
    "Confidence Score",
    "Type (SFO/MFO)",
    "Website",
    "AUM",
    "Background",
    "Investing Thesis",
    "Investing Mandates",
    "Corporate LinkedIn",
    "City",
    "State",
    "Country",
    "Phone",
    # Principal Intelligence
    "Principal Name",
    "Principal Title",
    "Principal LinkedIn",
    "Principal Email",
    # Signals
    "Recent Investments",
    "Recent Fund Commitments",
    "Recent Key Hires",
    "Recent News",
    # Classification Evidence
    "Indexed",
    "classifier reason",
    "classifier proof",
    "proof url",
]


def run(limit=None):
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    now       = datetime.now()
    timestamp = now.strftime("%d-%m-%y ") + now.strftime("%I-%M%p").lstrip("0").lower()
    label     = f"top{limit}" if limit else "all"
    filename  = EXPORTS_DIR / f"foi enriched {label} {timestamp}.csv"

    conn = get_connection()
    cur  = conn.cursor()

    # Load all FO-classified records sorted by confidence score DESC
    query = """
        SELECT
            r.id,
            r.fo_name,
            r.confidence_score,
            r.fo_type,
            r.fo_website,
            r.fo_aum,
            r.fo_background,
            r.fo_investing_thesis,
            r.fo_investing_mandates,
            r.fo_corporate_linkedin,
            r.fo_city,
            r.fo_state,
            r.fo_country,
            r.fo_phone,
            r.principal_name,
            r.principal_title,
            r.principal_linkedin,
            r.principal_email,
            r.principal_email_code,
            r.index_check,
            r.classifier_reasoning,
            r.classifier_proof_text,
            r.classifier_proof_url
        FROM records r
        WHERE r.classifier_check = 1
          AND r.fo_type IN ('SFO', 'MFO')
        ORDER BY r.confidence_score DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"

    cur.execute(query)
    records = cur.fetchall()

    # Load signals grouped by record and type
    cur.execute("""
        SELECT
            record_id,
            signal_type,
            GROUP_CONCAT(headline ORDER BY signal_date DESC SEPARATOR ' | ') AS headlines
        FROM signals
        GROUP BY record_id, signal_type
    """)
    raw_signals = cur.fetchall()

    cur.close()
    conn.close()

    # Build signals lookup: {record_id: {signal_type: headlines}}
    signals_map = {}
    for record_id, signal_type, headlines in raw_signals:
        if record_id not in signals_map:
            signals_map[record_id] = {}
        signals_map[record_id][signal_type] = headlines

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)

        for row in records:
            record_id = row[0]
            sigs      = signals_map.get(record_id, {})

            # Clean sentinel values
            email      = row[17] if row[17] not in (None, "not_found", "searching") else ""
            linkedin_p = row[16] if row[16] not in (None, "not_found", "searching") else ""
            linkedin_c = row[9]  if row[9]  not in (None, "not_found", "searching") else ""

            writer.writerow([
                # Entity Attributes
                row[1],           # fo_name
                f"{row[2]}%" if row[2] is not None else "",  # confidence_score
                row[3],           # fo_type
                row[4],           # fo_website
                row[5],           # fo_aum
                row[6],           # fo_background
                row[7],           # fo_investing_thesis
                row[8],           # fo_investing_mandates
                linkedin_c,       # fo_corporate_linkedin
                row[10],          # fo_city
                row[11],          # fo_state
                row[12],          # fo_country
                row[13],          # fo_phone
                # Principal Intelligence
                row[14],          # principal_name
                row[15],          # principal_title
                linkedin_p,       # principal_linkedin
                email,            # principal_email
                # Signals
                sigs.get("investment", ""),
                sigs.get("fund_commitment", ""),
                sigs.get("key_hire", ""),
                sigs.get("news", ""),
                # Classification Evidence
                "Yes" if row[19] == 1 else "No",   # index_check
                row[20] or "",    # classifier_reasoning
                row[21] or "",    # classifier_proof_text
                row[22] or "",    # classifier_proof_url
            ])

    print(f"Exported {len(records)} records → {filename.name}")
    print(f"Path: {filename}")
    if limit:
        print(f"(top {limit} by confidence score)")
    print()

    has_aum      = sum(1 for r in records if r[5])
    has_email    = sum(1 for r in records if r[17] and r[17] not in ("not_found", "searching"))
    has_linkedin = sum(1 for r in records if r[16] and "linkedin.com/in" in str(r[16]))
    has_signals  = sum(1 for r in records if signals_map.get(r[0]))
    has_indexed  = sum(1 for r in records if r[19] == 1)

    print(f"  Total records:           {len(records)}")
    print(f"  Indexed in ChromaDB:     {has_indexed}")
    print(f"  With AUM:                {has_aum}")
    print(f"  With principal email:    {has_email}")
    print(f"  With principal LinkedIn: {has_linkedin}")
    print(f"  With signals:            {has_signals}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export family office records to CSV")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max records to export (sorted by confidence DESC). Omit for all records.")
    args = parser.parse_args()
    run(limit=args.limit)
