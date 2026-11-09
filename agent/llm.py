"""Model clients. Only Ollama (local) is implemented; nothing here talks to a cloud API."""
from __future__ import annotations

import json
import os
from typing import Protocol

import httpx

# Default local models (override with env). The planner needs a capable model; the reader only fills schemas.
PLANNER_MODEL = os.environ.get("PLANNER_MODEL", "qwen3.5:4b")
READER_MODEL = os.environ.get("READER_MODEL", "qwen3.5:4b")


class LLM(Protocol):
    def complete_json(self, system: str, user: str) -> str: ...


class OllamaLLM:
    """Talks to the local Ollama server. The URL comes from policy.yaml (or the OLLAMA_URL env var)."""

    def __init__(self, model: str, url: str | None = None, timeout_s: float = 300,
                 num_ctx: int = int(os.environ.get("AGENT_NUM_CTX", "8192")),
                 keep_alive: str = os.environ.get("AGENT_KEEP_ALIVE", "5m"), num_predict: int = 2048):
        # num_ctx drives the KV cache: 16K context roughly adds 2-3 GB to an 8B model. 8K fits this
        # agent's prompts. num_predict caps the reply so a rambling model cannot run for hours.
        self.model = model
        self.url = (url or os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.timeout_s = timeout_s
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.num_predict = num_predict

    def complete_json(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "keep_alive": self.keep_alive,
            # Hidden "thinking" makes small models slow and memory-hungry for no gain on JSON-only answers.
            "think": os.environ.get("AGENT_THINK", "false").lower() == "true",
            "options": {"temperature": 0, "num_ctx": self.num_ctx, "num_predict": self.num_predict},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # total wall-clock limit, not just per-read: a stalled or swapping server is abandoned
        try:
            data = self._post(payload)
        except httpx.HTTPStatusError as e:
            # Ollama's GPU backend can get stuck after memory pressure ("failed to initialize the Metal
            # library"). Restarting the Ollama app clears it; do that once, then retry.
            if e.response.status_code != 500 or not restart_ollama():
                raise
            data = self._post(payload)
        return data["message"]["content"]

    def _post(self, payload: dict) -> dict:
        with httpx.Client(timeout=httpx.Timeout(self.timeout_s, read=self.timeout_s)) as client:
            r = client.post(f"{self.url}/api/chat", json=payload)
            r.raise_for_status()
            return r.json()


class OpenAICompatLLM:
    """A local OpenAI-compatible server (e.g. TurboFieldfare on 127.0.0.1:8080). Loopback only."""

    def __init__(self, model: str, url: str = "http://127.0.0.1:8080/v1", timeout_s: float = 300,
                 max_tokens: int = 2048):
        from urllib.parse import urlparse
        if (urlparse(url).hostname or "") not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"refusing non-local model server {url}")
        self.model, self.url, self.timeout_s, self.max_tokens = model, url.rstrip("/"), timeout_s, max_tokens

    def complete_json(self, system: str, user: str) -> str:
        # No JSON mode on these servers: stop at the end of the first fenced block, so a model that
        # likes to add prose or a "corrected" second copy cannot ramble past the plan.
        payload = {"model": self.model, "temperature": 0, "max_tokens": self.max_tokens, "stop": ["\n```"],
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        with httpx.Client(timeout=httpx.Timeout(self.timeout_s, read=self.timeout_s)) as client:
            r = client.post(f"{self.url}/chat/completions", json=payload)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]


def extract_json(text: str) -> dict:
    """Parse the first JSON object in `text`, tolerating ```json fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in model output")
    obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("model output is not a JSON object")
    return obj


def unload(model: str, url: str | None = None) -> None:
    """Ask Ollama to drop a model from memory now."""
    url = (url or os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
    try:
        httpx.post(f"{url}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
    except Exception:
        pass


_last_restart = 0.0


def restart_ollama() -> bool:
    """Quit and reopen the local Ollama app (at most once every 2 minutes). True if it came back."""
    import subprocess
    import time
    global _last_restart
    if time.time() - _last_restart < 120 or not os.path.isdir("/Applications/Ollama.app"):
        return False
    _last_restart = time.time()
    subprocess.run(["osascript", "-e", 'quit app "Ollama"'], capture_output=True, timeout=20)
    time.sleep(4)
    subprocess.run(["open", "-a", "Ollama"], capture_output=True, timeout=20)
    for _ in range(30):
        try:
            if httpx.get("http://127.0.0.1:11434/api/tags", timeout=2).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False
