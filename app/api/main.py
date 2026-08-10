import sys
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from config import SESSION_SECRET_KEY
from app.rag.retriever import search, answer, get_collection, NO_MATCH_PREFIX
from app.agent.agent import run as agent_run
from app.auth import (
    authenticate, get_current_user, get_current_user_optional,
    require_admin, apply_masking, mask_document_text,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# ── startup ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up ChromaDB connection on startup
    try:
        col = get_collection()
        log.info("ChromaDB ready. Collection: %d records.", col.count())
    except Exception as e:
        log.error("ChromaDB startup failed: %s", e)
    yield


app = FastAPI(
    title="FOI Family Office Intelligence",
    description="Family office intelligence API - structured and semantic search over family office data.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY)


# ── request / response models ─────────────────────────────────────────────────

class QueryRequest(BaseModel):
    q:              str            = Field(..., min_length=1, max_length=500,
                                          description="Natural language query")
    fo_type:        Optional[str]  = Field(None, description="SFO or MFO")
    fo_state:       Optional[str]  = Field(None, description="Two-letter US state code")
    fo_city:        Optional[str]  = Field(None, description="City name")
    fo_country:     Optional[str]  = Field(None, description="Country name")
    has_signals:    Optional[bool] = Field(None, description="Only firms with recent signals")
    min_confidence: Optional[int]  = Field(None, ge=0, le=100,
                                           description="Minimum confidence score")
    top_k:          int            = Field(10, ge=1, le=20,
                                           description="Number of results to return")


class FirmResult(BaseModel):
    id:                  str
    similarity:          float
    fo_name:             Optional[str]
    fo_type:             Optional[str]
    fo_city:             Optional[str]
    fo_state:            Optional[str]
    fo_country:          Optional[str]
    fo_aum:              Optional[str]
    fo_website:          Optional[str]
    fo_corporate_linkedin: Optional[str]
    fo_phone:            Optional[str]
    principal_name:      Optional[str]
    principal_title:     Optional[str]
    principal_linkedin:  Optional[str]
    principal_email:     Optional[str]
    confidence_score:    int
    has_signals:         bool


class QueryResponse(BaseModel):
    query:   str
    filters: dict
    count:   int
    answer:  str
    results: list[FirmResult]


class LoginRequest(BaseModel):
    email:    str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


# ── helpers ───────────────────────────────────────────────────────────────────

def build_filters(req: QueryRequest) -> dict:
    filters = {}
    if req.fo_type:        filters["fo_type"]        = req.fo_type.upper()
    if req.fo_state:       filters["fo_state"]        = req.fo_state.upper()
    if req.fo_city:        filters["fo_city"]         = req.fo_city.upper()
    if req.fo_country:     filters["fo_country"]      = req.fo_country
    if req.has_signals is not None: filters["has_signals"] = req.has_signals
    if req.min_confidence is not None: filters["min_confidence"] = req.min_confidence
    return filters


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Liveness check."""
    try:
        col   = get_collection()
        count = col.count()
        return {"status": "ok", "indexed_records": count}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Vector store unavailable: {str(e)}")


@app.post("/login")
def login(req: LoginRequest, request: Request):
    """Checks email/password against the users table (plain text - demo phase)."""
    user = authenticate(req.email, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    request.session["user"] = user
    return {"status": "ok", "role": user["role"]}


@app.get("/login")
def login_page(user: dict = Depends(get_current_user_optional)):
    """Serve the login page - bounce to / if already logged in."""
    if user:
        return RedirectResponse("/")
    page = FRONTEND_DIR / "login.html"
    if page.exists():
        return FileResponse(str(page))
    return {"message": "Login page not found."}


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


@app.post("/query", response_model=QueryResponse)
def query_endpoint(req: QueryRequest, user: dict = Depends(get_current_user)):
    """
    Main RAG endpoint. Semantic + structured search with grounded LLM answer.
    Masking is applied to every result - including the raw chunk text used to
    ground the LLM's answer - BEFORE the LLM ever sees any of it, not just on
    the response sent back to the browser.
    """
    filters = build_filters(req)

    try:
        search_result = search(req.q, filters=filters, top_k=req.top_k)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    if search_result["count"] == 0:
        return QueryResponse(
            query=req.q,
            filters=filters,
            count=0,
            answer="No matching family offices found for this query.",
            results=[],
        )

    masked_results = []
    for r in search_result["results"]:
        masked = apply_masking(r, user)
        masked["document"] = mask_document_text(r.get("document", ""), r, user)
        masked_results.append(masked)

    llm_answer = answer(req.q, masked_results, user=user)

    # LLM is the gate - if it found nothing relevant, suppress the cards
    matched = [] if llm_answer.startswith(NO_MATCH_PREFIX) else masked_results

    results = [
        FirmResult(**{k: v for k, v in r.items() if k != "document"})
        for r in matched
    ]

    return QueryResponse(
        query=req.q,
        filters=filters,
        count=len(results),
        answer=llm_answer,
        results=results,
    )


@app.get("/query", response_model=QueryResponse)
def query_get(
    q:              str           = Query(..., min_length=1, max_length=500),
    fo_type:        Optional[str] = Query(None),
    fo_state:       Optional[str] = Query(None),
    fo_city:        Optional[str] = Query(None),
    fo_country:     Optional[str] = Query(None),
    has_signals:    Optional[bool]= Query(None),
    min_confidence: Optional[int] = Query(None, ge=0, le=100),
    top_k:          int           = Query(5, ge=1, le=20),
    user:           dict          = Depends(get_current_user),
):
    """GET version of the query endpoint - useful for direct URL access."""
    req = QueryRequest(
        q=q, fo_type=fo_type, fo_state=fo_state, fo_city=fo_city,
        fo_country=fo_country, has_signals=has_signals,
        min_confidence=min_confidence, top_k=top_k
    )
    return query_endpoint(req, user)


class AgentRequest(BaseModel):
    goal: str = Field(..., min_length=5, max_length=1000, description="Natural language goal for the agent")


@app.post("/agent/run")
def agent_endpoint(req: AgentRequest, user: dict = Depends(get_current_user)):
    """
    Agentic reasoning endpoint. The model decides which tools to call,
    how many times, and when it has enough information to compose an answer.
    Returns the answer, structured firms, and a full run log.
    """
    try:
        result = agent_run(req.goal, user=user)
        return result
    except Exception as e:
        log.error("Agent run failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/pipeline/advance-run")
def advance_run():
    """
    Called by GitHub Actions after 48h to trigger Run 2.
    Flips all validated records to run_cycle=2 so the staleness agent picks them up.
    """
    try:
        from database.connection import get_connection
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE records SET run_cycle = 2
            WHERE validation_check = 1 AND run_cycle = 1
        """)
        advanced = cur.rowcount
        cur.execute("""
            UPDATE records SET staleness_check = NULL
            WHERE validation_check = 1 AND staleness_check IS NOT NULL
        """)
        reset = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        log.info("advance-run: %d records advanced to run_cycle=2, %d staleness checks reset", advanced, reset)
        return {"status": "ok", "records_advanced": advanced, "staleness_reset": reset}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


DOWNLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)


@app.get("/downloads", response_class=HTMLResponse)
def list_downloads(user: dict = Depends(require_admin)):
    """
    File manager UI listing all downloadable files. Admin (role=2) only -
    these are pre-generated CSV exports with full unmasked data, so there's
    no way to mask them per-viewer the way API responses are masked.
    """
    files = []
    for f in sorted(DOWNLOADS_DIR.iterdir()):
        if f.is_file():
            size = f.stat().st_size
            size_str = f"{size / 1_048_576:.1f} MB" if size >= 1_048_576 else f"{size / 1024:.1f} KB"
            files.append((f.name, size_str))

    rows = "".join(
        f'<tr><td><a href="/downloads/{name}" download>{name}</a></td><td>{size}</td></tr>'
        for name, size in files
    ) or "<tr><td colspan='2'>No files available.</td></tr>"

    template = (FRONTEND_DIR / "downloads.html").read_text()
    return HTMLResponse(content=template.replace("{{rows}}", rows))


@app.get("/downloads/{filename}")
def download_file(filename: str, user: dict = Depends(require_admin)):
    """Download a specific file from the downloads folder. Admin (role=2) only."""
    safe_name = Path(filename).name
    file_path = DOWNLOADS_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=str(file_path),
        filename=safe_name,
        media_type="application/octet-stream"
    )


@app.get("/stats")
def stats(user: dict = Depends(get_current_user)):
    """Dataset statistics (aggregate counts only, no per-record PII - no masking needed)."""
    try:
        from database.connection import get_connection
        conn = get_connection()
        cur  = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM records WHERE validation_check = 1")
        total = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM records WHERE index_check = 1")
        indexed = cur.fetchone()[0]

        cur.execute("""
            SELECT fo_type, COUNT(*) FROM records
            WHERE fo_type IS NOT NULL AND validation_check = 1
            GROUP BY fo_type
        """)
        by_type = dict(cur.fetchall())

        cur.execute("""
            SELECT COUNT(*) FROM records
            WHERE principal_email IS NOT NULL AND validation_check = 1
        """)
        with_email = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM records
            WHERE (principal_linkedin IS NOT NULL OR fo_corporate_linkedin IS NOT NULL)
              AND validation_check = 1
        """)
        with_linkedin = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM signals")
        signal_count = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(DISTINCT record_id) FROM signals
        """)
        firms_with_signals = cur.fetchone()[0]

        cur.execute("""
            SELECT AVG(confidence_score) FROM records
            WHERE confidence_score > 0 AND validation_check = 1
        """)
        avg_score = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM records
            WHERE staleness_check = 1
        """)
        stale_count = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM records WHERE run_cycle = 1
        """)
        run1_count = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM records WHERE run_cycle = 2
        """)
        run2_count = cur.fetchone()[0]

        cur.close()
        conn.close()

        return {
            "total_validated":    total,
            "total_indexed":      indexed,
            "by_type":            by_type,
            "with_email":         with_email,
            "with_linkedin":      with_linkedin,
            "total_signals":      signal_count,
            "firms_with_signals": firms_with_signals,
            "avg_confidence":     round(float(avg_score or 0), 1),
            "stale_records":      stale_count,
            "run_cycle_1":        run1_count,
            "run_cycle_2":        run2_count,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── static frontend ───────────────────────────────────────────────────────────

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
FRONTEND_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def root(user: dict = Depends(get_current_user_optional)):
    """Serve the search UI."""
    if not user:
        return RedirectResponse("/login")
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"message": "FOI API running. POST /query to search."}


@app.get("/agent")
def agent_page(user: dict = Depends(get_current_user_optional)):
    """Serve the agent chat UI."""
    if not user:
        return RedirectResponse("/login")
    page = FRONTEND_DIR / "agent.html"
    if page.exists():
        return FileResponse(str(page))
    return {"message": "Agent UI not found."}
