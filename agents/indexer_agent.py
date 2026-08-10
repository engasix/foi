import sys
import time
import chromadb
from pathlib import Path
from openai import OpenAI

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import OPENAI_API_KEY, AGENT_POLL_INTERVAL, MIN_CONFIDENCE_SCORE
from database.connection import get_connection
from logger import get_logger

log = get_logger("indexer")

CHROMA_PATH     = Path(__file__).parent.parent / "app" / "rag" / "chroma_db"
COLLECTION_NAME = "family_offices"
EMBED_MODEL     = "text-embedding-3-small"
MIN_CONFIDENCE  = MIN_CONFIDENCE_SCORE

client = OpenAI(api_key=OPENAI_API_KEY)


# ── ChromaDB ──────────────────────────────────────────────────────────────────

def get_collection():
    db = chromadb.PersistentClient(path=str(CHROMA_PATH))
    return db.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"}
    )


# ── database ──────────────────────────────────────────────────────────────────

def claim_for_indexing():
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("START TRANSACTION")
        cur.execute("""
            SELECT
                r.id, r.fo_name, r.fo_type, r.fo_website,
                r.fo_city, r.fo_state, r.fo_country,
                r.fo_aum, r.fo_background,
                r.fo_investing_thesis, r.fo_investing_mandates,
                r.fo_corporate_linkedin, r.fo_phone,
                r.principal_name, r.principal_title,
                r.principal_linkedin, r.principal_email,
                r.confidence_score, r.crd_number
            FROM records r
            WHERE r.validation_check = 1
              AND r.index_check IS NULL
              AND (r.staleness_check IS NULL OR r.staleness_check = 0)
              AND r.confidence_score >= %s
            LIMIT 1 FOR UPDATE
        """, (MIN_CONFIDENCE,))
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None, None
        cols   = [d[0] for d in cur.description]
        record = dict(zip(cols, row))

        cur.execute("""
            SELECT signal_type, headline, description, signal_date
            FROM signals WHERE record_id = %s
            ORDER BY signal_date DESC
        """, (record["id"],))
        signals = [
            dict(zip(["signal_type", "headline", "description", "signal_date"], s))
            for s in cur.fetchall()
        ]

        conn.commit()
        return record, signals
    except Exception as e:
        conn.rollback()
        log.error("Claim (index) failed: %s", e)
        return None, None
    finally:
        cur.close()
        conn.close()


def claim_for_removal():
    """Find one stale record that is still present in ChromaDB."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("START TRANSACTION")
        cur.execute("""
            SELECT id, fo_name FROM records
            WHERE staleness_check = 1 AND index_check = 1
            LIMIT 1 FOR UPDATE
        """)
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None
        conn.commit()
        return row
    except Exception as e:
        conn.rollback()
        log.error("Claim (removal) failed: %s", e)
        return None
    finally:
        cur.close()
        conn.close()


def mark_indexed(record_id):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("UPDATE records SET index_check = 1 WHERE id = %s", (record_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def mark_removed(record_id):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("UPDATE records SET index_check = 0 WHERE id = %s", (record_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def mark_failed(record_id):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("UPDATE records SET index_check = 0 WHERE id = %s", (record_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ── chunk builders ────────────────────────────────────────────────────────────

def build_chunks(record, signals):
    r = record

    fo_type_label = {"SFO": "Single Family Office", "MFO": "Multi Family Office"}.get(
        r["fo_type"] or "", "Family Office"
    )

    # Chunk A: Identity
    a = [f"{r['fo_name']} is a {fo_type_label}."]
    location = ", ".join(filter(None, [r["fo_city"], r["fo_state"], r["fo_country"]]))
    if location:
        a.append(f"Located in {location}.")
    if r["fo_aum"]:
        a.append(f"Assets under management: {r['fo_aum']}.")
    if r["fo_background"]:
        a.append(r["fo_background"])
    if r["fo_website"]:
        a.append(f"Website: {r['fo_website']}.")

    # Chunk B: Investment profile
    b = [f"{r['fo_name']} investment profile."]
    if r["fo_investing_thesis"]:
        b.append(f"Investment thesis: {r['fo_investing_thesis']}.")
    if r["fo_investing_mandates"]:
        b.append(f"Mandates and focus: {r['fo_investing_mandates']}.")
    if not r["fo_investing_thesis"] and not r["fo_investing_mandates"]:
        b.append("Investment focus not publicly disclosed.")

    # Chunk C: People + signals
    c = [f"{r['fo_name']} team and recent activity."]
    if r["principal_name"]:
        person = r["principal_name"]
        if r["principal_title"]:
            person += f", {r['principal_title']}"
        c.append(f"Principal: {person}.")
    if r["principal_email"]:
        c.append(f"Contact email: {r['principal_email']}.")
    if r["principal_linkedin"]:
        c.append(f"Principal LinkedIn: {r['principal_linkedin']}.")
    if r["fo_corporate_linkedin"]:
        c.append(f"Company LinkedIn: {r['fo_corporate_linkedin']}.")
    if signals:
        for s in signals[:5]:
            label = s["signal_type"].replace("_", " ").title()
            c.append(f"{label}: {s['headline']}. {s.get('description', '')}")
    else:
        c.append("No recent public activity found.")

    return [" ".join(a), " ".join(b), " ".join(c)]


def build_metadata(record, signals):
    r = record

    def s(val):
        return str(val) if val is not None else ""

    return {
        "record_id":             r["id"],
        "fo_name":               s(r["fo_name"]),
        "fo_type":               s(r["fo_type"]),
        "fo_city":               s(r["fo_city"]).upper(),
        "fo_state":              s(r["fo_state"]).upper(),
        "fo_country":            s(r["fo_country"]),
        "fo_aum":                s(r["fo_aum"]),
        "fo_website":            s(r["fo_website"]),
        "fo_corporate_linkedin": s(r["fo_corporate_linkedin"]),
        "principal_name":        s(r["principal_name"]),
        "principal_title":       s(r["principal_title"]),
        "principal_linkedin":    s(r["principal_linkedin"]),
        "principal_email":       s(r["principal_email"]),
        "fo_phone":              s(r["fo_phone"]),
        "confidence_score":      r["confidence_score"] or 0,
        "crd_number":            s(r["crd_number"]),
        "has_signals":           len(signals) > 0,
        "has_email":             bool(r["principal_email"]),
        "has_linkedin":          bool(r["principal_linkedin"] or r["fo_corporate_linkedin"]),
    }


# ── embedding ─────────────────────────────────────────────────────────────────

def embed_texts(texts):
    response = client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [item.embedding for item in response.data]


# ── index one record ──────────────────────────────────────────────────────────

def index_record(record, signals, collection):
    record_id = record["id"]
    chunk_ids = [f"{record_id}_a", f"{record_id}_b", f"{record_id}_c"]

    existing = collection.get(ids=[f"{record_id}_a"], include=[])
    if existing["ids"]:
        log.info("  [%d] already in ChromaDB, marking indexed.", record_id)
        mark_indexed(record_id)
        return

    chunks   = build_chunks(record, signals)
    metadata = build_metadata(record, signals)

    vectors = embed_texts(chunks)
    collection.add(
        ids=chunk_ids,
        documents=chunks,
        embeddings=vectors,
        metadatas=[metadata, metadata, metadata]
    )
    mark_indexed(record_id)
    log.info("  [%d] %s - 3 chunks added | signals: %d | confidence: %d",
             record_id, record["fo_name"], len(signals), record["confidence_score"] or 0)


# ── remove one stale record ───────────────────────────────────────────────────

def remove_record(record_id, fo_name, collection):
    chunk_ids = [f"{record_id}_a", f"{record_id}_b", f"{record_id}_c"]
    try:
        collection.delete(ids=chunk_ids)
        mark_removed(record_id)
        log.info("  [%d] %s - removed from ChromaDB (stale)", record_id, fo_name)
    except Exception as e:
        log.error("  [%d] removal failed: %s", record_id, e)


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("Indexer agent started. Min confidence: %d", MIN_CONFIDENCE)
    collection = get_collection()
    log.info("ChromaDB ready. Collection size: %d chunks", collection.count())

    while True:
        # Priority 1: remove stale records from ChromaDB
        stale = claim_for_removal()
        if stale:
            remove_record(stale[0], stale[1], collection)
            time.sleep(AGENT_POLL_INTERVAL)
            continue

        # Priority 2: index new validated records
        record, signals = claim_for_indexing()
        if record:
            try:
                index_record(record, signals, collection)
            except Exception as e:
                log.error("Indexing failed for record %d: %s", record["id"], e)
                mark_failed(record["id"])
            time.sleep(AGENT_POLL_INTERVAL)
            continue

        log.info("Nothing to index or remove. Sleeping %ds...", AGENT_POLL_INTERVAL)
        time.sleep(AGENT_POLL_INTERVAL)


if __name__ == "__main__":
    main()
