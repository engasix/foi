import sys
import json
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from app.rag.retriever import search as rag_search
from database.connection import get_connection
from app.auth import apply_masking


# ── tool: search_family_offices ───────────────────────────────────────────────

def search_family_offices(query: str = "family office", fo_type: str = None, fo_state: str = None,
                           fo_city: str = None, has_signals: bool = None,
                           min_confidence: int = None, top_k: int = 10, user: dict = None) -> list:
    filters = {}
    if fo_type:        filters["fo_type"]        = fo_type.upper()
    if fo_state:       filters["fo_state"]        = fo_state.upper()
    if fo_city:        filters["fo_city"]         = fo_city
    if has_signals is not None: filters["has_signals"] = has_signals
    if min_confidence: filters["min_confidence"]  = min_confidence

    result = rag_search(query, filters=filters, top_k=top_k)
    firms  = result.get("results", [])

    # return only fields the agent needs - not full doc text
    return [
        apply_masking({
            "fo_name":            f.get("fo_name"),
            "fo_type":            f.get("fo_type"),
            "fo_city":            f.get("fo_city"),
            "fo_state":           f.get("fo_state"),
            "fo_aum":             f.get("fo_aum"),
            "fo_investing_thesis":   _db_field(f.get("fo_name"), "fo_investing_thesis"),
            "fo_investing_mandates": _db_field(f.get("fo_name"), "fo_investing_mandates"),
            "fo_background":      _db_field(f.get("fo_name"), "fo_background"),
            "principal_name":     f.get("principal_name"),
            "principal_title":    f.get("principal_title"),
            "principal_email":    f.get("principal_email"),
            "fo_website":         f.get("fo_website"),
            "confidence_score":   f.get("confidence_score"),
            "has_signals":        f.get("has_signals"),
            "similarity":         f.get("similarity"),
        }, user)
        for f in firms
    ]


# ── tool: get_firm_details ────────────────────────────────────────────────────

def get_firm_details(fo_name: str, user: dict = None) -> dict:
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT
                r.fo_name, r.fo_type, r.fo_website, r.fo_city, r.fo_state, r.fo_country,
                r.fo_aum, r.fo_background, r.fo_investing_thesis, r.fo_investing_mandates,
                r.fo_corporate_linkedin, r.fo_phone,
                r.principal_name, r.principal_title, r.principal_linkedin, r.principal_email,
                r.confidence_score, r.staleness_check, r.staleness_reason,
                r.classifier_reasoning
            FROM records r
            WHERE r.fo_name = %s AND r.validation_check = 1
            LIMIT 1
        """, (fo_name,))
        row = cur.fetchone()
        if not row:
            # fuzzy fallback - try LIKE
            cur.execute("""
                SELECT
                    r.fo_name, r.fo_type, r.fo_website, r.fo_city, r.fo_state, r.fo_country,
                    r.fo_aum, r.fo_background, r.fo_investing_thesis, r.fo_investing_mandates,
                    r.fo_corporate_linkedin, r.fo_phone,
                    r.principal_name, r.principal_title, r.principal_linkedin, r.principal_email,
                    r.confidence_score, r.staleness_check, r.staleness_reason,
                    r.classifier_reasoning
                FROM records r
                WHERE r.fo_name LIKE %s AND r.validation_check = 1
                LIMIT 1
            """, (f"%{fo_name}%",))
            row = cur.fetchone()

        if not row:
            return {"error": f"No validated record found for: {fo_name}"}

        cols = [d[0] for d in cur.description]
        record = dict(zip(cols, row))

        # attach signals
        cur.execute("""
            SELECT signal_type, headline, description, signal_date, source_url
            FROM signals WHERE record_id = (
                SELECT id FROM records WHERE fo_name = %s LIMIT 1
            )
            ORDER BY signal_date DESC LIMIT 10
        """, (record["fo_name"],))
        signals = [dict(zip(["signal_type", "headline", "description", "signal_date", "source_url"], s))
                   for s in cur.fetchall()]
        record["signals"] = signals
        record["is_stale"] = record.get("staleness_check") == 1

        return apply_masking(record, user)
    finally:
        cur.close()
        conn.close()


# ── tool: search_by_signals ───────────────────────────────────────────────────

def search_by_signals(signal_type: str = None, keywords: str = None, top_k: int = 10, user: dict = None) -> list:
    conn = get_connection()
    cur  = conn.cursor()
    try:
        conditions = ["r.validation_check = 1", "r.index_check = 1"]
        params     = []

        if signal_type:
            conditions.append("s.signal_type = %s")
            params.append(signal_type)

        if keywords:
            conditions.append("(s.headline LIKE %s OR s.description LIKE %s)")
            params.extend([f"%{keywords}%", f"%{keywords}%"])

        where = " AND ".join(conditions)

        cur.execute(f"""
            SELECT DISTINCT
                r.fo_name, r.fo_type, r.fo_city, r.fo_state, r.fo_aum, r.fo_website,
                r.fo_investing_thesis, r.fo_investing_mandates,
                r.principal_name, r.principal_email,
                r.confidence_score,
                s.signal_type, s.headline, s.description, s.signal_date
            FROM records r
            JOIN signals s ON s.record_id = r.id
            WHERE {where}
            ORDER BY s.signal_date DESC
            LIMIT %s
        """, (*params, top_k))

        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [apply_masking(dict(zip(cols, row)), user) for row in rows]
    finally:
        cur.close()
        conn.close()


# ── tool: compare_firms ───────────────────────────────────────────────────────

def compare_firms(fo_names: list, user: dict = None) -> list:
    results = []
    for name in fo_names[:5]:  # cap at 5 to control token use
        details = get_firm_details(name, user=user)
        if "error" not in details:
            results.append({
                "fo_name":               details.get("fo_name"),
                "fo_type":               details.get("fo_type"),
                "fo_aum":                details.get("fo_aum"),
                "fo_city":               details.get("fo_city"),
                "fo_state":              details.get("fo_state"),
                "fo_investing_thesis":   details.get("fo_investing_thesis"),
                "fo_investing_mandates": details.get("fo_investing_mandates"),
                "principal_name":        details.get("principal_name"),
                "principal_email":       details.get("principal_email"),
                "confidence_score":      details.get("confidence_score"),
                "is_stale":              details.get("is_stale"),
                "signals_count":         len(details.get("signals", [])),
                "recent_signal":         details["signals"][0]["headline"] if details.get("signals") else None,
            })
    return results


# ── OpenAI tool schemas ───────────────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_family_offices",
            "description": (
                "Semantic search over validated family office records. "
                "Use for broad queries, topic-based searches, or when you need to discover relevant firms. "
                "Supports optional structured filters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query":          {"type": "string", "description": "Natural language search query"},
                    "fo_type":        {"type": "string", "enum": ["SFO", "MFO"], "description": "Filter by office type"},
                    "fo_state":       {"type": "string", "description": "Two-letter US state code"},
                    "fo_city":        {"type": "string", "description": "City name"},
                    "has_signals":    {"type": "boolean", "description": "Only return firms with recent activity signals"},
                    "min_confidence": {"type": "integer", "description": "Minimum confidence score 0-100"},
                    "top_k":          {"type": "integer", "description": "Number of results to return (default 10)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_firm_details",
            "description": (
                "Get full details for a specific family office by name, including all enriched fields and signals. "
                "Use when you need deep information about a specific firm."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fo_name": {"type": "string", "description": "Exact or partial name of the family office"},
                },
                "required": ["fo_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_by_signals",
            "description": (
                "Find family offices that have specific recent activity signals. "
                "Use when the goal involves recent investments, fund commitments, key hires, or news."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "signal_type": {
                        "type": "string",
                        "enum": ["investment", "fund_commitment", "key_hire", "news"],
                        "description": "Type of signal to filter by",
                    },
                    "keywords": {"type": "string", "description": "Keywords to match in signal headline or description"},
                    "top_k":    {"type": "integer", "description": "Number of results to return (default 10)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_firms",
            "description": (
                "Compare multiple family offices side by side. "
                "Use after identifying candidate firms to evaluate fit, mandate alignment, or data quality."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fo_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of family office names to compare (max 5)",
                    },
                },
                "required": ["fo_names"],
            },
        },
    },
]


# ── tool dispatcher ───────────────────────────────────────────────────────────

TOOL_MAP = {
    "search_family_offices": search_family_offices,
    "get_firm_details":      get_firm_details,
    "search_by_signals":     search_by_signals,
    "compare_firms":         compare_firms,
}


def execute_tool(name: str, args: dict, user: dict = None):
    """
    `user` is passed separately from `args` on purpose - args comes from the
    LLM's tool_call arguments, and the LLM must never be able to control or
    override whose access level is being applied.
    """
    fn = TOOL_MAP.get(name)
    if not fn:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(**args, user=user)
    except Exception as e:
        return {"error": str(e)}


# ── internal helper ───────────────────────────────────────────────────────────

def _db_field(fo_name: str, field: str):
    if not fo_name:
        return None
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute(f"SELECT {field} FROM records WHERE fo_name = %s LIMIT 1", (fo_name,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()
        conn.close()
