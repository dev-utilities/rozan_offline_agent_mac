"""Human approval gate for irreversible tools. Non-interactive sessions deny by default.

Two implementations:
  ConsoleApprover  asks on the terminal (CLI).
  AsyncApprover    publishes an approval request (to a websocket, via the server) and blocks the
                   executor thread until another thread answers or the timeout denies.
"""
from __future__ import annotations

import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

Approver = Callable[[str, Dict[str, Any]], bool]

DECISIONS = ("yes", "no", "all")


class ConsoleApprover:
    def __init__(self):
        self._approve_all: set[str] = set()

    def __call__(self, tool: str, args: Dict[str, Any]) -> bool:
        if tool in self._approve_all:
            return True
        if not sys.stdin.isatty():
            print(f"[approval] {tool} requires approval but stdin is not a TTY; denying", file=sys.stderr)
            return False
        print(f"\n[approval needed] {tool}")
        for k, v in args.items():
            s = str(v)
            print(f"   {k}: {s if len(s) < 200 else s[:200] + '...'}")
        while True:
            ans = input("   approve? [y]es / [n]o / [a]ll for this tool this run: ").strip().lower()
            if ans in ("y", "yes"):
                return True
            if ans in ("n", "no", ""):
                return False
            if ans in ("a", "all"):
                self._approve_all.add(tool)
                return True


@dataclass
class PendingApproval:
    id: str
    tool: str
    args: Dict[str, Any]
    created: float
    event: threading.Event = field(default_factory=threading.Event)
    decision: str | None = None
    by: str | None = None


@dataclass
class PendingChoice:
    id: str
    question: str
    options: List[str]
    multiple: bool
    suggested: List[str]
    created: float
    event: threading.Event = field(default_factory=threading.Event)
    selected: Optional[List[str]] = None
    by: str | None = None


MAX_OPTIONS = 100


def clean_options(options: Any) -> List[str]:
    """Options come from untrusted pages: keep short, distinct strings only."""
    out: List[str] = []
    for o in options if isinstance(options, list) else []:
        if isinstance(o, dict):
            o = o.get("label", "")
        o = str(o).strip()[:200]
        if o and o not in out:
            out.append(o)
    return out[:MAX_OPTIONS]


class AsyncApprover:
    """Approval gate for a UI.

    `__call__` runs on the executor's thread. It publishes an `approval.request` event through
    `publish` (which must be safe to call from any thread) and blocks until another thread calls
    `resolve()`, or `timeout_s` passes, in which case the action is denied. A decision of "all"
    approves every later call of the same tool for the lifetime of this approver (one plan run).
    `close()` denies everything still waiting and refuses future requests (used on cancel).
    """

    def __init__(self, publish: Callable[[Dict[str, Any]], None], timeout_s: float = 300.0):
        self.publish = publish
        self.timeout_s = timeout_s
        self._pending: Dict[str, PendingApproval] = {}
        self._choices: Dict[str, PendingChoice] = {}
        self._approve_all: set[str] = set()
        self._lock = threading.Lock()
        self._closed = False

    def ask_always(self, tool: str, args: Dict[str, Any]) -> bool:
        """Ask even if "approve all" was chosen for this tool (the watcher wants a fresh look)."""
        return self._ask(tool, args, allow_all=False)

    def __call__(self, tool: str, args: Dict[str, Any]) -> bool:
        return self._ask(tool, args, allow_all=True)

    def _ask(self, tool: str, args: Dict[str, Any], allow_all: bool) -> bool:
        if allow_all and tool in self._approve_all:
            return True
        with self._lock:
            if self._closed:
                return False
            p = PendingApproval(id=secrets.token_hex(8), tool=tool, args=args, created=time.time())
            self._pending[p.id] = p
        self.publish({"type": "approval.request", "approval_id": p.id, "tool": tool, "args": args,
                      "timeout_s": self.timeout_s})
        answered = p.event.wait(self.timeout_s)
        with self._lock:
            self._pending.pop(p.id, None)
            if not answered and p.decision is None:
                p.decision, p.by = "no", "timeout"
        if p.decision == "all":
            self._approve_all.add(tool)
        self.publish({"type": "approval.resolved", "approval_id": p.id, "tool": tool,
                      "decision": p.decision, "by": p.by})
        return p.decision in ("yes", "all")

    def resolve(self, approval_id: str, decision: str, by: str = "user") -> bool:
        """Answer a pending request. Returns False if it is unknown or already answered."""
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        with self._lock:
            p = self._pending.get(approval_id)
            if p is None or p.decision is not None:
                return False
            p.decision, p.by = decision, by
            p.event.set()
        return True

    # ---- choices: the owner picks from options; nothing picked on timeout or cancel ----------
    def choose(self, question: str, options: List[str], multiple: bool = True,
               suggested: Optional[List[str]] = None) -> List[str]:
        options = clean_options(options)
        suggested = [o for o in clean_options(suggested or []) if o in options]
        if not options:
            return []
        with self._lock:
            if self._closed:
                return []
            c = PendingChoice(id=secrets.token_hex(8), question=str(question)[:300], options=options,
                              multiple=bool(multiple), suggested=suggested, created=time.time())
            self._choices[c.id] = c
        self.publish({"type": "choice.request", "choice_id": c.id, "question": c.question, "options": options,
                      "multiple": c.multiple, "suggested": suggested, "timeout_s": self.timeout_s})
        answered = c.event.wait(self.timeout_s)
        with self._lock:
            self._choices.pop(c.id, None)
            if not answered and c.selected is None:
                c.selected, c.by = [], "timeout"
        self.publish({"type": "choice.resolved", "choice_id": c.id, "selected": c.selected, "by": c.by})
        return list(c.selected or [])

    def resolve_choice(self, choice_id: str, selected: List[str], by: str = "user") -> bool:
        """Answer a pending choice. Only offered options count; a single choice keeps one."""
        with self._lock:
            c = self._choices.get(choice_id)
            if c is None or c.selected is not None:
                return False
            picked = [o for o in c.options if o in set(selected or [])]
            c.selected = picked if c.multiple else picked[:1]
            c.by = by
            c.event.set()
        return True

    def pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = [{"approval_id": p.id, "tool": p.tool, "args": p.args, "created": p.created,
                    "timeout_s": self.timeout_s} for p in self._pending.values()]
            out += [{"choice_id": c.id, "question": c.question, "options": c.options, "multiple": c.multiple,
                     "suggested": c.suggested, "created": c.created, "timeout_s": self.timeout_s}
                    for c in self._choices.values()]
            return out

    def close(self, by: str = "cancel") -> None:
        with self._lock:
            self._closed = True
            ids = list(self._pending)
            cids = list(self._choices)
        for i in ids:
            self.resolve(i, "no", by)
        for i in cids:
            self.resolve_choice(i, [], by)


def deny_all(tool: str, args: Dict[str, Any]) -> bool:
    return False


def allow_all(tool: str, args: Dict[str, Any]) -> bool:
    return True
