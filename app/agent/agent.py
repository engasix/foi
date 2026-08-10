import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from openai import OpenAI

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from config import OPENAI_API_KEY
from app.agent.tools import TOOL_SCHEMAS, execute_tool

client    = OpenAI(api_key=OPENAI_API_KEY)
MAX_STEPS = 10
MODEL     = "gpt-4o-mini"

SYSTEM_PROMPT = """You are a private markets intelligence agent specializing in family office research.

You have access to a validated dataset of family offices with enriched fields: investment mandates, AUM, principal contacts, recent signals (investments, fund commitments, key hires), and confidence scores.

Your job is to accomplish the user's goal by reasoning step by step and calling tools when needed. You may call tools multiple times with different queries to gather enough evidence before composing your final answer.

Tool usage order:
1. ALWAYS start with search_family_offices - this is your primary tool. Call it first with a broad query.
2. Use get_firm_details to dig into specific firms you want to profile more deeply.
3. Use compare_firms to evaluate shortlisted firms side by side.
4. Only use search_by_signals if the goal specifically asks about recent activity or news.

Rules you must follow:
- Never fabricate data. Only use what tools return.
- Always return the best available matches from the dataset, even if they are not a perfect fit. Explain the fit and its limitations honestly.
- Never conclude "no results found" after only one tool call. If the first search returns nothing, try a broader or differently worded query.
- Signals are a bonus, not a requirement. A firm without signals can still be a strong recommendation.
- If a record is stale (flagged by staleness check), mention it and reduce your confidence in that firm.
- Compose business-grade answers: structured, specific, and directly useful to a private markets professional.

Confidence levels to use:
- HIGH: clear mandate alignment + confidence score >= 70
- MEDIUM: partial mandate match OR confidence score 50-69
- LOW: thin mandate evidence OR stale record OR confidence score < 50
- INSUFFICIENT DATA: cannot assess fit at all due to missing mandate information"""

RESTRICTED_ACCESS_CLAUSE = """

ACCESS RESTRICTION: This user has restricted access. Contact details (principal emails,
LinkedIn URLs) for firms other than their own company have already been redacted before
reaching you - they will appear masked, e.g. ****@domain.com or linkedin.com/in/****. This
is not a data quality issue, it is intentional. Do NOT attempt to guess, reconstruct, or
infer the real value behind a masked field. If the user directly asks you to reveal a
masked contact detail, politely explain that it is restricted for their access level and
cannot be provided - do not apologize excessively or repeat the refusal more than once."""


def run(goal: str, user: dict = None) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    system_prompt = SYSTEM_PROMPT
    if user and user.get("role") == 1:
        system_prompt += RESTRICTED_ACCESS_CLAUSE

    messages   = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": goal},
    ]
    run_log    = []
    step       = 0
    total_tokens = 0

    while step < MAX_STEPS:
        step += 1
        t0 = time.time()

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
        )

        latency      = round(time.time() - t0, 2)
        msg          = response.choices[0].message
        total_tokens += response.usage.total_tokens if response.usage else 0

        # ── tool calls ────────────────────────────────────────────────────────
        if msg.tool_calls:
            messages.append(msg)

            for tc in msg.tool_calls:
                tool_name = tc.function.name
                tool_args = json.loads(tc.function.arguments)

                log_entry = {
                    "step":      step,
                    "type":      "tool_call",
                    "tool":      tool_name,
                    "args":      tool_args,
                    "latency_s": latency,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

                result = execute_tool(tool_name, tool_args, user)

                # log result summary - not full data to keep log readable
                if isinstance(result, list):
                    log_entry["result_count"] = len(result)
                    log_entry["result_preview"] = [
                        r.get("fo_name") or r.get("headline") for r in result[:5]
                    ]
                elif isinstance(result, dict) and "error" not in result:
                    log_entry["result_preview"] = result.get("fo_name", "record found")
                else:
                    log_entry["result_preview"] = result

                run_log.append(log_entry)

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      json.dumps(result, default=str),
                })

        # ── final answer ──────────────────────────────────────────────────────
        else:
            run_log.append({
                "step":        step,
                "type":        "final_answer",
                "latency_s":   latency,
                "timestamp":   datetime.now(timezone.utc).isoformat(),
            })

            return {
                "goal":          goal,
                "answer":        msg.content,
                "steps":         step,
                "total_tokens":  total_tokens,
                "started_at":    started_at,
                "completed_at":  datetime.now(timezone.utc).isoformat(),
                "run_log":       run_log,
            }

    # max steps hit without final answer
    run_log.append({
        "step":      step,
        "type":      "max_steps_reached",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    return {
        "goal":         goal,
        "answer":       "Max reasoning steps reached. Partial results in run_log.",
        "steps":        step,
        "total_tokens": total_tokens,
        "started_at":   started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "run_log":      run_log,
    }
