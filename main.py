"""CLI entry point.

  python main.py                      interactive: type a request, review the plan, approve actions
  python main.py --dry-run            plan only, never execute
  python main.py --plan-file p.json   execute a hand-written plan (no planner model needed)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent.approval import ConsoleApprover
from agent.audit import Audit
from agent.executor import Executor
from agent.llm import PLANNER_MODEL, READER_MODEL, OllamaLLM
from agent.planner import Plan, PlanError, Planner, pretty, validate_plan
from agent.policy import Policy, PolicyViolation
from agent.quarantine import QuarantinedReader
from agent.tools import build_registry

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default=str(HERE / "policy.yaml"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--plan-file")
    ap.add_argument("request", nargs="*")
    args = ap.parse_args()

    policy = Policy.load(args.policy)
    registry = build_registry(policy)
    audit = Audit()

    ollama_url = policy.effective_ollama_url()   # refuses a non-local Ollama unless policy allows it
    reader = QuarantinedReader(OllamaLLM(READER_MODEL, url=ollama_url))
    planner = Planner(OllamaLLM(PLANNER_MODEL, url=ollama_url), registry.catalog(policy))
    executor = Executor(policy, registry, reader, ConsoleApprover(), audit)

    def run_plan(plan: Plan) -> None:
        validate_plan(plan, set(registry.names()))
        print("\n=== PLAN ===")
        print(pretty(plan))
        if args.dry_run:
            return
        if sys.stdin.isatty() and input("\nExecute this plan? [y/N] ").strip().lower() not in ("y", "yes"):
            print("cancelled")
            return
        try:
            executor.run(plan)
        except PolicyViolation:
            print("plan stopped at a policy refusal (see audit log)")
        print("done")

    try:
        if args.plan_file:
            with open(args.plan_file, "r", encoding="utf-8") as f:
                run_plan(Plan.model_validate(json.load(f)))
            return 0

        request = " ".join(args.request).strip()
        while True:
            if not request:
                try:
                    request = input("\nyou> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if request in ("", "exit", "quit"):
                    break
            try:
                run_plan(planner.plan(request))
            except PlanError as e:
                print(f"[planner] {e}")
            if args.request:
                break
            request = ""
    finally:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
