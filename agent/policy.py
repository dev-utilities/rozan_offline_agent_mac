"""Data-flow and approval policy, loaded from policy.yaml and enforced in code.

The policy is the security boundary. The model (planner or reader) can propose anything; this
module decides what is actually allowed to execute. It also lists every external service the
agent may contact: Ollama (local), Gmail (opt-in) and the owner's Chrome. Nothing else in
the code base opens a network connection.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List
from urllib.parse import urlparse

import yaml

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class PolicyViolation(Exception):
    pass


@dataclass
class ToolPolicy:
    name: str
    allow_sources: FrozenSet[str]  # "*" means any source
    approval: bool | str = False   # True, False, or "new_domain" (ask only on the first visit to a website)
    description: str = ""
    risk: str = "low"              # "high" sends the plan to the careful model for a second planning pass

    def allows(self, sources: FrozenSet[str]) -> bool:
        if "*" in self.allow_sources:
            return True
        return sources <= self.allow_sources


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_local_url(url: str) -> bool:
    return (urlparse(url).hostname or "") in LOCAL_HOSTS


@dataclass
class Policy:
    tools: Dict[str, ToolPolicy]
    readable_roots: List[Path] = field(default_factory=list)
    writable_roots: List[Path] = field(default_factory=list)
    denied_paths: List[Path] = field(default_factory=list)      # never readable or writable
    denied_globs: List[str] = field(default_factory=list)       # fnmatch on the full path or the name
    browser_enabled: bool = True
    browser_blocked_hosts: List[str] = field(default_factory=list)   # never opened, never asked
    desktop_enabled: bool = True
    desktop_blocked_apps: List[str] = field(default_factory=list)    # names or bundle ids; never controlled
    browser_profile_dir: Path = Path("~/.personal_agent/browser-profile").expanduser()
    shell_denied_patterns: List[str] = field(default_factory=list)
    shell_allowed_programs: List[str] = field(default_factory=list)  # empty = any program (not recommended)
    shell_cwd: Path = Path("~").expanduser()
    shell_timeout_s: int = 60
    max_steps: int = 200
    models: dict = field(default_factory=dict)    # policy.yaml → models (fast / strong), read by the server
    memory: dict = field(default_factory=dict)    # policy.yaml → memory (auto-standby thresholds)
    # network: everything the agent may talk to
    ollama_url: str = "http://127.0.0.1:11434"
    allow_remote_ollama: bool = False
    gmail_enabled: bool = False
    gmail_imap_host: str = "imap.gmail.com"
    gmail_smtp_host: str = "smtp.gmail.com"

    # ---- loading ---------------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | os.PathLike) -> "Policy":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Policy":
        tools: Dict[str, ToolPolicy] = {}
        for name, spec in (raw.get("tools") or {}).items():
            spec = spec or {}
            tools[name] = ToolPolicy(
                name=name,
                allow_sources=frozenset(spec.get("allow_sources") or []),
                approval=(spec.get("approval") if spec.get("approval") == "new_domain"
                          else bool(spec.get("approval", False))),
                description=spec.get("description", ""),
                risk=str(spec.get("risk", "low")),
            )
        fs = raw.get("filesystem") or {}
        br = raw.get("browser") or {}
        sh = raw.get("shell") or {}
        net = raw.get("network") or {}
        gm = net.get("gmail") or {}
        expand = lambda ps: [Path(p).expanduser().resolve() for p in (ps or [])]  # noqa: E731
        return cls(
            tools=tools,
            readable_roots=expand(fs.get("readable_roots")),
            writable_roots=expand(fs.get("writable_roots")),
            denied_paths=expand(fs.get("denied_paths")),
            denied_globs=[str(g) for g in (fs.get("denied_globs") or [])],
            browser_enabled=bool(br.get("enabled", True)),
            browser_blocked_hosts=[h.lower() for h in (br.get("blocked_hosts") or [])],
            desktop_enabled=bool((raw.get("desktop") or {}).get("enabled", True)),
            desktop_blocked_apps=[str(a).lower() for a in ((raw.get("desktop") or {}).get("blocked_apps") or [])],
            browser_profile_dir=Path(br.get("profile_dir", "~/.personal_agent/browser-profile")).expanduser(),
            shell_denied_patterns=list(sh.get("denied_patterns") or []),
            shell_allowed_programs=list(sh.get("allowed_programs") or []),
            shell_cwd=Path(sh.get("cwd", "~")).expanduser(),
            shell_timeout_s=int(sh.get("timeout_s", 60)),
            max_steps=int(raw.get("max_steps", 200)),
            models=dict(raw.get("models") or {}),
            memory=dict(raw.get("memory") or {}),
            ollama_url=str(net.get("ollama_url", "http://127.0.0.1:11434")).rstrip("/"),
            allow_remote_ollama=bool(net.get("allow_remote_ollama", False)),
            gmail_enabled=bool(gm.get("enabled", False)),
            gmail_imap_host=str(gm.get("imap_host", "imap.gmail.com")),
            gmail_smtp_host=str(gm.get("smtp_host", "smtp.gmail.com")),
        )

    # ---- checks ----------------------------------------------------------------------------
    def tool(self, name: str) -> ToolPolicy:
        if name not in self.tools:
            raise PolicyViolation(f"tool {name!r} is not in policy; refusing")
        return self.tools[name]

    def check_flow(self, tool_name: str, sources: FrozenSet[str], *, where: str = "args") -> None:
        tp = self.tool(tool_name)
        if not tp.allows(sources):
            bad = sorted(sources - tp.allow_sources)
            raise PolicyViolation(
                f"{tool_name}: data tagged {bad} may not flow into this tool ({where}). "
                f"Allowed sources: {sorted(tp.allow_sources)}"
            )

    def high_risk_tools(self) -> set:
        return {n for n, t in self.tools.items() if t.risk == "high"}

    def check_path(self, p: str | os.PathLike, *, write: bool = False) -> Path:
        """Resolve symlinks, require a root, then apply the deny list (which wins over roots)."""
        path = Path(p).expanduser().resolve()
        roots = self.writable_roots if write else self.readable_roots + self.writable_roots
        if not any(_is_under(path, root) for root in roots):
            mode = "writable" if write else "readable"
            raise PolicyViolation(f"path {path} is outside the {mode} roots {[str(r) for r in roots]}")
        for denied in self.denied_paths:
            if _is_under(path, denied):
                raise PolicyViolation(f"path {path} is under denied path {denied}")
        for pattern in self.denied_globs:
            if fnmatch.fnmatch(str(path), pattern) or fnmatch.fnmatch(path.name, pattern):
                raise PolicyViolation(f"path {path} matches denied pattern {pattern!r}")
        return path

    def check_host(self, host: str) -> None:
        """Refuse a never-visit host (and its subdomains)."""
        host = (host or "").lower().rstrip(".")
        if not host:
            raise PolicyViolation("URL has no host")
        for blocked in self.browser_blocked_hosts:
            if host == blocked or host.endswith("." + blocked):
                raise PolicyViolation(f"{host} is on the never-visit list (browser.blocked_hosts)")

    def check_app(self, name: str, bundle_id: str = "") -> None:
        """Refuse desktop control of a never-touch app (terminals, password managers, the agent itself...)."""
        n, b = (name or "").lower(), (bundle_id or "").lower()
        for blocked in self.desktop_blocked_apps:
            if blocked in (n, b) or (blocked.startswith("com.") and b.startswith(blocked)):
                raise PolicyViolation(f"{name or bundle_id} is on the never-control list (desktop.blocked_apps)")

    def check_shell_program(self, program: str) -> None:
        """Only allowlisted programs, by bare name or from the system bin folders (no lookalikes in ~)."""
        if not self.shell_allowed_programs:
            return
        name = os.path.basename(program)
        if "/" in program and not program.startswith(("/bin/", "/usr/bin/", "/usr/sbin/", "/sbin/")):
            raise PolicyViolation(f"program {program!r} is not a system program")
        if name not in self.shell_allowed_programs:
            raise PolicyViolation(f"program {name!r} is not in shell.allowed_programs")

    def check_shell(self, cmd: str) -> None:
        low = cmd.lower()
        for pat in self.shell_denied_patterns:
            if pat.lower() in low:
                raise PolicyViolation(f"shell command matches denied pattern {pat!r}")

    # ---- network -----------------------------------------------------------------------------
    def effective_ollama_url(self) -> str:
        """The Ollama endpoint actually used. The `OLLAMA_URL` env var overrides policy.yaml, but a
        non-loopback address is refused unless network.allow_remote_ollama is true."""
        url = (os.environ.get("OLLAMA_URL") or self.ollama_url).rstrip("/")
        if not is_local_url(url) and not self.allow_remote_ollama:
            raise PolicyViolation(
                f"Ollama at {url} is not on this machine; set network.allow_remote_ollama: true in policy.yaml to allow it")
        return url

    def external_services(self) -> List[dict]:
        """Everything that may leave this machine, for the settings screen and the audit trail."""
        url = (os.environ.get("OLLAMA_URL") or self.ollama_url).rstrip("/")
        return [
            {"service": "ollama", "enabled": True, "local": is_local_url(url), "endpoints": [url],
             "note": "Local language models. The only model path in the code; no cloud API exists."},
            {"service": "gmail", "enabled": self.gmail_enabled, "local": False,
             "endpoints": [f"imaps://{self.gmail_imap_host}:993", f"smtps://{self.gmail_smtp_host}:465"],
             "note": "IMAP/SMTP with an app password from the secrets broker. Off: the gmail.* tools do not exist."},
            {"service": "browser", "enabled": self.browser_enabled, "local": False,
             "endpoints": ["Google Chrome (your profiles and sign-ins)"],
             "note": "First visit to each new website asks you; never-visit list: "
                     + (", ".join(self.browser_blocked_hosts) or "empty") + ". Off: no browser.* tools."},
        ]
