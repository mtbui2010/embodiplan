# embodiplan/base.py
"""
Shared base class, data structures, and LLM backend abstraction for all embodiplan methods.

Supported backends:
  - "ollama"  : local Ollama server (default)
  - "openai"  : OpenAI API (GPT-4o, GPT-4o-mini, o1, ...)
  - "gemini"  : Google Gemini API (gemini-2.5-flash, gemini-2.0-flash, ...)
"""

from __future__ import annotations

import json
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# ── Defaults ──────────────────────────────────────────────────────────
# DEFAULT_HOST     = "http://localhost:11434"
DEFAULT_HOST     = "http://ollama.aistations.org"
DEFAULT_MODEL    = "llama3.3:70b"
DEFAULT_BACKEND  = "ollama"

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# ── Model presets per provider ─────────────────────────────────────────
PROVIDER_MODELS: dict[str, list[str]] = {
    "gemini": [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
    ],
    "ollama": [
        "llama3.3:70b", "llama3.2", "exaone3.5",
        "qwen2.5:3b", "qwen2.5:7b", "qwen2.5:14b",
        "mistral", "mistral-nemo",
        "gemma2:2b", "gemma2:9b",
        "phi3.5", "deepseek-r1:7b",
    ],
    "openai": [
        "gpt-4o", "gpt-4o-mini",
        "gpt-4-turbo", "gpt-4",
        "o1", "o1-mini", "o3-mini",
    ],
}

ROBOT_ACTIONS = [
    "MoveTo", "Find", "Pick", "Place", "PutIn",
    "Open", "Close", "TurnOn", "TurnOff",
    "Wash", "Sit", "LieOn", "Serve", "Wait",
]

# Optional cross-domain actions (e.g. ALFWorld's heat/cool/clean), opt-in via the
# EMBODIPLAN_EXTRA_ACTIONS env var so the DEFAULT vocabulary — and therefore every
# existing AI2-THOR prompt and cached result — stays byte-identical. The verifier
# imports ROBOT_ACTIONS, so it stays in sync automatically. Set the var BEFORE
# importing embodiplan, e.g. EMBODIPLAN_EXTRA_ACTIONS="Heat,Cool,Clean".
# PYPLANNER_EXTRA_ACTIONS is the pre-rename name, still honoured.
_extra = os.environ.get("EMBODIPLAN_EXTRA_ACTIONS") or os.environ.get("PYPLANNER_EXTRA_ACTIONS", "")
for _a in (x.strip() for x in _extra.split(",")):
    if _a and _a not in ROBOT_ACTIONS:
        ROBOT_ACTIONS.append(_a)

ACTIONS_STR = "\n".join(f"  {a}" for a in ROBOT_ACTIONS)

STEP_SCHEMA = """\
Each step is a JSON object with exactly two fields:
  "action" : one action from the list above (exact spelling)
  "object" : target in CamelCase, e.g. Apple, CoffeeMachine, Kitchen, DiningTable

Action rules:
- MoveTo <room|furniture>  : navigate to a room (Kitchen, LivingRoom, Bedroom, Bathroom)
                             or furniture/receptacle in current room (DiningTable, Sofa).
                             Updates 'arrived' in robot state.
                             Use MoveTo for ALL navigation — rooms AND furniture.
- Find <object>            : find a PICKUPABLE object in the current room (e.g. Apple, Mug,
                             RemoteControl, Towel, SoapBottle). DO NOT use Find for rooms
                             or furniture — use MoveTo for those.
                             Updates 'found' in robot state.
- Pick                     : assert 'found' is set, then pick it up. object field ignored.
- Open/Close/TurnOn/TurnOff: assert 'found' is set, then act on it. object field ignored.
                             Note: use Find before Open/Close only for containers (Fridge,
                             Cabinet, Drawer, Microwave) that hold an object to retrieve.
- Place <receptacle>       : assert 'holding' and 'arrived', then place held object.
                             object = receptacle name (must match 'arrived').

Container rule (REQUIRED even if not stated in the task):
  If the target object is inside a container (Fridge, Cabinet, Drawer, Microwave, etc.),
  you MUST navigate to the container, open it, find the object inside, pick it, then close.
  Pattern: MoveTo <container> → Open <container> → Find <object> → Pick → Close <container>

Typical sequences:
  Simple grab and place:
    MoveTo Kitchen → Find Apple → Pick → MoveTo LivingRoom → MoveTo DiningTable → Place DiningTable
  Object inside container (e.g. apple in fridge):
    MoveTo Kitchen → MoveTo Fridge → Open Fridge → Find Apple → Pick → Close Fridge → MoveTo LivingRoom → MoveTo DiningTable → Place DiningTable"""

# Schema note for the opt-in cross-domain actions — appended ONLY when they are
# enabled, so the default THOR schema string is unchanged.
if any(_x in ROBOT_ACTIONS for _x in ("Heat", "Cool", "Clean")):
    STEP_SCHEMA += """

Cross-domain object-treatment actions (use ONLY when the task requires them):
- Heat <object>            : heat the object you are HOLDING using an appliance
                             (e.g. Microwave). Requires 'holding' the object.
- Cool <object>            : cool the object you are HOLDING using an appliance
                             (e.g. Fridge). Requires 'holding' the object.
- Clean <object>           : clean/rinse the object you are HOLDING at a sink.
                             Requires 'holding' the object.
  Typical: ... Find <obj> → Pick → Heat <obj> → MoveTo <recep> → Place <recep>"""

JSON_EXAMPLE = """\
{"steps": [
  {"action": "MoveTo", "object": "Kitchen"},
  {"action": "Find",   "object": "Apple"},
  {"action": "Pick",   "object": "Apple"},
  {"action": "MoveTo", "object": "DiningTable"},
  {"action": "Place",  "object": "DiningTable"}
]}

Example with container:
{"steps": [
  {"action": "MoveTo", "object": "Kitchen"},
  {"action": "Find",   "object": "Fridge"},
  {"action": "Open",   "object": "Fridge"},
  {"action": "Find",   "object": "Apple"},
  {"action": "Pick",   "object": "Apple"},
  {"action": "Find",   "object": "Fridge"},
  {"action": "Close",  "object": "Fridge"},
  {"action": "MoveTo", "object": "DiningTable"},
  {"action": "Place",  "object": "DiningTable"}
]}"""


# ══════════════════════════════════════════════════════════════════════
# PlanMetrics
# ══════════════════════════════════════════════════════════════════════
@dataclass
class PlanMetrics:
    """Metrics collected for every generate_plan / replan call."""
    method:        str   = ""
    model:         str   = ""
    backend:       str   = ""
    latency_s:     float = 0.0
    llm_calls:     int   = 0
    input_tokens:  int   = 0
    output_tokens: int   = 0
    num_steps:     int   = 0
    parse_ok:      bool  = True
    notes:         str   = ""
    extra:         dict  = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def tokens_per_step(self) -> float:
        return self.total_tokens / self.num_steps if self.num_steps else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "method":          self.method,
            "model":           self.model,
            "backend":         self.backend,
            "latency_s":       round(self.latency_s, 3),
            "llm_calls":       self.llm_calls,
            "input_tokens":    self.input_tokens,
            "output_tokens":   self.output_tokens,
            "total_tokens":    self.total_tokens,
            "num_steps":       self.num_steps,
            "tokens_per_step": round(self.tokens_per_step, 1),
            "parse_ok":        self.parse_ok,
            "notes":           self.notes,
        }


# ══════════════════════════════════════════════════════════════════════
# LLMBackend  — unified _chat() for Ollama / OpenAI / Anthropic
# ══════════════════════════════════════════════════════════════════════
class LLMBackend:
    """
    Thin wrapper that exposes a single chat() method regardless of provider.

    Args:
        provider:    "ollama" | "openai" | "anthropic"
        model:       model string for the chosen provider
        host:        Ollama host URL (ignored for openai/anthropic)
        api_key:     API key for openai/anthropic (falls back to env vars)
        temperature: default sampling temperature
    """

    def __init__(
        self,
        provider:    str   = DEFAULT_BACKEND,
        model:       str   = DEFAULT_MODEL,
        host:        str   = DEFAULT_HOST,
        api_key:     str   = "",
        temperature: float = 0.2,
        seed:        int | None = None,
    ):
        self.provider    = provider.lower()
        self.model       = model
        self.temperature = temperature
        # Optional deterministic sampling seed. When set, it is forwarded to
        # the Ollama `options` block so that temperature>0 runs are reproducible
        # per-seed (used by the multi-seed robustness study). Default None keeps
        # the legacy request body byte-identical.
        self.seed        = seed

        # Normalize host: ensure scheme is present
        # "ollama.aistations.org"        → "https://ollama.aistations.org"
        # "localhost:11434"               → "http://localhost:11434"
        # "http://localhost:11434"        → "http://localhost:11434"  (unchanged)
        # "https://ollama.aistations.org" → "https://ollama.aistations.org" (unchanged)
        h = host.strip().rstrip("/")
        if not h.startswith("http://") and not h.startswith("https://"):
            # localhost / 127.0.0.1 / 192.168.x.x → http, everything else → https
            is_local = (
                h.startswith("localhost") or
                h.startswith("127.") or
                h.startswith("192.168.") or
                h.startswith("10.") or
                h.startswith("172.")
            )
            h = ("http://" if is_local else "https://") + h
        self.host = h

        # Resolve API key: arg > env var
        if self.provider == "openai":
            self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        elif self.provider == "gemini":
            self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        else:
            self.api_key = ""  # Ollama needs no key

        # Lazy-init Ollama client only when needed
        self._ollama_client = None

    def _get_ollama_client(self):
        if self._ollama_client is None:
            import ollama as _ollama
            self._ollama_client = _ollama.Client(host=self.host)
        return self._ollama_client

    def chat(
        self,
        messages:    list[dict],
        temperature: float | None = None,
    ) -> tuple[str, int, int]:
        """
        Send messages and return (content, input_tokens, output_tokens).
        temperature=None uses the instance default.
        """
        temp = temperature if temperature is not None else self.temperature

        if self.provider == "ollama":
            return self._chat_ollama(messages, temp)
        elif self.provider == "openai":
            return self._chat_openai(messages, temp)
        elif self.provider == "gemini":
            return self._chat_gemini(messages, temp)
        else:
            raise ValueError(f"Unknown provider '{self.provider}'. Use: ollama, openai, gemini")

    # ── Ollama ──
    @staticmethod
    def _parse_ollama_response(raw_text: str) -> tuple[str, dict]:
        """
        Robustly parse Ollama /api/chat response handling all shapes:
          1. Normal stream=False  → single JSON object
          2. Cloudflare leaks chunked stream → multiple JSON lines joined
          3. Empty content        → model returned nothing
          4. Error                → {"error": "..."}

        Returns (content, data_dict).
        """
        import json as _json
        raw_text = raw_text.strip()

        # Shape 1: standard single JSON object
        try:
            data = _json.loads(raw_text)
            if "error" in data:
                raise RuntimeError(f"Ollama error: {data['error']}")
            content = data.get("message", {}).get("content", "")
            return content, data
        except _json.JSONDecodeError:
            pass

        # Shape 2: Cloudflare leaked chunked stream
        # Multiple newline-delimited JSON objects — concatenate content,
        # take token counts from the last chunk with done=true
        lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        full_content = ""
        last_data: dict = {}
        for line in lines:
            try:
                chunk = _json.loads(line)
                full_content += chunk.get("message", {}).get("content", "")
                if chunk.get("done"):
                    last_data = chunk
            except _json.JSONDecodeError:
                continue

        if full_content or last_data:
            last_data.setdefault("message", {})["content"] = full_content
            return full_content, last_data

        raise RuntimeError(
            f"Cannot parse Ollama response "
            f"(HTTP 200 but unrecognised body). "
            f"First 300 chars: {raw_text[:300]!r}"
        )

    def _chat_ollama(self, messages, temperature):
        """
        Call Ollama with stream=False via plain requests (not the ollama
        Python client) to avoid Cloudflare Tunnel buffering / SSE issues.

        Handles all known response shapes including Cloudflare leaking
        chunked stream bodies as concatenated NDJSON.
        """
        import requests as _req

        url  = self.host.rstrip("/") + "/api/chat"
        options = {"temperature": temperature}
        if self.seed is not None:
            options["seed"] = self.seed
        # Same context size as the other clients of this server (OLLAMA_NUM_CTX): a request with another num_ctx
        # makes Ollama reload the model, and without one qwen3-vl loads its 262k default context.
        if os.getenv("OLLAMA_NUM_CTX"):
            options["num_ctx"] = int(os.getenv("OLLAMA_NUM_CTX"))
        # Opt-in streaming (OLLAMA_STREAM=1): keep the HTTP connection fed with
        # tokens so a reverse-proxy IDLE timeout (e.g. ~125s on some gateways)
        # cannot cut a long generation to an empty body. Needed for very large
        # models (e.g. llama3.3:70b) whose single plan can exceed the idle limit.
        if os.getenv("OLLAMA_STREAM", "0") == "1":
            return self._chat_ollama_stream(url, messages, options)

        body = {
            "model":    self.model,
            "messages": messages,
            "stream":   False,      # single JSON response, no SSE
            "options":  options,
        }

        try:
            # Timeout is configurable via OLLAMA_TIMEOUT (seconds) so that a
            # slow-but-valid generation under load is not silently truncated to
            # an empty plan. Default 120 preserves legacy behaviour.
            _timeout = float(os.getenv("OLLAMA_TIMEOUT", "120"))
            resp = _req.post(url, json=body, timeout=_timeout)
        except _req.exceptions.ConnectionError as e:
            raise ConnectionRefusedError(
                f"Cannot reach Ollama at {self.host}\n"
                f"  Error: {e}\n"
                f"  Check: OLLAMA_HOST=0.0.0.0:11434 and OLLAMA_ORIGINS=* "
                f"are set in the container"
            ) from e

        # Surface HTTP errors with useful context
        if resp.status_code != 200:
            raise RuntimeError(
                f"Ollama returned HTTP {resp.status_code}\n"
                f"  URL: {url}\n"
                f"  Body: {resp.text[:200]}"
            )

        raw      = resp.text
        content, data = self._parse_ollama_response(raw)

        if not content:
            # Empty response — log raw for debugging and raise clearly
            raise RuntimeError(
                f"Ollama returned empty content.\n"
                f"  Model   : {self.model}\n"
                f"  Host    : {self.host}\n"
                f"  Raw resp: {raw[:400]!r}\n"
                f"  Hint    : check OLLAMA_ORIGINS=* and "
                f"HTTP Host Header in Cloudflare tunnel config"
            )

        in_tok  = data.get("prompt_eval_count") or _approx_tokens(
            " ".join(m.get("content", "") for m in messages)
        )
        out_tok = data.get("eval_count") or _approx_tokens(content)
        return content, in_tok, out_tok

    def _chat_ollama_stream(self, url, messages, options):
        """Streaming /api/chat: accumulate NDJSON chunks. Continuous token flow
        keeps a proxy idle-timeout from truncating long (e.g. 70B) generations."""
        import requests as _req, json as _json
        body = {"model": self.model, "messages": messages, "stream": True, "options": options}
        _timeout = float(os.getenv("OLLAMA_TIMEOUT", "120"))
        content, data = "", {}
        with _req.post(url, json=body, stream=True, timeout=_timeout) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"Ollama HTTP {resp.status_code} (stream)\n  URL: {url}\n  Body: {resp.text[:200]}")
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    d = _json.loads(line)
                except ValueError:
                    continue
                content += d.get("message", {}).get("content", "")
                if d.get("done"):
                    data = d
        if not content:
            raise RuntimeError(f"Ollama returned empty content (stream).\n  Model: {self.model}\n  Host: {self.host}")
        in_tok  = data.get("prompt_eval_count") or _approx_tokens(
            " ".join(m.get("content", "") for m in messages))
        out_tok = data.get("eval_count") or _approx_tokens(content)
        return content, in_tok, out_tok

    # ── OpenAI ──
    def _chat_openai(self, messages, temperature):
        import requests as _req
        if not self.api_key:
            raise ValueError("OpenAI API key not set. Pass api_key= or set OPENAI_API_KEY env var.")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        body    = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": 2048}
        resp    = _req.post(OPENAI_API_URL, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        data    = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage   = data.get("usage", {})
        return content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)

    # ── Gemini ──
    def _chat_gemini(self, messages, temperature):
        import requests as _req
        if not self.api_key:
            raise ValueError("Gemini API key not set. Pass api_key= or set GEMINI_API_KEY env var.")
        # Extract system message; map roles user/assistant → user/model
        system_text = ""
        contents    = []
        for m in messages:
            if m["role"] == "system":
                system_text = m["content"]
            else:
                role = "model" if m["role"] == "assistant" else "user"
                contents.append({"role": role, "parts": [{"text": m["content"]}]})

        body: dict = {
            "contents":        contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 2048},
        }
        if system_text:
            body["system_instruction"] = {"parts": [{"text": system_text}]}

        url  = GEMINI_API_URL.format(model=self.model) + f"?key={self.api_key}"
        resp = _req.post(url, json=body, timeout=60)
        resp.raise_for_status()
        data    = resp.json()
        content = data["candidates"][0]["content"]["parts"][0]["text"]
        usage   = data.get("usageMetadata", {})
        return content, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)


# ══════════════════════════════════════════════════════════════════════
# Utilities
# ══════════════════════════════════════════════════════════════════════
def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _text_fields(steps) -> list[dict]:
    """Steps with ``action`` / ``object`` / ``target`` as text: a model may write a number
    (``{"action": "Wait", "object": 5}``), which every ``.strip()`` downstream choked on.
    Items that are not dicts are dropped; None stays absent."""
    out = []
    for s in steps if isinstance(steps, list) else []:
        if not isinstance(s, dict):
            continue
        s = dict(s)
        for k in ("action", "object", "target"):
            if k in s and s[k] is not None and not isinstance(s[k], str):
                s[k] = str(s[k])
        out.append(s)
    return out


def parse_steps(raw: str) -> list[dict]:
    """Robustly parse LLM output into a list of step dicts (fields as text, ``_text_fields``)."""
    return _text_fields(_parse_steps(raw))


def _parse_steps(raw) -> list:
    raw = re.sub(r"```json|```", "", str(raw or "")).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data.get("steps", [])
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[.*?\]", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    steps = []
    for m in re.finditer(r"\{[^{}]+\}", raw):
        try:
            step = json.loads(m.group())
            if "action" in step:
                steps.append(step)
        except json.JSONDecodeError:
            pass
    return steps


# ══════════════════════════════════════════════════════════════════════
# BasePlanner
# ══════════════════════════════════════════════════════════════════════
class BasePlanner(ABC):
    """
    Abstract base for all embodiplan methods.

    Accepts any supported backend (ollama / openai / anthropic) via LLMBackend.
    All subclasses use self._chat() — backend-agnostic.
    """

    name: str        = "base"
    description: str = ""

    def __init__(
        self,
        host:     str = DEFAULT_HOST,
        model:    str = DEFAULT_MODEL,
        provider: str = DEFAULT_BACKEND,
        api_key:  str = "",
        **kwargs,
    ):
        self.host     = host
        self.model    = model
        self.provider = provider
        self._backend = LLMBackend(provider=provider, model=model, host=host, api_key=api_key)

    def _chat(self, messages: list[dict], temperature: float = 0.2) -> tuple[str, int, int]:
        """
        Call the configured LLM backend. Returns (content, in_tokens, out_tokens).
        Connection/auth errors are re-raised so evaluate_sample() can record them
        properly instead of silently producing empty plans.
        """
        try:
            return self._backend.chat(messages, temperature=temperature)
        except Exception as e:
            err_str = str(e).lower()
            # Re-raise connection/auth errors — these indicate a config problem,
            # not a recoverable parse failure, and should be visible immediately.
            FATAL_KEYWORDS = [
                "connection refused", "cannot connect", "timed out", "timeout",
                "name or service not known", "no route to host",
                "network is unreachable", "remotedisconnected", "connectionreset",
                "401", "unauthorized", "api key", "invalid key",
                "http error 4", "http error 5",
            ]
            if any(kw in err_str for kw in FATAL_KEYWORDS):
                raise  # propagate — do not silently return empty plan
            raise  # always propagate; planners' except blocks store in metrics.notes

    def _make_metrics(self, **kwargs) -> PlanMetrics:
        """Helper to create a PlanMetrics pre-filled with method/model/backend."""
        return PlanMetrics(
            method  = self.name,
            model   = self.model,
            backend = self.provider,
            **kwargs,
        )

    @abstractmethod
    def generate_plan(
        self,
        task: str,
        obs: str,
        visible_objects: list[str],
    ) -> tuple[list[dict], PlanMetrics]:
        ...

    @abstractmethod
    def replan(
        self,
        task: str,
        completed: list[dict],
        failed_step: dict,
        failure_reason: str,
        obs: str,
        visible_objects: list[str],
    ) -> tuple[list[dict], PlanMetrics]:
        ...

    # ── Shared prompt helpers ──
    def _context_str(self, task: str, obs: str, visible_objects: list[str]) -> str:
        obj_str = ", ".join(visible_objects[:30]) if visible_objects else "none visible yet"
        return (
            f"User request: {task}\n\n"
            f"Current robot observation:\n{obs}\n\n"
            f"Objects currently visible:\n{obj_str}"
        )

    def _replan_context(self, task: str, completed: list[dict], failed_step: dict,
                        failure_reason: str, obs: str, visible_objects: list[str]) -> str:
        completed_str = "\n".join(
            f"  {i+1}. {s.get('action','')} {s.get('object','')}"
            + (f" → {s['target']}" if s.get("target") else "")
            for i, s in enumerate(completed)
        ) or "  (none yet)"
        obj_str = ", ".join(visible_objects[:30]) if visible_objects else "none visible"
        return (
            f"User request: {task}\n\nSteps already completed:\n{completed_str}\n\n"
            f"Failed step:\n  action : {failed_step.get('action','')}\n"
            f"  object : {failed_step.get('object','')}\n  target : {failed_step.get('target','')}\n"
            f"  reason : {failure_reason}\n\nCurrent observation:\n{obs}\n\nVisible objects:\n{obj_str}\n\n"
            "Generate ONLY the remaining steps. Do NOT repeat completed steps. Fix the root cause."
        )