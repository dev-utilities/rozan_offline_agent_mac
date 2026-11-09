"""Plan interpreter. This is where policy is enforced.

For every tool step it:
  1. resolves each argument, collecting the provenance tags of everything referenced,
  2. adds the tags of any `when` condition the step depends on (control-flow taint),
  3. asks the policy whether that union of tags may flow into the tool,
  4. asks the human if the tool requires approval,
  5. runs the tool and tags the result with the tool's own source label.
The planner and reader are never consulted here.
"""
from __future__ import annotations

import json

from typing import Any, Dict, FrozenSet, List, Tuple

from urllib.parse import urlparse

from .approval import Approver
from .domains import KnownDomains
from .audit import Audit
from .planner import ForEachStep, Plan, QuarantineStep, Step, ToolStep
from .policy import Policy, PolicyViolation
from .quarantine import QuarantinedReader, QuarantineError
from .tainted import Tainted, get_path, strip, taint
from .tools import Registry


class ExecutionError(Exception):
    pass


class Executor:
    def __init__(self, policy: Policy, registry: Registry, reader: QuarantinedReader,
                 approver: Approver, audit: Audit, printer=print, allowed_tools=None,
                 domains: KnownDomains | None = None, watcher=None, request: str = ""):
        self.policy = policy
        self.registry = registry
        self.reader = reader
        self.approver = approver
        self.audit = audit
        self.print = printer
        self.allowed_tools = allowed_tools   # task scope; None = no scope restriction
        self.domains = domains if domains is not None else KnownDomains(None)
        self.watcher = watcher           # optional second check on changing actions (agent/watcher.py)
        self.request = request           # the owner's original words, for the watcher
        self._count = 0

    # ---- public -----------------------------------------------------------------------------
    def run(self, plan: Plan) -> Dict[str, Tainted]:
        scope: Dict[str, Tainted] = {}
        self.audit.log("plan.start", goal=plan.goal)
        self._run_steps(plan.steps, scope, control=frozenset())
        self.audit.log("plan.end", goal=plan.goal)
        return scope

    # ---- internals --------------------------------------------------------------------------
    def _tick(self) -> None:
        self._count += 1
        if self._count > self.policy.max_steps:
            raise ExecutionError(f"exceeded max_steps={self.policy.max_steps}")

    def _run_steps(self, steps: List[Step], scope: Dict[str, Tainted], control: FrozenSet[str]) -> None:
        for step in steps:
            if isinstance(step, ForEachStep):
                self._run_foreach(step, scope, control)
                continue
            # evaluate the condition; its taint joins the control taint for this step
            step_control = control
            if step.when is not None:
                ok, cond_sources = self._eval_cond(step.when, scope)
                step_control = control | cond_sources
                if not ok:
                    self.audit.log("step.skip", step=step.id)
                    continue
            if isinstance(step, ToolStep):
                scope[step.id] = self._run_tool(step, scope, step_control)
            elif isinstance(step, QuarantineStep):
                scope[step.id] = self._run_quarantine(step, scope)

    def _run_foreach(self, step: ForEachStep, scope: Dict[str, Tainted], control: FrozenSet[str]) -> None:
        self._tick()
        items = self._resolve(step.foreach, scope)
        if not isinstance(items.value, list):
            raise ExecutionError(f"step {step.id}: foreach target is not a list")
        for i in range(len(items.value)):
            scope[step.item] = get_path(items, [i])
            # Iterating over untrusted data is itself a control dependency: whatever runs inside
            # the loop exists because that data existed. Tag it.
            self._run_steps(step.steps, scope, control | items.sources)
        scope[step.id] = taint(len(items.value), *items.sources)

    def _run_tool(self, step: ToolStep, scope: Dict[str, Tainted], control: FrozenSet[str]) -> Tainted:
        self._tick()
        spec = self.registry.get(step.tool)
        raw_args: Dict[str, Any] = {}
        arg_sources: FrozenSet[str] = frozenset()
        for k, v in step.args.items():
            try:
                t = self._resolve(v, scope)
            except ExecutionError as e:
                # the data this step needs does not exist (e.g. a failed read): skip, don't guess
                self.audit.log("step.skip", step=step.id, tool=step.tool, reason=str(e))
                self.print(f"[executor] skipping {step.id}: {e}")
                return taint({"skipped": True, "reason": str(e)}, "planner")
            raw_args[k] = strip(t)
            arg_sources |= t.sources
        for req in spec.required:
            if req not in raw_args:
                raise ExecutionError(f"step {step.id}: {step.tool} missing required arg {req!r}")

        try:
            if self.allowed_tools is not None and step.tool not in self.allowed_tools:
                raise PolicyViolation(f"{step.tool} is outside what this task was asked to do")
            self.policy.check_flow(step.tool, arg_sources, where="args")
            if control:
                self.policy.check_flow(step.tool, control, where="control flow (when/foreach)")
        except PolicyViolation as e:
            self.audit.log("policy.refuse", step=step.id, tool=step.tool, args=raw_args,
                           arg_sources=sorted(arg_sources), control_sources=sorted(control), reason=str(e))
            self.print(f"[policy] refused step {step.id}: {e}")
            raise

        if step.tool == "ask.choose":
            return self._run_choice(step, raw_args, arg_sources)
        if step.tool == "show":
            raw_args["text"] = self._readable(raw_args.get("text"), arg_sources)

        tp = self.policy.tool(step.tool)
        host = None
        if tp.approval == "new_domain":
            host = (urlparse(str(raw_args.get("url", ""))).hostname or "").lower()
            try:
                self.policy.check_host(host)
            except PolicyViolation as e:
                self.audit.log("policy.refuse", step=step.id, tool=step.tool, args=raw_args, reason=str(e))
                self.print(f"[policy] refused step {step.id}: {e}")
                raise
        needs_ok = tp.approval is True or (tp.approval == "new_domain" and not self.domains.known(host))
        force_ask = False
        if self.watcher is not None and (tp.approval or tp.risk == "high"):
            verdict, why = self.watcher.review(self.request, step.tool, raw_args)
            self.audit.log("watcher", step=step.id, tool=step.tool, verdict=verdict, reason=why)
            if verdict == "stop":
                self.print(f"[watcher] stopped at {step.id}: {why}")
                raise PolicyViolation(f"the watcher stopped {step.tool}: {why}")
            if verdict == "ask":
                needs_ok = force_ask = True
                raw_note = why
        if needs_ok:
            ask_args = dict(raw_args, first_visit_to=host) if host else dict(raw_args)
            if force_ask:
                ask_args["watcher"] = raw_note
            approver = getattr(self.approver, "ask_always", None) if force_ask else None
            approved = (approver or self.approver)(step.tool, ask_args)
            if approved and host:
                self.domains.add(host)
            self.audit.log("approval", step=step.id, tool=step.tool, args=raw_args, approved=approved)
            if not approved:
                self.print(f"[approval] step {step.id} ({step.tool}) denied by user; skipping")
                return taint({"skipped": True}, "planner")

        try:
            result = spec.fn(**raw_args)
        except Exception as e:  # tool failure is data, not a crash of the whole plan
            self.audit.log("tool.error", step=step.id, tool=step.tool, args=raw_args, error=str(e))
            self.print(f"[tool] {step.tool} failed: {e}")
            return taint({"error": str(e)}, spec.source)
        self.audit.log("tool.call", step=step.id, tool=step.tool, args=raw_args,
                       arg_sources=sorted(arg_sources | control), result=result)
        return taint(result, spec.source)

    def _readable(self, value: Any, sources: FrozenSet[str]) -> Any:
        """Long or structured output becomes {answer, details}: a short answer to the owner's request
        written by the reader (no tools, display only), with the raw data kept for "Show details"."""
        if isinstance(value, dict) and isinstance(value.get("summary"), str):
            return {"answer": value["summary"], "details": value}
        text = value if isinstance(value, str) else json.dumps(value, default=str, indent=1)
        if len(text) <= 400 or not self.request:
            return value
        try:
            out = self.reader.ask(f"In 1-3 short sentences, answer the owner's request using only this data. "
                                  f"Request: {self.request[:300]}", Tainted(text, sources or frozenset({"planner"})), "Answer")
            return {"answer": out.value["answer"], "details": value}
        except Exception:
            return value

    def _run_choice(self, step: ToolStep, raw_args: Dict[str, Any], arg_sources: FrozenSet[str]) -> Tainted:
        """The owner picks from options (often read from an untrusted page). What the owner picks is
        the owner's decision, so the result is tagged `user`; options nobody picked never come back."""
        chooser = getattr(self.approver, "choose", None)
        options = raw_args.get("options")
        selected = chooser(str(raw_args.get("question", "")), options, bool(raw_args.get("multiple", True)),
                           raw_args.get("suggested")) if chooser else []
        self.audit.log("choice", step=step.id, question=raw_args.get("question"),
                       options=len(options) if isinstance(options, list) else 0,
                       option_sources=sorted(arg_sources), selected=selected)
        return taint(selected, "user")

    def _run_quarantine(self, step: QuarantineStep, scope: Dict[str, Tainted]) -> Tainted:
        self._tick()
        q = step.quarantine
        content = self._resolve(q["input"], scope)
        try:
            out = self.reader.ask(q["question"], content, q["schema"])
        except QuarantineError as e:
            self.audit.log("quarantine.error", step=step.id, error=str(e))
            self.print(f"[quarantine] {step.id}: {e}")
            # A failed read yields no decision; downstream `when` on it will be a non-bool and fail closed.
            return taint({"error": str(e)}, *content.sources, "quarantine")
        self.audit.log("quarantine", step=step.id, schema=q["schema"], question=q["question"], result=out)
        return out

    def _eval_cond(self, cond: Dict[str, Any], scope: Dict[str, Tainted]) -> Tuple[bool, FrozenSet[str]]:
        negate = False
        if "not" in cond:
            cond = cond["not"]
            negate = True
        try:
            t = self._resolve(cond, scope)
        except ExecutionError as e:
            # e.g. the reader failed and the field does not exist: fail closed, skip the step
            self.print(f"[executor] condition {cond} unavailable ({e}); treating as false")
            root = str(cond.get("$ref", "")).split(".")[0]
            return False, scope[root].sources if root in scope else frozenset()
        if not isinstance(t.value, bool):
            # fail closed: a non-boolean condition never runs the step
            self.print(f"[executor] condition {cond} is not a bool ({type(t.value).__name__}); treating as false")
            return False, t.sources
        return (not t.value) if negate else t.value, t.sources

    def _resolve(self, node: Any, scope: Dict[str, Tainted]) -> Tainted:
        """Turn a plan value into a Tainted. Literals written by the planner are trusted."""
        if isinstance(node, dict) and "$ref" in node:
            path = str(node["$ref"]).split(".")
            root, rest = path[0], path[1:]
            if root not in scope:
                raise ExecutionError(f"$ref {node['$ref']!r}: {root!r} is not defined yet")
            try:
                return get_path(scope[root], rest)
            except KeyError as e:
                raise ExecutionError(f"$ref {node['$ref']!r}: {e}") from e
        if isinstance(node, dict):
            parts = {k: self._resolve(v, scope) for k, v in node.items()}
            srcs = frozenset().union(*(p.sources for p in parts.values())) if parts else frozenset({"planner"})
            return Tainted({k: p.value for k, p in parts.items()}, srcs)
        if isinstance(node, list):
            parts = [self._resolve(v, scope) for v in node]
            srcs = frozenset().union(*(p.sources for p in parts)) if parts else frozenset({"planner"})
            return Tainted([p.value for p in parts], srcs)
        return taint(node, "planner")
