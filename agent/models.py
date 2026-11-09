"""Which model does the work: the fast one by default, the careful one when the rules say so.

Escalation is decided by code (see `escalation_reasons` in server.py), never by a model. Only one
model is in memory at a time: the fast model is unloaded before the careful one starts, and the
careful one is stopped as soon as its job is done. If there is not enough free memory, the work is
refused with a clear message instead of risking swap.
"""
from __future__ import annotations

import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional, Protocol

import httpx

from .llm import LLM, OpenAICompatLLM, unload
from .memory import free_gb as _free_gb
from .memory import swap_free_gb as _swap_free


class LowMemory(Exception):
    def __init__(self, have_gb: float, need_gb: float, what: str = "the careful model"):
        self.have_gb, self.need_gb = have_gb, need_gb
        super().__init__(f"Not enough free memory for {what}: {have_gb:.1f} GB free, {need_gb:.1f} GB needed. "
                         "Close some apps and try again.")


class StrongUnavailable(Exception):
    pass


class StrongBackend(Protocol):
    name: str
    need_gb: float

    def start(self) -> LLM: ...
    def stop(self) -> None: ...


class TurboFieldfareBackend:
    """Starts TurboFieldfare's loopback server on demand and stops it afterwards."""

    def __init__(self, binary: str, model_dir: str, port: int = 8080, max_context: int = 8192,
                 need_gb: float = 4.0, model_id: str = "gemma-4-26b-a4b-it", startup_timeout_s: float = 180,
                 timeout_s: float = 240):
        self.binary = str(Path(binary).expanduser())
        self.model_dir = str(Path(model_dir).expanduser())
        self.port, self.max_context, self.need_gb = port, max_context, need_gb
        self.model_id, self.startup_timeout_s, self.timeout_s = model_id, startup_timeout_s, timeout_s
        self.name = f"{model_id} (TurboFieldfare)"
        self._proc: Optional[subprocess.Popen] = None

    def _healthy(self) -> bool:
        try:
            return httpx.get(f"http://127.0.0.1:{self.port}/health", timeout=2).status_code == 200
        except Exception:
            return False

    def start(self) -> LLM:
        if not Path(self.binary).exists() or not Path(self.model_dir).exists():
            raise StrongUnavailable(f"careful model not installed ({self.binary})")
        if not self._healthy():
            self._proc = subprocess.Popen(
                [self.binary, "--model", self.model_dir, "--port", str(self.port), "--max-context", str(self.max_context)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.time() + self.startup_timeout_s
            while not self._healthy():
                if self._proc.poll() is not None or time.time() > deadline:
                    self.stop()
                    raise StrongUnavailable("careful model failed to start")
                time.sleep(1)
        return OpenAICompatLLM(self.model_id, url=f"http://127.0.0.1:{self.port}/v1", timeout_s=self.timeout_s)

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


class ModelManager:
    def __init__(self, fast: LLM, fast_name: str, strong: Optional[StrongBackend] = None,
                 free_gb: Callable[[], float] = _free_gb, unload_fast: Optional[Callable[[], None]] = None,
                 swap_free_gb: Optional[Callable[[], float]] = None, min_swap_free_gb: float = 2.0):
        self.fast, self.fast_name, self.strong_backend = fast, fast_name, strong
        self.free_gb = free_gb
        # Streaming a big model from SSD pushes other apps into swap; refuse when swap is already tight.
        self.swap_free_gb = swap_free_gb or (_swap_free if free_gb is _free_gb else (lambda: 99.0))
        self.min_swap_free_gb = min_swap_free_gb
        self.unload_fast = unload_fast or (lambda: unload(fast_name))

    @property
    def strong_name(self) -> Optional[str]:
        return self.strong_backend.name if self.strong_backend else None

    @contextmanager
    def strong(self) -> Iterator[LLM]:
        """The careful model, for the duration of the block. Raises LowMemory or StrongUnavailable."""
        if self.strong_backend is None:
            raise StrongUnavailable("no careful model is configured (policy.yaml → models.strong)")
        self.unload_fast()
        have = self.free_gb()
        if have < self.strong_backend.need_gb:
            raise LowMemory(have, self.strong_backend.need_gb)
        swap = self.swap_free_gb()
        if swap < self.min_swap_free_gb:
            raise LowMemory(have, self.strong_backend.need_gb,
                            what=f"the careful model (swap nearly full: {swap:.1f} GB free, {self.min_swap_free_gb:.1f} GB needed)")
        llm = self.strong_backend.start()
        try:
            yield llm
        finally:
            self.strong_backend.stop()


def strong_from_config(cfg: Optional[dict]) -> Optional[StrongBackend]:
    if not cfg or os.environ.get("AGENT_NO_STRONG") == "1":
        return None
    if cfg.get("backend") == "turbofieldfare":
        return TurboFieldfareBackend(cfg["binary"], cfg["model_dir"], port=int(cfg.get("port", 8080)),
                                     max_context=int(cfg.get("max_context", 8192)),
                                     need_gb=float(cfg.get("min_free_gb", 4.0)))
    raise ValueError(f"unknown strong model backend {cfg.get('backend')!r}")
