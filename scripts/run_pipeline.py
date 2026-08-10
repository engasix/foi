"""
Starts every long-running pipeline agent as its own subprocess and streams all of
their logs into one terminal, each agent color-coded and labeled so you can tell
who said what at a glance.

Runs the 7 long-running polling agents in parallel, each in its own subprocess.
Deliberately staggers startup (default 15s between agents) rather than launching
everything at once - the same fix applied in production after all 9 agents booting
simultaneously spiked memory and froze the EC2 box (see dev-logs-stage-2.txt).

discovery_json_agent is intentionally NOT part of this script - it's a one-shot
seed step (reads records.json, inserts new rows, exits), not something that should
fire every time you spin up the pipeline. Run it yourself first, deliberately, then
start this:
    uv run python agents/discovery_json_agent.py
    uv run python scripts/run_pipeline.py

For a small controlled test batch instead of the full local dataset, use
scripts/insert_test_batch.py in place of discovery_json_agent.

NOT included at all here (run separately if you ever need them):
  - discovery_agent: live sec-api.io discovery (discovery_json_agent uses the local
    cached records.json instead)
  - signals_agent / staleness_agent: not part of the active pipeline

Usage:
    uv run python scripts/run_pipeline.py
    uv run python scripts/run_pipeline.py --stagger 20
    uv run python scripts/run_pipeline.py --agents classifier,search_enrichment
"""
import sys
import argparse
import subprocess
import signal
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# name -> (script path, color)
# colors are 256-color ANSI codes, picked to stay readable on light and dark terminals
AGENTS = {
    "classifier":         (ROOT / "agents" / "classifier_agent.py",         51),   # cyan
    "web_enrichment":     (ROOT / "agents" / "web_enrichment_agent.py",     220),  # yellow
    "search_enrichment":  (ROOT / "agents" / "search_enrichment_agent.py",  208),  # orange
    "linkedin_enrichment": (ROOT / "agents" / "linkedin_enrichment_agent.py", 141), # purple
    "email_enrichment":   (ROOT / "agents" / "email_enrichment_agent.py",   84),   # green
    "validation":         (ROOT / "agents" / "validation_agent.py",         39),   # blue
    "indexer":            (ROOT / "agents" / "indexer_agent.py",            213),  # pink
}

DEFAULT_ORDER = [
    "classifier", "web_enrichment", "search_enrichment",
    "linkedin_enrichment", "email_enrichment", "validation", "indexer",
]

RESET = "\033[0m"
BOLD  = "\033[1m"
DIM   = "\033[2m"


def color(code):
    return f"\033[38;5;{code}m"


class AgentProcess:
    def __init__(self, name, script_path, code, label_width):
        self.name        = name
        self.script_path = script_path
        self.code        = code
        self.label_width = label_width
        self.proc        = None

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(self.script_path)],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        label = self.name.ljust(self.label_width)
        prefix = f"{color(self.code)}{BOLD}{label}{RESET}{color(self.code)} │{RESET} "
        try:
            for line in self.proc.stdout:
                print(f"{prefix}{line.rstrip()}")
        except Exception:
            pass
        code = self.proc.wait()
        status = f"{color(196)}{BOLD}exited (code {code}){RESET}" if code != 0 else f"{DIM}exited{RESET}"
        print(f"{prefix}{status}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGINT)
            except Exception:
                pass

    def force_kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None


def print_banner(order, stagger):
    print(f"{BOLD}Family Office Intel Pipeline - agent runner{RESET}")
    print(f"{DIM}Starting {len(order)} agent(s) in parallel, {stagger}s apart, "
          f"real (non-headless) browsers where used.{RESET}")
    for name in order:
        _, code = AGENTS[name]
        print(f"  {color(code)}●{RESET} {name}")
    print(f"{DIM}Ctrl+C to stop everything.{RESET}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stagger", type=float, default=15.0,
                         help="Seconds to wait between starting each agent (default: 15)")
    parser.add_argument("--agents", type=str, default=None,
                         help="Comma-separated subset of agent names to run (default: all)")
    args = parser.parse_args()

    if args.agents:
        order = [a.strip() for a in args.agents.split(",") if a.strip()]
        unknown = [a for a in order if a not in AGENTS]
        if unknown:
            print(f"Unknown agent name(s): {', '.join(unknown)}")
            print(f"Available: {', '.join(AGENTS.keys())}")
            sys.exit(1)
    else:
        order = list(DEFAULT_ORDER)

    label_width = max(len(n) for n in order)
    print_banner(order, args.stagger)

    processes = []
    stop_requested = threading.Event()

    def handle_sigint(signum, frame):
        if stop_requested.is_set():
            print(f"\n{BOLD}Force killing...{RESET}")
            for p in processes:
                p.force_kill()
            sys.exit(1)
        stop_requested.set()
        print(f"\n{BOLD}Stopping all agents (Ctrl+C again to force kill)...{RESET}")
        for p in processes:
            p.stop()

    signal.signal(signal.SIGINT, handle_sigint)

    for i, name in enumerate(order):
        if stop_requested.is_set():
            break
        script_path, code = AGENTS[name]
        agent = AgentProcess(name, script_path, code, label_width)
        agent.start()
        processes.append(agent)
        if i < len(order) - 1:
            time.sleep(args.stagger)

    # wait for all children to exit (either naturally or via stop())
    while any(p.is_running() for p in processes) and not stop_requested.is_set():
        time.sleep(0.5)

    if stop_requested.is_set():
        deadline = time.time() + 10
        while any(p.is_running() for p in processes) and time.time() < deadline:
            time.sleep(0.5)
        for p in processes:
            p.force_kill()

    print(f"\n{BOLD}All agents stopped.{RESET}")


if __name__ == "__main__":
    main()
