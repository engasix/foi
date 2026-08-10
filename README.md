# Family Office Intelligence Pipeline

A multi-agent system that discovers, enriches, validates, and indexes family office data end-to-end, then exposes it through a RAG-powered search interface and a tool-calling research agent.

Built to answer a simple question well: given a messy, unstructured universe of entities (websites, SEC filings, news, LinkedIn), can a set of autonomous agents assemble a clean, trustworthy, queryable dataset with no human in the loop?

---

## Overview

The system runs as a pipeline of independent agents that each own one stage of the data lifecycle - discovery, classification, enrichment, validation, and indexing. Agents claim work from a shared database, process a record, and hand it off. Nothing is orchestrated top-down; each agent polls for records in the state it knows how to handle and moves them forward.

On top of the resulting dataset sits a retrieval-augmented search layer (semantic + structured filtering) and a small tool-using research agent that can answer open-ended questions like "find firms with an active real estate mandate and a recent fund close" by chaining multiple tool calls before composing an answer.

## Architecture

**Pipeline (write path)**

```
discovery → classification → web enrichment → search enrichment
   → LinkedIn enrichment → email enrichment → validation → indexing
```

- Each agent is a standalone process with its own poll loop - no sequential blocking, no central scheduler.
- Records advance through the pipeline via a set of per-stage status columns (`NULL` = not processed, `1` = passed, `0` = failed) rather than a single monolithic status field, so any stage's outcome is independently inspectable.
- `SELECT ... FOR UPDATE` row locking lets multiple agent instances run concurrently against the same table without double-claiming work.
- Browser-based stages (classification, search enrichment) use a real, non-headless Chromium instance rather than headless mode, since many sites specifically block headless browsers. A site being unreachable never drops a record from the pipeline - it's carried forward as "unknown" and later stages (especially search enrichment) get a chance to fill the gap independently.
- LinkedIn enrichment deliberately does not visit linkedin.com at all - anonymous/automated visits get served an authwall regardless of headless/non-headless, and scripted login would violate LinkedIn's Terms of Service. It matches candidates against Serper's own indexed title/snippet instead.
- `signals_agent` and `staleness_agent` exist in `agents/` but are not part of the active pipeline right now (see below).

**Query layer (read path)**

- Records are chunked (identity / investment profile / people & signals) and embedded into a vector store.
- Queries run semantic search with a similarity floor, apply structured filters, and use an LLM as a relevance gate before results are surfaced - vague or empty queries return nothing rather than noisy guesses.
- A FastAPI service exposes `/query`, `/stats`, `/health`, and a small HTML frontend for interactive search.

**Research agent**

- A tool-calling agent sits on top of the query layer with tools for searching, pulling firm detail, comparing shortlisted firms, and searching by recent signals.
- System prompt enforces grounding rules: never fabricate data, always attempt a broader retry before concluding "no results," and explicitly downgrade confidence for stale or thin records.

## Pipeline Agents

| Agent | Responsibility |
|---|---|
| `discovery_agent` / `discovery_json_agent` | Sources candidate records from SEC EDGAR data (live API or cached local JSON); no longer pre-filters on a basic website-alive check |
| `classifier_agent` | Multi-page site scrape (real browser) + LLM classification with cited proof (URL + quoted text). If the site can't be reached, the record still passes through as an unconfirmed ("Unknown") type rather than being dropped |
| `web_enrichment_agent` | Extracts structured detail directly from site content. Runs even when nothing could be scraped, so downstream stages still get a turn |
| `search_enrichment_agent` | Casts a wide net via Serper.dev - multiple query patterns (quoted/unquoted name, field-targeted), up to 3 pages of results per query, real-browser scraping of candidate pages, and an LLM name-attribution check so content from a similarly-named but different entity is never attributed to the wrong record. Fills any of the core fields still missing after web enrichment, including firm type when classification couldn't confirm it |
| `linkedin_enrichment_agent` | Finds corporate and principal LinkedIn profiles via Serper.dev, matched against the indexed page title/snippet - deliberately does not visit linkedin.com directly (anonymous/automated visits hit LinkedIn's authwall, and scripted login would violate its Terms of Service) |
| `email_enrichment_agent` | Resolves principal contact emails via the Hunter.io API |
| `validation_agent` | Cross-checks against a regulatory data source, computes a weighted confidence score - only runs once a record has a confirmed SFO/MFO type |
| `indexer_agent` | Chunks and embeds validated records into the vector store |

`signals_agent` and `staleness_agent` remain in the codebase but are currently disabled - see the pipeline diagram above.

Confidence scoring is a transparent, additive rubric (field completeness, external cross-validation match, contact and social presence) rather than a single opaque model score - every point is traceable to a specific check.

## Tech Stack

- **Language / tooling:** Python 3.12, [`uv`](https://github.com/astral-sh/uv) for dependency and environment management
- **Data:** MySQL/MariaDB (pipeline state), ChromaDB (vector store)
- **AI:** OpenAI `text-embedding-3-small` for embeddings, GPT-4o-mini for extraction/classification/agent reasoning, Anthropic Claude as a coding/reasoning partner during development
- **Scraping / enrichment:** Playwright, Serper.dev, Hunter.io, SEC/regulatory data APIs
- **Service:** FastAPI, Uvicorn, Jinja2

## Project Structure

```
.
├── agents/              # One file per pipeline stage, each independently runnable
├── app/
│   ├── api/              # FastAPI app - query, stats, health, pipeline control endpoints
│   ├── rag/               # Indexing and retrieval logic
│   ├── agent/             # Tool-calling research agent
│   └── frontend/           # Search UI
├── database/             # Connection handling, schema, migrations, backup/restore utilities
├── data/                  # Export utilities
├── config.py              # Environment-driven configuration
└── main.py                # Pipeline entrypoint
```

## Getting Started

```bash
# install dependencies
uv venv
uv pip install -r requirements.txt
uv run playwright install

# configure environment
cp .env.example .env   # then fill in the values below

# set up the database
mysql -u root -p < database/schema.sql

# run the pipeline (agents) and the API separately
uv run main.py
uv run uvicorn app.api.main:app --reload
```

### Environment Variables

```
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=
DB_NAME=family_office_intel

OPENAI_API_KEY=
ANTHROPIC_API_KEY=
SERPER_API_KEY=
HUNTER_API_KEY=
SEC_API_KEY=

AGENT_POLL_INTERVAL=10
MIN_CONFIDENCE_SCORE=40
DISCOVERY_TARGET=75
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /health` | Service health check |
| `GET /query`, `POST /query` | Semantic + structured search over the indexed dataset |
| `POST /agent/run` | Run the tool-calling research agent against a free-form goal |
| `GET /stats` | Pipeline and dataset statistics |
| `POST /pipeline/advance-run` | Advance eligible records into a second processing cycle (staleness re-check) - currently inactive, `staleness_agent` isn't running |
| `GET /` , `GET /agent` | Frontend UI |

## What This Demonstrates

- Designing an autonomous, concurrent multi-agent pipeline without a central orchestrator
- Building a grounded RAG system with relevance gating instead of always-return-something search
- Layering a tool-using agent on top of a retrieval system for open-ended, multi-step research tasks
- Treating data quality as a first-class concern: transparent confidence scoring, cited proof for classification decisions, and a pipeline that degrades gracefully (unreachable sites don't drop records) instead of failing closed
