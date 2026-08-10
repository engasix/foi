import sys
import logging
import chromadb
from pathlib import Path
from openai import OpenAI
from typing import Optional

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from config import OPENAI_API_KEY

log = logging.getLogger(__name__)

CHROMA_PATH          = Path(__file__).parent / "chroma_db"
COLLECTION_NAME      = "family_offices"
EMBED_MODEL          = "text-embedding-3-small"
DEFAULT_TOP_K        = 10
MAX_TOP_K            = 20
SIMILARITY_THRESHOLD = 10   # discard results below this %
CHUNKS_PER_FIRM      = 3    # _a, _b, _c per record
NO_MATCH_PREFIX      = "No matching family offices found"

client = OpenAI(api_key=OPENAI_API_KEY)

_collection = None


def get_collection():
    global _collection
    if _collection is None:
        db = chromadb.PersistentClient(path=str(CHROMA_PATH))
        _collection = db.get_collection(COLLECTION_NAME)
    return _collection


# ── embedding ─────────────────────────────────────────────────────────────────

def embed_query(text: str) -> list[float]:
    response = client.embeddings.create(model=EMBED_MODEL, input=[text])
    return response.data[0].embedding


# ── filter builder ────────────────────────────────────────────────────────────

def build_where_clause(filters: dict) -> Optional[dict]:
    conditions = []

    if filters.get("fo_type"):
        conditions.append({"fo_type": {"$eq": filters["fo_type"]}})
    if filters.get("fo_state"):
        conditions.append({"fo_state": {"$eq": filters["fo_state"].upper()}})
    if filters.get("fo_city"):
        conditions.append({"fo_city": {"$eq": filters["fo_city"].upper()}})
    if filters.get("fo_country"):
        conditions.append({"fo_country": {"$eq": filters["fo_country"]}})
    if filters.get("has_signals") is not None:
        conditions.append({"has_signals": {"$eq": bool(filters["has_signals"])}})
    if filters.get("min_confidence") is not None:
        conditions.append({"confidence_score": {"$gte": int(filters["min_confidence"])}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


# ── deduplication ─────────────────────────────────────────────────────────────

def deduplicate(raw, top_k: int) -> list[dict]:
    """
    ChromaDB returns chunk-level hits (_a, _b, _c).
    Group by record_id, keep highest similarity, merge documents.
    Return top_k unique firms sorted by best similarity.
    """
    ids       = raw.get("ids", [[]])[0]
    docs      = raw.get("documents", [[]])[0]
    metas     = raw.get("metadatas", [[]])[0]
    distances = raw.get("distances", [[]])[0]

    seen = {}   # record_id -> aggregated entry

    for i, chunk_id in enumerate(ids):
        meta       = (metas[i] or {}) if i < len(metas) else {}
        doc        = docs[i]      if i < len(docs)       else ""
        dist       = distances[i] if i < len(distances)  else 1.0
        similarity = round((1 - dist) * 100, 1)
        record_id  = meta.get("record_id")

        if record_id is None:
            continue

        if record_id not in seen:
            seen[record_id] = {
                "similarity": similarity,
                "meta":       meta,
                "chunks":     [doc],
            }
        else:
            if similarity > seen[record_id]["similarity"]:
                seen[record_id]["similarity"] = similarity
            seen[record_id]["chunks"].append(doc)

    # sort by best similarity, cap at top_k
    sorted_firms = sorted(seen.values(), key=lambda x: x["similarity"], reverse=True)
    return sorted_firms[:top_k]


# ── result formatter ──────────────────────────────────────────────────────────

def format_results(firms: list[dict]) -> list[dict]:
    def clean(val):
        return val if val not in ("not_found", "searching", "", None) else None

    results = []
    for firm in firms:
        meta = firm["meta"]
        results.append({
            "id":                    str(meta.get("record_id", "")),
            "similarity":            firm["similarity"],
            "fo_name":               meta.get("fo_name"),
            "fo_type":               clean(meta.get("fo_type")),
            "fo_city":               clean(meta.get("fo_city")),
            "fo_state":              clean(meta.get("fo_state")),
            "fo_country":            clean(meta.get("fo_country")),
            "fo_aum":                clean(meta.get("fo_aum")),
            "fo_website":            clean(meta.get("fo_website")),
            "fo_corporate_linkedin": clean(meta.get("fo_corporate_linkedin")),
            "fo_phone":              clean(meta.get("fo_phone")),
            "principal_name":        clean(meta.get("principal_name")),
            "principal_title":       clean(meta.get("principal_title")),
            "principal_linkedin":    clean(meta.get("principal_linkedin")),
            "principal_email":       clean(meta.get("principal_email")),
            "confidence_score":      meta.get("confidence_score", 0),
            "has_signals":           meta.get("has_signals", False),
            # all matching chunks joined - richer context for LLM
            "document":              "\n".join(firm["chunks"]),
        })
    return results


# ── retrieval ─────────────────────────────────────────────────────────────────

def search(
    query: str,
    filters: Optional[dict] = None,
    top_k: int = DEFAULT_TOP_K
) -> dict:
    top_k = min(top_k, MAX_TOP_K)

    try:
        collection = get_collection()
    except Exception as e:
        log.error("ChromaDB unavailable: %s", e)
        raise RuntimeError("Vector store unavailable.") from e

    if collection.count() == 0:
        return {"query": query, "filters": filters or {}, "count": 0, "results": []}

    try:
        vector = embed_query(query)
    except Exception as e:
        log.error("Embedding failed: %s", e)
        raise RuntimeError("Query embedding failed.") from e

    where = build_where_clause(filters or {})

    # fetch top_k * CHUNKS_PER_FIRM so dedup yields enough unique firms
    n_fetch = min(top_k * CHUNKS_PER_FIRM, collection.count())

    try:
        kwargs = {
            "query_embeddings": [vector],
            "n_results":        n_fetch,
            "include":          ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        raw = collection.query(**kwargs)
    except Exception as e:
        log.error("ChromaDB query failed: %s", e)
        raise RuntimeError(f"Search failed: {str(e)}") from e

    firms   = deduplicate(raw, top_k)
    results = format_results(firms)

    # drop results below similarity threshold
    results = [r for r in results if r["similarity"] >= SIMILARITY_THRESHOLD]

    return {
        "query":   query,
        "filters": filters or {},
        "count":   len(results),
        "results": results,
    }


# ── grounded answer ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a private markets intelligence assistant specializing in family offices.

Answer the user's query using ONLY the family office records provided below.
Do not add information that is not in the records. If the records do not contain enough information to answer, say so clearly.
Be concise and specific. Reference firm names when citing facts.
If no records are relevant, say: "No matching family offices found for this query." """

RESTRICTED_ACCESS_CLAUSE = """

ACCESS RESTRICTION: This user has restricted access. Contact details (principal emails,
LinkedIn URLs) for firms other than their own company have already been redacted in the
records below - they appear masked, e.g. ****@domain.com or linkedin.com/in/****. This is
intentional, not missing data. Do NOT attempt to guess or reconstruct the real value. If
asked to reveal a masked detail, politely explain it is restricted for their access level."""


def answer(query: str, results: list[dict], user: dict = None) -> str:
    if not results:
        return "No matching family offices found for this query."

    context = "\n\n---\n\n".join(r["document"] for r in results)

    system_prompt = SYSTEM_PROMPT
    if user and user.get("role") == 1:
        system_prompt += RESTRICTED_ACCESS_CLAUSE

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": f"Query: {query}\n\nRecords:\n{context}"}
            ],
            temperature=0,
            max_tokens=600,
        )
        return response.choices[0].message.content
    except Exception as e:
        log.error("LLM answer generation failed: %s", e)
        return "Answer generation failed. Retrieved records are shown below."


# ── convenience wrapper ───────────────────────────────────────────────────────

def query(
    q: str,
    filters: Optional[dict] = None,
    top_k: int = DEFAULT_TOP_K
) -> dict:
    search_result = search(q, filters=filters, top_k=top_k)
    llm_answer    = answer(q, search_result["results"])
    results       = [] if llm_answer.startswith(NO_MATCH_PREFIX) else search_result["results"]

    return {
        "query":   q,
        "filters": search_result["filters"],
        "count":   len(results),
        "answer":  llm_answer,
        "results": results,
    }
