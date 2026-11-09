"""Task scope: what a request is allowed to do, decided from the user's words alone.

A separate model call reads ONLY the user's request (never tool results or untrusted content) and
picks the capabilities it needs from a fixed list. Code then refuses any plan step whose tool is
outside those capabilities, both when the plan is made and again at run time. "Label my bank
emails" therefore can never produce a plan that sends or trashes mail, whatever the planner wrote.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List, Literal, Set

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .llm import LLM, extract_json
from .planner import ForEachStep, Plan, Step, ToolStep

# capability -> (plain description, tools it unlocks). A tool not listed here is never in scope.
CAPABILITIES: Dict[str, tuple[str, FrozenSet[str]]] = {
    "mail.read":    ("list and read emails", frozenset({"gmail.list", "gmail.get"})),
    "mail.organize": ("label, archive or mark emails read", frozenset({"gmail.label", "gmail.archive", "gmail.mark_read"})),
    "mail.trash":   ("move emails to Trash", frozenset({"gmail.trash"})),
    "mail.send":    ("send an email", frozenset({"gmail.send"})),
    "files.read":   ("list and read files", frozenset({"fs.list", "fs.read"})),
    "files.write":  ("create or change files", frozenset({"fs.write"})),
    "files.trash":  ("move files to the Trash", frozenset({"fs.move_to_trash"})),
    "apps.open":    ("open an app", frozenset({"app.open"})),
    "system.read":  ("check this Mac: memory, disk space, battery, busiest apps, keyboards",
                     frozenset({"system.memory", "system.disk", "system.battery", "system.top_apps", "system.keyboards"})),
    "system.keyboards": ("add or remove keyboards (input languages)",
                         frozenset({"system.add_keyboard", "system.remove_keyboard"})),
    "shell":        ("run a terminal command", frozenset({"shell.run"})),
    "browser.read": ("open web pages and read them", frozenset({"browser.open", "browser.read", "browser.screenshot", "browser.list", "browser.controls"})),
    "browser.act":  ("click and type on web pages", frozenset({"browser.click", "browser.type"})),
    "desktop.read": ("see which apps are open and read their windows",
                     frozenset({"desktop.apps", "desktop.controls", "desktop.read"})),
    "desktop.act":  ("click, type and press keys in other apps", frozenset({"desktop.press", "desktop.type", "desktop.key"})),
    "setup":        ("install or set up the agent's own Chrome extension (browser bridge)",
                     frozenset({"setup.chrome_extension"})),
}
ALWAYS_ALLOWED: FrozenSet[str] = frozenset({"show", "ask.choose"})
# Acting on something needs reading it first. Added in code so the scope model need not remember.
IMPLIES: Dict[str, FrozenSet[str]] = {
    "mail.organize": frozenset({"mail.read"}),
    "mail.trash": frozenset({"mail.read"}),
    "files.write": frozenset({"files.read"}),
    "files.trash": frozenset({"files.read"}),
    "browser.act": frozenset({"browser.read"}),
    "system.keyboards": frozenset({"system.read"}),
    "desktop.act": frozenset({"desktop.read", "apps.open"}),
}
CapabilityName = Literal["mail.read", "mail.organize", "mail.trash", "mail.send", "files.read", "files.write",
                         "files.trash", "apps.open", "system.read", "system.keyboards", "shell", "browser.read", "browser.act",
                         "desktop.read", "desktop.act", "setup"]


class ScopeError(Exception):
    pass


class TaskScope(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=200)
    capabilities: List[CapabilityName] = Field(max_length=len(CAPABILITIES))
    open_ended: bool = False

    def expanded(self) -> List[str]:
        caps = set(self.capabilities)
        for c in list(caps):
            caps |= IMPLIES.get(c, frozenset())
        return [c for c in CAPABILITIES if c in caps]

    def allowed_tools(self) -> FrozenSet[str]:
        tools: Set[str] = set(ALWAYS_ALLOWED)
        for c in self.expanded():
            tools |= CAPABILITIES[c][1]
        return frozenset(tools)

    def describe(self) -> List[str]:
        return [CAPABILITIES[c][0].split(" (")[0] for c in self.expanded()]


SYSTEM = """You decide the MINIMUM permissions a personal assistant needs for one request.
Choose only from this list:
{caps}
Rules:
- Pick only what the request clearly needs. Reading is not writing; labelling is not deleting; reading
  mail is not sending mail. If the user did not ask to send, delete, write, run commands or click, do not
  include those.
- Questions about facts from the internet (weather, news, prices, scores, opening hours, "look up ...") need
  browser.read. Questions about this Mac need system.read. General how-to questions need nothing.
- Doing something in another app (Notes, Teams, Finder...) with no dedicated permission needs desktop.act.
- open_ended is true when the request is vague or broad ("handle my inbox", "clean up my Mac", "do whatever
  is needed"), false when it names a specific job.
Return ONLY a JSON object: {{"capabilities": [..], "open_ended": true|false}}"""


class Scoper:
    def __init__(self, llm: LLM, retries: int = 1):
        self.llm = llm
        self.retries = retries
        self.system = SYSTEM.format(caps="\n".join(f"- {k}: {v[0]}" for k, v in CAPABILITIES.items()))

    def scope(self, request: str, context: str = "") -> TaskScope:
        """`context`: earlier turns of the same chat (the owner's words and planner replies only)."""
        err: Exception | None = None
        for _ in range(self.retries + 1):
            text = self.llm.complete_json(self.system, f"{context}REQUEST: {request}")
            try:
                return TaskScope.model_validate(extract_json(text))
            except (ValueError, ValidationError) as e:
                err = e
        raise ScopeError(f"could not work out what this request is allowed to do: {err}")


def plan_tools(plan: Plan) -> Set[str]:
    out: Set[str] = set()

    def walk(steps: List[Step]) -> None:
        for s in steps:
            if isinstance(s, ToolStep):
                out.add(s.tool)
            elif isinstance(s, ForEachStep):
                walk(s.steps)
    walk(plan.steps)
    return out


def out_of_scope(plan: Plan, scope: TaskScope) -> List[str]:
    return sorted(plan_tools(plan) - scope.allowed_tools())
