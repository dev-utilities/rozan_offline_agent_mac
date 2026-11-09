"""Local HTTP/WebSocket service around the core, for the native shell and the web UI.

  python -m agent.server            real models + tools (needs Ollama; Gmail/browser on first use)
  python -m agent.server --fake     scripted models + fake tools, for UI work and demos
  python -m agent.server --open     also print/open the UI URL with the token in the fragment

Loopback only. Every API call needs the bearer token the server writes to
~/.personal_agent/session.token (mode 600) at startup; the static UI itself needs none. The port
and pid are written next to it in session.json so a shell can find the server.

  POST /chat                  {message, conversation_id?}  -> turn with a validated plan (or an error)
  POST /plan/{id}/run         start executing (202); one run at a time
  WS   /events                first message {token}; then audit records, show output, approval
                              requests and `state` changes (idle/planning/running/needs_approval/error/paused)
  POST /approval/{id}         {decision: yes|no|all}
  POST /run/{id}/cancel       stop at the next step; pending approval is denied
  GET  /run/{id}              status + buffered events (resync after a reconnect)
  POST /pause, POST /resume   pause refuses new plans/runs and cancels the current run
  POST /state/ack             clear the sticky error state (the shell calls it when the modal opens)
  GET  /audit?limit=&run=     tail of the audit log
  GET  /health                unauthenticated liveness; GET /status has the details
  GET  /settings              read-only view of policy.yaml, external services and model names
  GET  /permissions           macOS permission status with plain explanations; POST /permissions lets the
                              shell report what it knows; POST /permissions/probe checks folder access
  GET/POST/DELETE /conversations[/{id}]   persisted under ~/.personal_agent/conversations/
  GET  /examples, POST /examples/{name}/load

Nothing here changes the security model: the same Executor/Policy run every plan, on one worker
thread, and the planner still sees only the user's request. Stored conversations are never fed
back to the planner.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import re
import secrets
import signal
import socket
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .approval import AsyncApprover
from .audit import AUDIT_PATH, Audit, verify as verify_audit
from .bridge import get_bridge
from .domains import KnownDomains
from .broker import SECRETS_PATH
from .executor import ExecutionError, Executor
from .llm import LLM, PLANNER_MODEL, READER_MODEL, OllamaLLM
from .models import LowMemory, ModelManager, StrongUnavailable, strong_from_config
from .watcher import Watcher
from .vault import Vault, VaultError
from .scope import Scoper, ScopeError, TaskScope, out_of_scope, plan_tools
from .planner import Plan, PlanError, Planner, history_block, validate_plan
from .policy import Policy, PolicyViolation
from .quarantine import QuarantinedReader
from .tools import Registry, build_registry

VERSION = "0.2.0"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
UI_DIR = ROOT / "ui"
EXAMPLES_DIR = ROOT / "examples"
HOME = Path(os.environ.get("AGENT_HOME", "~/.personal_agent")).expanduser()
TOKEN_PATH = HOME / "session.token"
SESSION_PATH = HOME / "session.json"
CONVERSATIONS_DIR = HOME / "conversations"
APPROVAL_TIMEOUT_S = float(os.environ.get("AGENT_APPROVAL_TIMEOUT", "300"))
EVENT_BUFFER = 1000
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
EXAMPLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")
STATES = ("idle", "planning", "running", "needs_approval", "error", "paused")
PERMISSION_STATUSES = ("granted", "denied", "not_requested", "unknown")

# macOS permissions the product needs, each with the plain explanation the UI shows when asking.
# `source` says who can know the status: the core probes its own file access; the shell reports the rest.
PERMISSIONS: List[dict] = [
    {"name": "full_disk_access", "title": "Full Disk Access", "source": "core",
     "why": "Lets the agent read Mail, Messages, Safari data and other folders macOS protects. Only needed "
            "if you widen the readable folders to include them.",
     "how": "System Settings → Privacy & Security → Full Disk Access → add Rozan."},
    {"name": "accessibility", "title": "Accessibility", "source": "shell",
     "why": "Needed only for a global keyboard shortcut that toggles the bubble.",
     "how": "System Settings → Privacy & Security → Accessibility."},
    {"name": "screen_recording", "title": "Screen Recording", "source": "shell",
     "why": "Needed only if a screenshot tool is enabled. Its output is untrusted like any other content.",
     "how": "System Settings → Privacy & Security → Screen & System Audio Recording."},
    {"name": "automation", "title": "Automation", "source": "shell",
     "why": "Lets the agent drive other apps (Mail, Finder) through AppleScript. Not used yet.",
     "how": "macOS asks the first time an app is scripted; manage under Privacy & Security → Automation."},
    {"name": "microphone", "title": "Microphone", "source": "shell",
     "why": "Used only while you dictate a request (⌃⌥D). Audio is turned into text on this Mac.",
     "how": "macOS asks the first time you dictate; manage under Privacy & Security → Microphone."},
    {"name": "speech_recognition", "title": "Speech Recognition", "source": "shell",
     "why": "Turns dictation into text with Apple's on-device recognizer only; nothing is sent to Apple.",
     "how": "macOS asks the first time you dictate; manage under Privacy & Security → Speech Recognition."},
    {"name": "notifications", "title": "Notifications", "source": "shell",
     "why": "Tells you when an approval is waiting and the bubble is hidden or on another display.",
     "how": "System Settings → Notifications → Rozan."},
]


# ---- state ------------------------------------------------------------------------------------
@dataclass
class Turn:
    id: str
    kind: str                       # "chat" | "example"
    request: str
    created: float
    plan_id: Optional[str] = None
    plan: Optional[dict] = None
    error: Optional[str] = None
    run_ids: List[str] = field(default_factory=list)
    scope: Optional[List[str]] = None          # capabilities this task may use (None = not scoped)
    scope_text: Optional[List[str]] = None     # the same, in plain words, for the plan card
    open_ended: bool = False
    planned_by: Optional[str] = None           # model that wrote the plan
    escalated: List[str] = field(default_factory=list)   # why the careful model was used
    careful_reader: bool = False               # read untrusted content with the careful model at run time
    notes: List[str] = field(default_factory=list)
    answer: Optional[str] = None               # a direct reply written by the planner (no tools, nothing to run)


CHAT_MODES = ("normal", "isolated", "incognito")


@dataclass
class Conversation:
    id: str
    created: float
    updated: float = 0.0
    turns: List[Turn] = field(default_factory=list)
    mode: str = "normal"       # normal: uses memory · isolated: no memory · incognito: no memory, never saved,
                               # permanently deleted after inactivity

    def title(self) -> str:
        for t in self.turns:
            if t.request:
                return t.request[:60]
        return "New chat"


@dataclass
class Run:
    id: str
    plan_id: str
    conversation_id: str
    goal: str
    started: float
    status: str = "running"         # running | done | refused | error | cancelled
    ended: Optional[float] = None
    error: Optional[str] = None
    events: List[dict] = field(default_factory=list)
    approver: Optional[AsyncApprover] = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def pending(self) -> List[dict]:
        return self.approver.pending() if (self.approver and self.status == "running") else []

    def summary(self) -> dict:
        return {
            "id": self.id, "plan_id": self.plan_id, "conversation_id": self.conversation_id,
            "goal": self.goal, "status": self.status, "started": self.started, "ended": self.ended,
            "error": self.error, "pending_approvals": self.pending(),
        }

    def to_dict(self) -> dict:
        d = self.summary()
        d.pop("pending_approvals")
        d["events"] = list(self.events)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Run":
        run = cls(id=d["id"], plan_id=d["plan_id"], conversation_id=d["conversation_id"], goal=d.get("goal", ""),
                  started=d.get("started", 0.0), status=d.get("status", "error"), ended=d.get("ended"),
                  error=d.get("error"), events=list(d.get("events") or []))
        if run.status == "running":  # the core died mid-run
            run.status, run.error = "error", "the core restarted during this run"
            run.ended = run.ended or time.time()
        return run


class ConversationStore:
    """One JSON file per conversation under `directory` (mode 600, written atomically).

    Files hold the user's requests, the plans, and the run events, which include untrusted text
    (email subjects, reader verdicts, `show` output). They are read back only to display them;
    `AgentService.plan_request` passes nothing but the new request to the planner.
    """

    def __init__(self, directory: Optional[Path]):
        self.dir = directory
        self._lock = threading.Lock()
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)

    def path(self, cid: str) -> Path:
        return self.dir / f"{cid}.json"

    def save(self, conv: Conversation, runs: Dict[str, Run]) -> None:
        if self.dir is None or conv.mode == "incognito":
            return
        data = {
            "version": 1, "id": conv.id, "created": conv.created, "updated": conv.updated, "title": conv.title(),
            "mode": conv.mode,
            "turns": [asdict(t) for t in conv.turns],
            "runs": {rid: runs[rid].to_dict() for t in conv.turns for rid in t.run_ids if rid in runs},
        }
        text = json.dumps(data, default=str)
        with self._lock:
            tmp = self.path(conv.id).with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path(conv.id))

    def delete(self, cid: str) -> None:
        if self.dir is None:
            return
        with self._lock:
            try:
                self.path(cid).unlink()
            except FileNotFoundError:
                pass

    def load_all(self) -> tuple[Dict[str, Conversation], Dict[str, Run]]:
        convs: Dict[str, Conversation] = {}
        runs: Dict[str, Run] = {}
        if self.dir is None:
            return convs, runs
        for p in sorted(self.dir.glob("*.json")):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                conv = Conversation(id=d["id"], created=d.get("created", 0.0), updated=d.get("updated", 0.0),
                                    turns=[Turn(**t) for t in d.get("turns", [])], mode=d.get("mode", "normal"))
                for rid, rd in (d.get("runs") or {}).items():
                    runs[rid] = Run.from_dict(rd)
                convs[conv.id] = conv
            except Exception:
                continue  # a damaged file is skipped, not fatal
        return convs, runs


class _CancellableExecutor(Executor):
    """Executor that stops at the next step once `cancel` is set."""

    def __init__(self, *args, cancel: threading.Event, **kwargs):
        super().__init__(*args, **kwargs)
        self._cancel = cancel

    def _tick(self) -> None:
        if self._cancel.is_set():
            raise ExecutionError("cancelled by user")
        super()._tick()


class AgentService:
    """Owns the registry, the models, conversations, and the single execution worker thread.

    All plan execution happens on one dedicated thread (tools like the Playwright browser are
    thread-bound). Events are published from that thread and fanned out on the asyncio loop.
    """

    def __init__(self, policy: Policy, registry_factory: Callable[..., Registry], planner_llm: LLM,
                 reader_llm: LLM, audit_path: Path = AUDIT_PATH, *, store_dir: Optional[Path] = None,
                 fake: bool = False, approval_timeout_s: float = APPROVAL_TIMEOUT_S,
                 planner_model: str = PLANNER_MODEL, reader_model: str = READER_MODEL,
                 scoper: Optional[Scoper] = None, models: Optional[ModelManager] = None):
        self.policy = policy
        self.registry = registry_factory(printer=self._log, show=self._show)
        self.catalog = self.registry.catalog(policy)
        self.planner = Planner(planner_llm, self.catalog)
        self.scoper = scoper
        self.models = models
        self.reader = QuarantinedReader(reader_llm)
        self.audit_path = Path(audit_path)
        self.fake = fake
        self.permission_needed: Optional[str] = None
        self.standby_info: Optional[dict] = None           # what the watchdog saw when it put Rozan on standby
        from .memory import top_apps as _top_apps
        self.top_apps_reader = _top_apps
        self.browser_ready: Callable[[], bool] = lambda: True    # the real service checks the Chrome bridge
        self.approval_timeout_s = approval_timeout_s
        self.model_names = {"planner": planner_model, "reader": reader_model}
        self.started = time.time()
        self.store = ConversationStore(store_dir)
        self.conversations, self.runs = self.store.load_all()
        self.plans: Dict[str, tuple[Plan, str]] = {}      # plan_id -> (plan, conversation_id); rebuilt from store
        self.plan_meta: Dict[str, dict] = {}              # plan_id -> {allowed: set|None, careful: bool}
        for conv in self.conversations.values():
            for t in conv.turns:
                if t.plan_id and t.plan:
                    try:
                        self.plans[t.plan_id] = (Plan.model_validate(t.plan), conv.id)
                        allowed = TaskScope(capabilities=t.scope).allowed_tools() if t.scope is not None else None
                        self.plan_meta[t.plan_id] = {"allowed": allowed, "careful": t.careful_reader, "request": t.request}
                    except Exception:
                        pass
        self.domains = KnownDomains(HOME / "known_domains.json" if store_dir is not None else None)
        self.current_run: Optional[Run] = None
        self.paused = False
        self.watcher_enabled = bool((policy.models or {}).get("watcher", False))
        mem = policy.memory or {}
        self.vault = Vault(HOME / "memory.enc" if store_dir is not None else None,
                           idle_s=float(mem.get("auto_lock_minutes", 15)) * 60)
        self.incognito_ttl_s = float(mem.get("incognito_delete_after_minutes", 60)) * 60
        self.standby_reason: Optional[str] = None
        self.memory_reader: Callable[[], dict] = _memory_snapshot
        self.last_memory: Optional[dict] = None
        self.last_error: Optional[str] = None
        self.permission_reports: Dict[str, dict] = {}
        self._folder_probe: Dict[str, str] = {}
        self._planning = 0
        self._last_state: Optional[tuple] = None
        self._run_lock = threading.Lock()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="executor")
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: set[asyncio.Queue] = set()

    # ---- events ---------------------------------------------------------------------------------
    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=EVENT_BUFFER)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict) -> None:
        """Thread-safe: called from the executor thread and from request handlers."""
        event = dict(event)
        event.setdefault("ts", time.time())
        rec = event.get("record") if event.get("type") == "audit" else None
        if isinstance(rec, dict) and rec.get("event") == "tool.error":
            self.note_permission_error(str(rec.get("error", "")))
        run = self.current_run
        if run is not None:
            event.setdefault("run_id", run.id)
            if event["run_id"] == run.id and event["type"] != "state":
                run.events.append(event)
                if len(run.events) > EVENT_BUFFER:
                    del run.events[: len(run.events) - EVENT_BUFFER]
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._fanout, event)
        if event["type"].startswith(("approval.", "choice.")):
            self._emit_state()

    def _fanout(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow client; it can resync via GET /run/{id}

    def _log(self, msg: Any) -> None:
        self.publish({"type": "log", "text": str(msg)})

    def _show(self, value: Any) -> None:
        self.publish({"type": "show", "value": value})

    # ---- state (what the bubble shows) -----------------------------------------------------------
    def state(self) -> str:
        if self.paused:
            return "paused"
        run = self.current_run
        if run is not None:
            return "needs_approval" if run.pending() else "running"
        if self._planning > 0:
            return "planning"
        if self.last_error:
            return "error"
        return "idle"

    def state_view(self) -> dict:
        run = self.current_run
        return {"state": self.state(), "paused": self.paused, "standby_reason": self.standby_reason,
                "watcher": self.watcher_enabled,
                "memory": self.last_memory, "last_error": self.last_error,
                "pending_approvals": len(run.pending()) if run else 0, "run_id": run.id if run else None}

    def _emit_state(self) -> None:
        view = self.state_view()
        key = (view["state"], view["pending_approvals"], view["last_error"], view["standby_reason"], view["watcher"])
        if key != self._last_state:
            self._last_state = key
            self.publish({"type": "state", **view})

    def ack_error(self) -> None:
        self.last_error = None
        self._emit_state()

    def pause(self, reason: str = "Paused by you") -> None:
        """Standby: stop the current task, free the models' memory, and refuse new work until resumed."""
        self.paused = True
        self.standby_reason = reason
        run = self.current_run
        if run is not None:
            self.cancel_run(run.id)
        self._emit_state()
        threading.Thread(target=self._free_models, daemon=True, name="standby-unload").start()

    def _free_models(self) -> None:
        if self.models is None:
            return
        try:
            self.models.unload_fast()
        except Exception:
            pass
        backend = self.models.strong_backend
        if backend is not None:
            try:
                backend.stop()
            except Exception:
                pass

    def resume(self) -> None:
        self.paused = False
        self.standby_reason = None
        self._emit_state()

    def housekeeping(self) -> None:
        """Watchdog tick: auto-lock memory, and permanently drop idle incognito chats."""
        self.vault.expire()
        now = time.time()
        for c in list(self.conversations.values()):
            if c.mode == "incognito" and now - (c.updated or c.created) > self.incognito_ttl_s:
                run = self.current_run
                if run is None or run.conversation_id != c.id:
                    self.delete_conversation(c.id)

    def check_memory(self) -> Optional[str]:
        """Watchdog tick. Puts the agent on standby when memory is nearly exhausted; returns the reason."""
        cfg = self.policy.memory or {}
        limit = float(cfg.get("standby_at_percent", 92))
        min_swap = float(cfg.get("min_swap_free_gb", 0.5))
        try:
            snap = self.memory_reader()
        except Exception:
            return None
        self.last_memory = snap
        if self.paused:
            return None
        reason = None
        if snap["used_percent"] >= limit:
            reason = f"Memory at {snap['used_percent']:.0f}% (limit {limit:.0f}%)"
        elif snap.get("pressure", 1) >= 4:
            reason = "macOS reports critical memory pressure"
        elif snap["swap_free_gb"] < min_swap:
            reason = f"No room left to swap ({snap['swap_free_gb']:.1f} GB of disk free for it)"
        if reason:
            # Snapshot the biggest apps now, before standby unloads the model, so the owner sees what caused it
            # (including Rozan itself).
            try:
                apps = self.top_apps_reader()
            except Exception:
                apps = []
            listed = ", ".join(f"{a['name']} {a['memory_gb']:.1f} GB" for a in apps)
            self.standby_info = {"ts": time.time(), "reason": reason, "used_percent": snap.get("used_percent"),
                                 "apps": apps}
            self.pause(f"Auto-standby: {reason}." + (f" Using the most memory: {listed}." if listed else "")
                       + " Close some apps, then press Resume.")
        return reason

    # ---- conversations --------------------------------------------------------------------------
    def _save(self, conv: Conversation) -> None:
        conv.updated = time.time()
        self.store.save(conv, self.runs)

    def new_conversation(self, mode: str = "normal") -> Conversation:
        if mode not in CHAT_MODES:
            raise ValueError(f"mode must be one of {CHAT_MODES}")
        c = Conversation(id=uuid.uuid4().hex[:12], created=time.time(), updated=time.time(), mode=mode)
        self.conversations[c.id] = c
        self._save(c)
        return c

    def conversation(self, cid: Optional[str]) -> Conversation:
        if cid is None:
            return self.new_conversation()
        if cid not in self.conversations:
            raise KeyError(cid)
        return self.conversations[cid]

    def delete_conversation(self, cid: str) -> None:
        conv = self.conversations[cid]
        run = self.current_run
        if run is not None and run.conversation_id == cid:
            raise RuntimeError("a plan from this conversation is running")
        for t in conv.turns:
            for rid in t.run_ids:
                self.runs.pop(rid, None)
            if t.plan_id:
                self.plans.pop(t.plan_id, None)
        del self.conversations[cid]
        self.store.delete(cid)

    def conversation_list(self) -> List[dict]:
        return [{"id": c.id, "created": c.created, "updated": c.updated, "title": c.title(), "turns": len(c.turns),
                 "mode": c.mode}
                for c in sorted(self.conversations.values(), key=lambda c: c.updated or c.created, reverse=True)]

    def turn_view(self, t: Turn) -> dict:
        d = asdict(t)
        if not t.answer and t.plan and not t.run_ids:
            # older chats: a plan that only shows the planner's own text reads as a reply
            try:
                d["answer"] = direct_answer(Plan.model_validate(t.plan))
            except Exception:
                pass
        if t.plan_id in self.plans:
            missing = self.missing_tools(self.plans[t.plan_id][0])
            d["unavailable"] = unavailable_message(missing) if missing else None
            d["auto_run"] = not missing and self.auto_runnable(self.plans[t.plan_id][0])
        return d

    def note_permission_error(self, error: str) -> None:
        """A tool said macOS permission is missing: the app reads this from /status and shows the prompt."""
        if "Accessibility permission" in (error or ""):
            self.permission_needed = "accessibility"

    def auto_runnable(self, plan: Plan) -> bool:
        """Plans that only read, look things up or show results run without an Execute press. Anything
        that needs approval (shell, send, click, type, trash, write) or is high-risk waits for the owner;
        and inside a run each such step still asks again, showing its exact arguments."""
        for name in plan_tools(plan):
            tp = self.policy.tools.get(name)
            if tp is None or tp.risk == "high" or tp.approval not in (False, "new_domain"):
                return False
        return True

    def conversation_view(self, c: Conversation) -> dict:
        turns = []
        for t in c.turns:
            d = self.turn_view(t)
            d["runs"] = [self.runs[r].summary() | {"events": list(self.runs[r].events)} for r in t.run_ids if r in self.runs]
            turns.append(d)
        return {"id": c.id, "created": c.created, "updated": c.updated, "title": c.title(), "turns": turns,
                "mode": c.mode}

    # ---- planning ---------------------------------------------------------------------------------
    def _model_view(self) -> dict:
        out = dict(self.model_names)
        if self.models is not None:
            out["careful"] = self.models.strong_name
        return out

    def _register_plan(self, turn: Turn, plan: Plan, conv: Conversation,
                       allowed: Optional[frozenset] = None) -> None:
        validate_plan(plan, set(self.registry.names()))
        turn.plan_id = uuid.uuid4().hex[:12]
        turn.plan = plan.model_dump(by_alias=True, exclude_none=True)
        self.plans[turn.plan_id] = (plan, conv.id)
        self.plan_meta[turn.plan_id] = {"allowed": allowed, "careful": turn.careful_reader, "request": turn.request}

    def _show_real_results(self, plan: Plan) -> None:
        """Small models like to end with a fixed "Done, X was added!" message, which would claim success even
        if the step failed. When a top-level show of literal text follows a tool that returns its own
        `summary`, show that summary instead."""
        from .planner import ToolStep
        last = None
        for st in plan.steps:
            if isinstance(st, ToolStep) and st.tool != "show":
                try:
                    returns = getattr(self.registry.get(st.tool), "returns", "") or ""
                except KeyError:
                    returns = ""
                last = st if "summary" in str(returns) else None
            elif isinstance(st, ToolStep) and st.tool == "show" and last is not None and isinstance(st.args.get("text"), str):
                st.args["text"] = {"$ref": f"{last.id}.summary"}
                last = None
        # A plan that looks something up but never shows it: show the last step's result.
        shows = [s for s in plan.steps if isinstance(s, ToolStep) and s.tool == "show"]
        tail = plan.steps[-1] if plan.steps else None
        if not shows and isinstance(tail, ToolStep) and tail.tool not in ("show", "ask.choose") and not tail.when:
            plan.steps.append(ToolStep.model_validate({"id": f"{tail.id}_shown", "tool": "show",
                                                       "args": {"text": {"$ref": tail.id}}}))

    @staticmethod
    def _drop_guessed_browser(plan: Plan, message: str) -> None:
        """A `browser` argument is the owner's choice: keep it only if the request names it."""
        from .planner import ForEachStep, ToolStep
        said = message.lower()

        def walk(steps) -> None:
            for st in steps:
                if isinstance(st, ToolStep) and st.tool.startswith("browser.") and isinstance(st.args.get("browser"), str):
                    name = st.args["browser"].strip().lower()
                    generic = name in ("browser", "the browser", "my browser", "default", "default browser", "current", "web")
                    if generic or name not in said:
                        del st.args["browser"]
                elif isinstance(st, ForEachStep):
                    walk(st.steps)
        walk(plan.steps)

    def _try_plan(self, planner: Planner, message: str, scope: Optional[TaskScope], prefs=None,
                  history=None) -> tuple:
        """-> (plan or None, problem or None). A plan is only returned if it is valid and in scope."""
        try:
            plan = planner.plan(message, preferences=prefs, history=history)
            self._drop_guessed_browser(plan, message)
            self._show_real_results(plan)
            validate_plan(plan, set(self.registry.names()))
        except PlanError as e:
            return None, f"invalid plan: {str(e).splitlines()[0][:300]}"
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
        if scope is not None:
            extra = out_of_scope(plan, scope)
            if extra:
                return None, f"the plan used {', '.join(extra)}, which this request did not ask for"
        return plan, None

    def _prefs_for(self, conv: Conversation, turn: Turn) -> Optional[List[str]]:
        """Owner-written preferences, only for normal chats and only while memory is unlocked."""
        if conv.mode != "normal" or not self.vault.exists:
            return None
        if not self.vault.unlocked:
            turn.notes.append("Memory is locked, so your saved preferences were not used. Unlock it in Settings.")
            return None
        return [p["text"] for p in self.vault.preferences()]

    @staticmethod
    def _explain_failure(problem: str, careful: str = "") -> str:
        """Plain words first, details after."""
        if problem.startswith("the plan used"):
            head = ("I can't do that with the tools I have yet. The only way I found needed "
                    + problem[len("the plan used "):].split(",")[0]
                    + ", which isn't allowed for a request like this.")
        else:
            head = "I couldn't make a plan for that."
        details = [problem] + ([f"A careful second try was skipped: {careful}"] if careful else [])
        return head + "\n\nDetails: " + " ".join(d.rstrip(".") + "." for d in details)

    @staticmethod
    def _history(conv: Conversation) -> List[tuple]:
        """Earlier turns for context: what the owner typed, and what the planner itself replied. Never
        anything a tool produced (web pages, mail, files), so no untrusted text reaches the planner."""
        out = []
        for t in conv.turns[-6:]:
            if t.answer:
                reply = t.answer
            elif t.plan:
                reply = f"(made a plan: {t.plan.get('goal', '')})"
            elif t.error:
                reply = "(could not help with that)"
            else:
                reply = ""
            out.append((t.request, reply))
        return out

    def _audit_answer(self, turn: Turn) -> None:
        try:
            Audit(self.audit_path).log("answer", request=turn.request[:300], answer=turn.answer[:2000],
                                       planned_by=turn.planned_by)
        except Exception:
            pass

    def _make_plan(self, turn: Turn, message: str, prefs: Optional[List[str]] = None,
                   history: Optional[List[tuple]] = None) -> Optional[Plan]:
        """Scope -> fast plan -> (rules say so) careful plan. Fills turn fields; returns the plan or None."""
        scope: Optional[TaskScope] = None
        if self.scoper is not None:
            scope = self.scoper.scope(message, history_block(history))   # the owner's words + planner replies only
            turn.scope, turn.scope_text, turn.open_ended = list(scope.capabilities), scope.describe(), scope.open_ended
        plan, problem = self._try_plan(self.planner, message, scope, prefs, history)
        turn.planned_by = self.model_names["planner"]

        reasons: List[str] = []
        if scope is not None and scope.open_ended:
            reasons.append("the request is open-ended")
        if problem:
            reasons.append(f"the fast model's {problem}" if problem.startswith("invalid") else problem)
        elif plan is not None:
            risky = sorted(plan_tools(plan) & self.policy.high_risk_tools())
            if risky:
                reasons.append(f"the plan uses high-risk {', '.join(risky)}")
        if not reasons:
            return plan

        turn.escalated = reasons
        if self.models is None or self.models.strong_backend is None:
            turn.notes.append("A careful second pass was due (" + "; ".join(reasons) + ") but no careful model is set up.")
            if plan is None:
                turn.error = self._explain_failure(problem)
            return plan
        try:
            with self.models.strong() as llm:
                careful_plan, careful_problem = self._try_plan(Planner(llm, self.catalog), message, scope, prefs, history)
        except (LowMemory, StrongUnavailable) as e:
            turn.notes.append(f"Careful second pass skipped: {e}")
            if plan is None:
                turn.error = self._explain_failure(problem, str(e))
            else:
                turn.notes.append("This is the fast model's plan. Review it closely before you execute it.")
            return plan
        if careful_plan is None:
            if plan is not None:
                turn.notes.append(f"The careful check could not produce a plan ({careful_problem[:160]}). "
                                  "This is the fast model's plan. Review it closely before you execute it.")
                return plan
            turn.error = f"The careful model could not make a safe plan either: {careful_problem}"
            return None
        turn.planned_by = self.models.strong_name
        turn.careful_reader = bool(re_has_quarantine(careful_plan))
        return careful_plan

    def plan_request(self, conversation_id: str, message: str) -> Turn:
        """Blocking (calls the planner model); run it off the event loop.

        Only `message` reaches the planner. Earlier turns, plans, run events and anything stored
        on disk are display data and are never part of the planner prompt.
        """
        if self.paused:
            raise RuntimeError("agent is paused")
        conv = self.conversations[conversation_id]
        turn = Turn(id=uuid.uuid4().hex[:12], kind="chat", request=message, created=time.time())
        self._planning += 1
        self._emit_state()
        try:
            plan = self._make_plan(turn, message, self._prefs_for(conv, turn), self._history(conv))
            answer = direct_answer(plan) if plan is not None else None
            if answer is not None:
                # Only planner-written text and nothing to execute: show it as the reply.
                turn.answer = answer
                turn.plan = plan.model_dump(by_alias=True, exclude_none=True)
                self._audit_answer(turn)
                self.last_error = None
            elif plan is not None:
                allowed = TaskScope(capabilities=turn.scope).allowed_tools() if turn.scope is not None else None
                self._register_plan(turn, plan, conv, allowed)
                self.last_error = None
        except ScopeError as e:
            turn.error = str(e)
        except PlanError as e:
            turn.error = str(e)
        except Exception as e:  # model unreachable, etc.
            turn.error = f"{type(e).__name__}: {e}"
        finally:
            self._planning -= 1
        if turn.error:
            self.last_error = turn.error
        conv.turns.append(turn)
        self._save(conv)
        self._emit_state()
        return turn

    def list_examples(self) -> List[dict]:
        out = []
        for p in sorted(EXAMPLES_DIR.glob("*.json")):
            if not EXAMPLE_NAME.match(p.stem):
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                item = {"name": p.stem, "goal": str(data.get("goal", "")), "steps": len(data.get("steps", []))}
                try:
                    missing = self.missing_tools(Plan.model_validate(data))
                except Exception:
                    missing = []
                item["unavailable"] = unavailable_message(missing) if missing else None
                out.append(item)
            except Exception:
                continue
        return out

    def load_example(self, conversation_id: str, name: str) -> Turn:
        if not EXAMPLE_NAME.match(name):
            raise KeyError(name)
        path = EXAMPLES_DIR / f"{name}.json"
        if not path.is_file():
            raise KeyError(name)
        conv = self.conversations[conversation_id]
        turn = Turn(id=uuid.uuid4().hex[:12], kind="example", request=f"Example: {name}", created=time.time())
        try:
            with open(path, "r", encoding="utf-8") as f:
                plan = Plan.model_validate(json.load(f))
            self._register_plan(turn, plan, conv)
        except Exception as e:
            turn.error = f"{type(e).__name__}: {e}"
        conv.turns.append(turn)
        self._save(conv)
        return turn

    # ---- execution --------------------------------------------------------------------------------
    def missing_tools(self, plan: Plan) -> List[str]:
        """Tools a (possibly old, restored) plan needs that this Mac does not have right now."""
        tools = plan_tools(plan)
        missing = set(tools) - set(self.registry.names())
        # the browser tools exist but do nothing until the Chrome extension has connected
        if not self.browser_ready():
            missing |= {t for t in tools if t.startswith("browser.")}
        return sorted(missing)

    def start_run(self, plan_id: str) -> Run:
        if self.paused:
            raise RuntimeError("agent is paused")
        plan, cid = self.plans[plan_id]
        missing = self.missing_tools(plan)
        if missing:
            raise RuntimeError(unavailable_message(missing))
        with self._run_lock:
            if self.current_run is not None:
                raise RuntimeError("a plan is already running")
            run = Run(id=uuid.uuid4().hex[:12], plan_id=plan_id, conversation_id=cid, goal=plan.goal,
                      started=time.time())
            run.approver = AsyncApprover(self.publish, timeout_s=self.approval_timeout_s)
            self.runs[run.id] = run
            self.current_run = run
        self.last_error = None
        conv = self.conversations[cid]
        for t in conv.turns:
            if t.plan_id == plan_id:
                t.run_ids.append(run.id)
        self._save(conv)
        self.publish({"type": "run.start", "run_id": run.id, "plan_id": plan_id, "conversation_id": cid,
                      "goal": plan.goal})
        self._emit_state()
        self._worker.submit(self._execute, run, plan)
        return run

    def _execute(self, run: Run, plan: Plan) -> None:
        audit = Audit(self.audit_path, sink=lambda rec: self.publish({"type": "audit", "record": rec}))
        audit.run_id = run.id
        meta = self.plan_meta.get(run.plan_id, {"allowed": None, "careful": False})

        def execute(reader: QuarantinedReader) -> None:
            # The watcher uses whichever model is already loaded (one model in memory at a time).
            watcher = Watcher(reader.llm) if self.watcher_enabled else None
            ex = _CancellableExecutor(self.policy, self.registry, reader, run.approver, audit,
                                      printer=self._log, cancel=run.cancel, allowed_tools=meta["allowed"],
                                      domains=self.domains, watcher=watcher, request=meta.get("request", ""))
            ex.run(plan)

        status, error = "done", None
        try:
            if meta["careful"] and self.models is not None and self.models.strong_backend is not None:
                # untrusted content in this plan decides actions: read it with the careful model
                self._log(f"[models] reading with {self.models.strong_name}")
                with self.models.strong() as llm:
                    execute(QuarantinedReader(llm))
            else:
                execute(self.reader)
            if run.cancel.is_set():
                status = "cancelled"
        except LowMemory as e:
            status, error = "error", str(e)
        except PolicyViolation as e:
            status, error = "refused", str(e)
        except ExecutionError as e:
            status, error = ("cancelled", None) if run.cancel.is_set() else ("error", str(e))
        except Exception as e:
            status, error = "error", f"{type(e).__name__}: {e}"
        finally:
            run.approver.close()
            run.status, run.error, run.ended = status, error, time.time()
            if status in ("error", "refused"):
                self.last_error = error
            self.publish({"type": "run.end", "run_id": run.id, "plan_id": run.plan_id, "status": status,
                          "error": error})
            with self._run_lock:
                self.current_run = None
            conv = self.conversations.get(run.conversation_id)
            if conv is not None:
                self._save(conv)
            self._emit_state()

    def cancel_run(self, run_id: str) -> bool:
        run = self.runs[run_id]
        if run.status != "running":
            return False
        run.cancel.set()
        run.approver.close(by="cancel")
        return True

    def resolve_choice(self, choice_id: str, selected: List[str]) -> bool:
        run = self.current_run
        if run is None or run.approver is None:
            return False
        return run.approver.resolve_choice(choice_id, selected)

    def resolve_approval(self, approval_id: str, decision: str) -> bool:
        run = self.current_run
        if run is None or run.approver is None:
            return False
        return run.approver.resolve(approval_id, decision)

    # ---- introspection ----------------------------------------------------------------------------
    def read_audit(self, limit: int = 200, run_id: Optional[str] = None) -> List[dict]:
        if not self.audit_path.exists():
            return []
        with open(self.audit_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4_000_000))
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        if size > 4_000_000 and lines:
            lines = lines[1:]  # first line may be cut
        out = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if run_id is None or rec.get("run") == run_id:
                out.append(rec)
        return out[-limit:]

    def settings(self) -> dict:
        p = self.policy
        return {
            "models": self._model_view(),
            "fake": self.fake,
            "paused": self.paused,
            "external_services": p.external_services(),
            "policy": {
                "max_steps": p.max_steps,
                "tools": [{"name": n, "allow_sources": sorted(t.allow_sources), "approval": t.approval,
                           "description": t.description, "available": n in self.registry.names()}
                          for n, t in p.tools.items()],
                "filesystem": {"readable_roots": [str(r) for r in p.readable_roots],
                               "writable_roots": [str(r) for r in p.writable_roots],
                               "denied_paths": [str(r) for r in p.denied_paths],
                               "denied_globs": list(p.denied_globs)},
                "browser": {"enabled": p.browser_enabled, "blocked_hosts": list(p.browser_blocked_hosts)},
                "shell": {"cwd": str(p.shell_cwd), "timeout_s": p.shell_timeout_s,
                          "denied_patterns": list(p.shell_denied_patterns),
                          "allowed_programs": list(p.shell_allowed_programs)},
            },
            "paths": {"audit": str(self.audit_path), "secrets": str(SECRETS_PATH), "home": str(HOME),
                      "conversations": str(self.store.dir) if self.store.dir else None},
            "approval_timeout_s": self.approval_timeout_s,
            "chrome_extension": {"connected": get_bridge().connected(), "version": get_bridge().extension_version,
                                 "browsers": get_bridge().browsers()},
        }

    def status(self) -> dict:
        run = self.current_run
        return {
            "ok": True, "version": VERSION, "pid": os.getpid(), "started": self.started, "fake": self.fake,
            "models": self._model_view(), "run": run.summary() if run else None,
            "conversations": len(self.conversations), "ui_clients": len(self._subscribers), **self.state_view(),
            "permission_needed": self.permission_needed, "standby_info": self.standby_info,
            "chrome_extension": {"connected": get_bridge().connected(), "version": get_bridge().extension_version,
                                 "browsers": get_bridge().browsers()},
        }

    def probe_ollama(self) -> dict:
        """Blocking; reports whether Ollama answers and whether the configured models are pulled."""
        try:
            url = self.policy.effective_ollama_url()
        except PolicyViolation as e:
            return {"reachable": False, "error": str(e), "url": None}
        try:
            r = httpx.get(f"{url}/api/tags", timeout=2.0)
            r.raise_for_status()
            names = {m.get("name", "") for m in r.json().get("models", [])}
        except Exception as e:
            return {"reachable": False, "error": f"{type(e).__name__}: {e}", "url": url}
        return {"reachable": True, "url": url,
                "models_present": {k: (v in names or f"{v}:latest" in names) for k, v in self.model_names.items()}}

    # ---- permissions -------------------------------------------------------------------------------
    @staticmethod
    def _probe_dir(path: Path) -> str:
        try:
            os.listdir(path)
            return "granted"
        except PermissionError:
            return "denied"
        except FileNotFoundError:
            return "unknown"
        except OSError:
            return "denied"

    def probe_folders(self) -> Dict[str, str]:
        """Touches each readable root once. On macOS this is what triggers the folder-access prompt,
        so it runs only when asked (settings → Check folder access), never on a plain GET."""
        for root in self.policy.readable_roots + self.policy.writable_roots:
            self._folder_probe[str(root)] = self._probe_dir(root)
        return dict(self._folder_probe)

    def permissions(self) -> List[dict]:
        out = []
        for spec in PERMISSIONS:
            entry = dict(spec)
            if spec["name"] == "full_disk_access":
                # Listing ~/Library/Safari fails with EPERM without Full Disk Access; it never prompts.
                entry["status"] = self._probe_dir(Path("~/Library/Safari").expanduser())
            else:
                rep = self.permission_reports.get(spec["name"])
                entry["status"] = rep["status"] if rep else "unknown"
                entry["detail"] = rep.get("detail") if rep else None
            out.append(entry)
        for root in self.policy.readable_roots + self.policy.writable_roots:
            out.append({"name": f"folder:{root}", "title": f"Folder access: {root}", "source": "core",
                        "why": "macOS asks the first time the agent reads a protected folder such as Desktop, "
                               "Documents or Downloads.",
                        "how": "Allow the prompt, or System Settings → Privacy & Security → Files and Folders.",
                        "status": self._folder_probe.get(str(root), "unknown")})
        return out

    def report_permission(self, name: str, status: str, detail: Optional[str] = None) -> dict:
        if name not in {p["name"] for p in PERMISSIONS}:
            raise KeyError(name)
        if status not in PERMISSION_STATUSES:
            raise ValueError(status)
        self.permission_reports[name] = {"status": status, "detail": detail, "ts": time.time()}
        return next(p for p in self.permissions() if p["name"] == name)

    def shutdown(self) -> None:
        run = self.current_run
        if run is not None:
            self.cancel_run(run.id)
        self._worker.shutdown(wait=False, cancel_futures=True)
        browser = getattr(self.registry, "browser", None)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def direct_answer(plan: Plan) -> Optional[str]:
    """The reply text if the plan only shows text the planner wrote itself (no tools, no references)."""
    from .planner import ToolStep
    texts = []
    for st in plan.steps:
        if not (isinstance(st, ToolStep) and st.tool == "show" and not st.when):
            return None
        t = st.args.get("text")
        if not isinstance(t, str) or set(st.args) - {"text"}:
            return None
        texts.append(t.strip())
    return "\n\n".join(texts) if texts and any(texts) else None


def unavailable_message(missing: List[str]) -> str:
    why = ""
    if any(m.startswith("gmail.") for m in missing):
        why = " The Gmail mail-server tools need a Gmail app password; or ask me to do it in the browser instead."
    elif any(m.startswith("browser.") for m in missing):
        why = (" The browser isn't connected: load the Rozan extension in Chrome (README → Browser) "
               "and keep Chrome open. Then press Execute again.")
    if any(m.startswith("browser.") for m in missing) and all(m.startswith("browser.") for m in missing):
        return ("This needs the browser, which isn't connected yet. Load the Rozan extension in Chrome "
                "(README → Browser) and keep Chrome open, then press Execute again.")
    return f"This plan can't run here: it needs {', '.join(missing)}, which isn't available.{why}"


def _memory_snapshot() -> dict:
    from .memory import snapshot
    return snapshot()


def re_has_quarantine(plan: Plan) -> bool:
    from .planner import ForEachStep, QuarantineStep

    def walk(steps) -> bool:
        return any(isinstance(s, QuarantineStep) or (isinstance(s, ForEachStep) and walk(s.steps)) for s in steps)
    return walk(plan.steps)


def build_service(policy: Policy, *, fake: bool = False, audit_path: Path = AUDIT_PATH,
                  store_dir: Optional[Path] = None) -> AgentService:
    store_dir = CONVERSATIONS_DIR if store_dir is None else store_dir
    if fake:
        from .fakes import ScriptedPlannerLLM, ScriptedReaderLLM, ScriptedScopeLLM, build_fake_registry
        return AgentService(policy, lambda printer, show: build_fake_registry(policy, show=show),
                            ScriptedPlannerLLM(), ScriptedReaderLLM(), audit_path, store_dir=store_dir, fake=True,
                            planner_model="fake", reader_model="fake", scoper=Scoper(ScriptedScopeLLM()))
    url = policy.effective_ollama_url()
    fast_name = os.environ.get("PLANNER_MODEL") or policy.models.get("fast") or PLANNER_MODEL
    fast = OllamaLLM(fast_name, url=url, keep_alive=os.environ.get("AGENT_KEEP_ALIVE", "1m"))
    models = ModelManager(fast, fast_name, strong_from_config(policy.models.get("strong")))
    svc = AgentService(policy, lambda printer, show: build_registry(policy, printer=printer, show=show),
                        fast, fast, audit_path, store_dir=store_dir, planner_model=fast_name,
                        reader_model=fast_name, scoper=Scoper(fast), models=models)
    svc.browser_ready = lambda: get_bridge().connected()
    return svc


# ---- HTTP app -----------------------------------------------------------------------------------
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: Optional[str] = None


class ConversationIn(BaseModel):
    conversation_id: Optional[str] = None


class ChoiceIn(BaseModel):
    selected: List[str] = Field(default_factory=list, max_length=100)


class NewConversationIn(BaseModel):
    mode: Literal["normal", "isolated", "incognito"] = "normal"


class PassIn(BaseModel):
    passphrase: str = Field(min_length=1, max_length=200)


class PrefIn(BaseModel):
    text: str = Field(min_length=1, max_length=300)


class WatcherIn(BaseModel):
    enabled: bool


class LabelIn(BaseModel):
    label: str = Field(max_length=40)


class ApprovalIn(BaseModel):
    decision: Literal["yes", "no", "all"]


class PermissionIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    status: Literal["granted", "denied", "not_requested", "unknown"]
    detail: Optional[str] = Field(default=None, max_length=400)


def _host_ok(host: str) -> bool:
    host = (host or "").strip().lower()
    if host.startswith("["):
        host = host.split("]")[0] + "]"
    else:
        host = host.split(":")[0]
    return host in ALLOWED_HOSTS


def create_app(service: AgentService, token: str, ui_dir: Path = UI_DIR) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service.attach(asyncio.get_running_loop())

        async def watchdog():
            every = float((service.policy.memory or {}).get("check_every_s", 5))
            while True:
                await asyncio.sleep(every)
                await asyncio.to_thread(service.check_memory)
                service.housekeeping()

        dog = asyncio.create_task(watchdog()) if not service.fake else None
        yield
        if dog:
            dog.cancel()
        service.shutdown()

    app = FastAPI(title="personal_agent", version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service

    def token_ok(candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), token.encode())

    def require_token(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not token_ok(auth[7:].strip()):
            raise HTTPException(status_code=401, detail="missing or invalid session token")

    auth = [Depends(require_token)]

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        if not _host_ok(request.headers.get("host", "")):
            return JSONResponse({"detail": "bad Host header"}, status_code=403)
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = CSP
        return resp

    def conv_or_404(cid: Optional[str]) -> Conversation:
        try:
            return service.conversation(cid)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown conversation")

    # ---- liveness / status
    @app.get("/health")
    async def health():
        return {"ok": True, "version": VERSION, "pid": os.getpid()}

    @app.get("/status", dependencies=auth)
    async def status(deep: bool = False):
        out = service.status()
        if deep:
            out["ollama"] = None if service.fake else await asyncio.to_thread(service.probe_ollama)
        return out

    @app.post("/pause", dependencies=auth)
    async def pause():
        service.pause()
        return service.state_view()

    @app.post("/watcher", dependencies=auth)
    async def set_watcher(body: WatcherIn):
        service.watcher_enabled = body.enabled
        service._emit_state()
        return service.state_view()

    @app.post("/standby", dependencies=auth)
    async def standby():
        service.pause()
        return service.state_view()

    @app.post("/resume", dependencies=auth)
    async def resume():
        service.resume()
        await asyncio.to_thread(service.check_memory)     # still too full? straight back to standby, with the reason
        return service.state_view()

    @app.post("/state/ack", dependencies=auth)
    async def ack():
        service.ack_error()
        return service.state_view()

    @app.get("/settings", dependencies=auth)
    async def settings():
        return service.settings()

    @app.get("/permissions", dependencies=auth)
    async def permissions():
        return service.permissions()

    @app.post("/permissions", dependencies=auth)
    async def report_permission(body: PermissionIn):
        try:
            return service.report_permission(body.name, body.status, body.detail)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown permission")

    @app.post("/permissions/probe", dependencies=auth)
    async def probe_permissions():
        await asyncio.to_thread(service.probe_folders)
        return service.permissions()

    # ---- conversations and planning
    @app.get("/conversations", dependencies=auth)
    async def conversations():
        return service.conversation_list()

    @app.post("/conversations", dependencies=auth, status_code=201)
    async def new_conversation(body: NewConversationIn | None = None):
        return service.conversation_view(service.new_conversation(body.mode if body else "normal"))

    def vault_call(fn):
        try:
            return fn()
        except VaultError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/memory", dependencies=auth)
    async def memory_status():
        out = service.vault.status()
        if out["unlocked"]:
            out["preferences"] = service.vault.preferences()
        return out

    @app.post("/memory/setup", dependencies=auth)
    async def memory_setup(body: PassIn):
        await asyncio.to_thread(vault_call, lambda: service.vault.setup(body.passphrase))
        return await memory_status()

    @app.post("/memory/unlock", dependencies=auth)
    async def memory_unlock(body: PassIn):
        await asyncio.to_thread(vault_call, lambda: service.vault.unlock(body.passphrase))
        return await memory_status()

    @app.post("/memory/lock", dependencies=auth)
    async def memory_lock():
        service.vault.lock()
        return await memory_status()

    @app.post("/memory/preferences", dependencies=auth)
    async def memory_add(body: PrefIn):
        vault_call(lambda: service.vault.add_preference(body.text))
        return await memory_status()

    @app.delete("/memory/preferences/{pref_id}", dependencies=auth)
    async def memory_remove(pref_id: str):
        if not vault_call(lambda: service.vault.remove_preference(pref_id)):
            raise HTTPException(status_code=404, detail="no such preference")
        return await memory_status()

    @app.get("/conversations/{cid}", dependencies=auth)
    async def get_conversation(cid: str):
        return service.conversation_view(conv_or_404(cid))

    @app.delete("/conversations/{cid}", dependencies=auth, status_code=204)
    async def delete_conversation(cid: str):
        conv_or_404(cid)
        try:
            service.delete_conversation(cid)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return Response(status_code=204)

    @app.post("/chat", dependencies=auth)
    async def chat(body: ChatIn):
        if service.paused:
            raise HTTPException(status_code=409, detail="agent is paused")
        conv = conv_or_404(body.conversation_id)
        try:
            turn = await asyncio.to_thread(service.plan_request, conv.id, body.message)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"conversation_id": conv.id, "turn": service.turn_view(turn)}

    @app.get("/examples", dependencies=auth)
    async def examples():
        return service.list_examples()

    @app.post("/examples/{name}/load", dependencies=auth)
    async def load_example(name: str, body: ConversationIn | None = None):
        conv = conv_or_404(body.conversation_id if body else None)
        try:
            turn = service.load_example(conv.id, name)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown example")
        return {"conversation_id": conv.id, "turn": service.turn_view(turn)}

    # ---- execution
    @app.post("/plan/{plan_id}/run", dependencies=auth, status_code=202)
    async def run_plan(plan_id: str):
        if plan_id not in service.plans:
            raise HTTPException(status_code=404, detail="unknown plan")
        try:
            run = service.start_run(plan_id)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return run.summary()

    @app.get("/run/{run_id}", dependencies=auth)
    async def get_run(run_id: str):
        if run_id not in service.runs:
            raise HTTPException(status_code=404, detail="unknown run")
        run = service.runs[run_id]
        return run.summary() | {"events": list(run.events)}

    @app.post("/run/{run_id}/cancel", dependencies=auth)
    async def cancel_run(run_id: str):
        if run_id not in service.runs:
            raise HTTPException(status_code=404, detail="unknown run")
        return {"cancelled": service.cancel_run(run_id)}

    @app.post("/choice/{choice_id}", dependencies=auth)
    async def choose(choice_id: str, body: ChoiceIn):
        if not service.resolve_choice(choice_id, body.selected):
            raise HTTPException(status_code=404, detail="no such pending choice")
        return {"ok": True, "choice_id": choice_id}

    @app.post("/approval/{approval_id}", dependencies=auth)
    async def approve(approval_id: str, body: ApprovalIn):
        if not service.resolve_approval(approval_id, body.decision):
            raise HTTPException(status_code=404, detail="no such pending approval")
        return {"ok": True, "approval_id": approval_id, "decision": body.decision}

    @app.get("/browsers", dependencies=auth)
    async def browsers():
        return get_bridge().browsers()

    @app.post("/browsers/{instance_id}/label", dependencies=auth)
    async def label_browser(instance_id: str, body: LabelIn):
        if instance_id not in {b["id"] for b in get_bridge().browsers()}:
            raise HTTPException(status_code=404, detail="no such connected browser")
        get_bridge().set_label(instance_id, body.label)
        return get_bridge().browsers()

    @app.post("/stop", dependencies=auth)
    async def emergency_stop():
        """Emergency stop: cancel the running plan, deny its pending approval, and pause the agent."""
        service.pause("Emergency stop")
        return service.state_view()

    @app.get("/audit/verify", dependencies=auth)
    async def audit_verify():
        if not service.audit_path.exists():
            return {"ok": True, "records": 0, "problem": ""}
        ok, n, problem = await asyncio.to_thread(verify_audit, service.audit_path)
        return {"ok": ok, "records": n, "problem": problem}

    @app.get("/audit", dependencies=auth)
    async def audit(limit: int = 200, run: Optional[str] = None):
        limit = max(1, min(int(limit), 2000))
        return await asyncio.to_thread(service.read_audit, limit, run)

    # ---- events
    @app.websocket("/events")
    async def events(ws: WebSocket):
        if not _host_ok(ws.headers.get("host", "")):
            await ws.close(code=1008)
            return
        await ws.accept()
        header = ws.headers.get("authorization", "")
        ok = header.startswith("Bearer ") and token_ok(header[7:].strip())
        if not ok:
            try:
                first = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                ok = isinstance(first, dict) and token_ok(str(first.get("token", "")))
            except Exception:
                ok = False
        if not ok:
            await ws.close(code=1008)
            return
        q = service.subscribe()
        recv = asyncio.ensure_future(ws.receive())
        get = asyncio.ensure_future(q.get())
        try:
            run = service.current_run
            await ws.send_text(json.dumps({"type": "hello", "version": VERSION, "ts": time.time(),
                                           "run": run.summary() if run else None, **service.state_view()},
                                          default=str))
            while True:
                done, _ = await asyncio.wait({recv, get}, return_when=asyncio.FIRST_COMPLETED)
                if recv in done:
                    msg = recv.result()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    recv = asyncio.ensure_future(ws.receive())  # client pings are ignored
                if get in done:
                    await ws.send_text(json.dumps(get.result(), default=str))
                    get = asyncio.ensure_future(q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            service.unsubscribe(q)
            recv.cancel()
            get.cancel()

    # ---- static UI (no auth: the page is public, the API is not)
    @app.get("/")
    async def index():
        return FileResponse(ui_dir / "index.html")

    app.mount("/", StaticFiles(directory=str(ui_dir)), name="ui")
    return app


# ---- entry point ---------------------------------------------------------------------------------
def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(128)
    return sock


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent.server", description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("AGENT_PORT", "8765")), help="0 picks a free port")
    ap.add_argument("--policy", default=str(ROOT / "policy.yaml"))
    ap.add_argument("--fake", action="store_true", help="scripted models and fake tools; no Ollama, Gmail or browser")
    ap.add_argument("--open", action="store_true", help="print the UI URL (token in the fragment) and open it")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    policy = Policy.load(args.policy)
    try:
        service = build_service(policy, fake=args.fake)
    except PolicyViolation as e:
        print(f"refusing to start: {e}")
        return 2
    token = secrets.token_urlsafe(32)
    HOME.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)
    _write_private(TOKEN_PATH, token)
    sock = bind(args.port)
    port = sock.getsockname()[1]
    _write_private(SESSION_PATH, json.dumps({"port": port, "pid": os.getpid(), "started": time.time(),
                                             "token_file": str(TOKEN_PATH), "fake": args.fake}))
    app = create_app(service, token)
    if not args.fake and policy.browser_enabled:
        get_bridge().start()               # the Chrome extension's native host connects here
    url = f"http://127.0.0.1:{port}/"
    external = [s for s in policy.external_services() if s["enabled"] and not s["local"]]
    print(f"personal_agent core on {url}  token: {TOKEN_PATH}{'  [FAKE MODE]' if args.fake else ''}", flush=True)
    print("external services enabled: " + (", ".join(s["service"] for s in external) or "none (fully offline)"), flush=True)
    if args.open:
        print(f"{url}#token={token}", flush=True)
        webbrowser.open(f"{url}#token={token}")
    config = uvicorn.Config(app, log_level="info" if args.verbose else "warning", access_log=args.verbose,
                            ws_ping_interval=20, ws_ping_timeout=20, timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)

    def cleanup() -> None:
        get_bridge().stop()
        for p in (TOKEN_PATH, SESSION_PATH):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    # uvicorn handles SIGINT/SIGTERM itself and, after a graceful shutdown, re-raises the signal with
    # the default handler, which would end the process before any `finally` here. These handlers are
    # what it restores and re-raises into, so the session files are removed on the way out.
    def on_signal(signum, frame):
        cleanup()
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)
    try:
        server.run(sockets=[sock])
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
