"""Tool registry. Each tool is a plain function taking raw (already policy-checked) arguments.

A tool returns a Python value; the executor wraps it in Tainted with the tool's `source` label.
Tools that read external content (email, web, files, shell output) are the untrusted sources.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    description: str
    args: Dict[str, str]           # arg name -> description
    source: str                    # provenance label applied to the result
    required: List[str] = field(default_factory=list)
    returns: str = ""              # shape of the result, so the planner can $ref the right fields


class Registry:
    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}

    def add(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"unknown tool {name!r}")
        return self._tools[name]

    def names(self) -> List[str]:
        return sorted(self._tools)

    def catalog(self, policy=None) -> str:
        lines = []
        for name in self.names():
            t = self._tools[name]
            args = ", ".join(f"{k} ({v})" for k, v in t.args.items())
            extra = ""
            if policy is not None and name in policy.tools:
                tp = policy.tools[name]
                extra = f" [arg sources allowed: {sorted(tp.allow_sources)}; approval={tp.approval}]"
            ret = f" Returns: {t.returns}." if t.returns else ""
            lines.append(f"- {name}: {t.description} Args: {args or 'none'}.{ret}{extra}")
        return "\n".join(lines)


def _ask_choose(**_):
    raise RuntimeError("ask.choose is handled by the executor")


ASK_CHOOSE = ToolSpec(
    "ask.choose", _ask_choose,
    "Ask the owner to pick from options (e.g. labels read from a page with browser.controls). Only what the "
    "owner picks comes back, and it counts as the owner's own input, so it may be used as a click target.",
    {"question": "what to ask", "options": "list of option labels (may come from a page)",
     "multiple": "true to allow several", "suggested": "optional pre-ticked options"},
    source="user", required=["question", "options"], returns="list of the labels the owner picked")


def _has_secret(handle: str) -> bool:
    from .. import broker
    try:
        broker.get(handle)
        return True
    except Exception:
        return False


def build_registry(policy, printer=print, show: Callable[[Any], Any] | None = None) -> Registry:
    """`show` receives the raw value of every `show` step (a UI renders it as untrusted output);
    by default it is printed, labelled, with `printer`."""
    from . import apps, browser, desktop, fs, gmail, shell, system  # noqa: WPS433
    if show is None:
        show = lambda text: printer(f"[untrusted output] {text}")  # noqa: E731
    reg = Registry()
    reg.add(ToolSpec("show", lambda text: show(text) or True,
                     "Display a value to the user. The only place untrusted text may go.",
                     {"text": "value to display"}, source="show", required=["text"], returns="true"))
    reg.add(ASK_CHOOSE)
    fs.register(reg, policy)
    shell.register(reg, policy)
    apps.register(reg, policy)
    system.register(reg, policy)
    # External services are opt-in (policy.yaml → network / browser). When one is off its tools are
    # not registered at all, so the planner's catalog never mentions them.
    # IMAP mail tools appear only once an app password is saved; otherwise mail is done in the browser.
    if policy.gmail_enabled and _has_secret("gmail"):
        gmail.register(reg, policy)
    if policy.browser_enabled:
        browser.register(reg, policy)
    if policy.desktop_enabled:
        desktop.register(reg, policy)
        desktop.register_setup(reg, policy)
    return reg
