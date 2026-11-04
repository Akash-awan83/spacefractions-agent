"""DeepSeek API client (OpenAI-compatible Chat Completions) + cost tracking.

Built on the standard library (urllib) on purpose: no SDK means a smaller .exe
and fewer PyInstaller surprises.

Two DeepSeek rules this module enforces (from the official thinking-mode guide):
  * Thinking mode is on by default and does not accept temperature/top_p.
  * When a request carries `tools`, every later request must send each assistant
    message back WITH its `reasoning_content`, or the API answers HTTP 400.
    `clean_assistant_message` keeps that field for exactly this reason.
"""
from __future__ import annotations

import json
import socket
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    """Any failure talking to the model."""


class AuthError(LLMError):
    """The API key was rejected (HTTP 401)."""


class BalanceError(LLMError):
    """The DeepSeek account has no balance left (HTTP 402)."""


# --------------------------------------------------------------------------- cost
def is_peak(pricing: dict, now: datetime | None = None) -> bool:
    """DeepSeek peak hours: Mon-Fri, inside one of the UTC windows in pricing['peak_hours_utc']."""
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    if now.weekday() >= 5:
        return False
    return any(a <= now.hour < b for a, b in pricing.get("peak_hours_utc", []))


PEAK_WARNING = ("DeepSeek PEAK hours now: prices are DOUBLE. Peak = Mon-Fri 01:00-04:00 and 06:00-10:00 UTC (Pakistan: 06:00-09:00 and 11:00-15:00). Weekends are always off-peak.")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CostTracker:
    """Adds up token usage and converts it to dollars.

    DeepSeek reports cache hits and misses separately. A cache hit (a prompt
    prefix the server has seen before) costs about 3% of a miss, which is why the
    agent keeps its system prompt and architecture JSON identical on every call.
    Each call is priced at the moment it happens (peak hours cost double)."""
    pricing: dict
    clock: Callable[[], datetime] = _utc_now
    total_cost: float = 0.0
    peak_calls: int = 0
    calls: int = 0
    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    history: list = field(default_factory=list)

    def add(self, usage: dict) -> dict:
        usage = usage or {}
        prompt = int(usage.get("prompt_tokens", 0))
        hit = int(usage.get("prompt_cache_hit_tokens", 0))
        miss = int(usage.get("prompt_cache_miss_tokens", prompt - hit))
        out = int(usage.get("completion_tokens", 0))
        reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0))
        peak = is_peak(self.pricing, self.clock())
        cost = self._price(hit, miss, out) * (float(self.pricing.get("peak_multiplier", 1.0)) if peak else 1.0)
        self.total_cost += cost
        self.peak_calls += int(peak)
        self.calls += 1
        self.prompt_tokens += prompt
        self.cache_hit_tokens += hit
        self.cache_miss_tokens += miss
        self.completion_tokens += out
        self.reasoning_tokens += reasoning
        delta = {"prompt": prompt, "hit": hit, "miss": miss, "output": out,
                 "reasoning": reasoning, "cost_usd": cost, "peak": peak}
        self.history.append(delta)
        return delta

    def _price(self, hit: int, miss: int, out: int) -> float:
        p = self.pricing
        return (hit * p["cache_hit_input"] + miss * p["cache_miss_input"] + out * p["output"]) / 1_000_000

    @property
    def cost_usd(self) -> float:
        return self.total_cost

    @property
    def max_step_cost(self) -> float:
        """The most expensive single call so far: the guard keeps this much in reserve."""
        return max((h["cost_usd"] for h in self.history), default=0.0)

    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_hit_tokens + self.cache_miss_tokens
        return self.cache_hit_tokens / total if total else 0.0

    def summary(self) -> dict:
        return {"calls": self.calls, "prompt_tokens": self.prompt_tokens,
                "cache_hit_tokens": self.cache_hit_tokens, "cache_miss_tokens": self.cache_miss_tokens,
                "cache_hit_rate": round(self.cache_hit_rate, 4),
                "completion_tokens": self.completion_tokens, "reasoning_tokens": self.reasoning_tokens,
                "cost_usd": round(self.cost_usd, 6), "peak_calls": self.peak_calls,
                "pricing_per_million": self.pricing}


# ------------------------------------------------------------------------ messages
def clean_assistant_message(msg: dict) -> dict:
    """Keep only the fields the API accepts back, INCLUDING reasoning_content."""
    out = {"role": "assistant", "content": msg.get("content")}
    if msg.get("reasoning_content") is not None:
        out["reasoning_content"] = msg["reasoning_content"]
    if msg.get("tool_calls"):
        # some OpenAI-compatible providers omit or repeat ids; they must be unique per message
        out["tool_calls"] = []
        for i, tc in enumerate(msg["tool_calls"]):
            clean = {"id": tc.get("id") or f"call_{i}", "type": tc.get("type") or "function",
                     "function": {"name": tc["function"]["name"],
                                  "arguments": tc["function"].get("arguments") or "{}"}}
            # Gemini 3 (testing provider) attaches extra_content.google.thought_signature to each
            # tool call and rejects the next request unless it is echoed back unchanged.
            if tc.get("extra_content"):
                clean["extra_content"] = tc["extra_content"]
            out["tool_calls"].append(clean)
        ids = [tc["id"] for tc in out["tool_calls"]]
        if len(set(ids)) != len(ids):
            for i, tc in enumerate(out["tool_calls"]):
                tc["id"] = f"{tc['id']}_{i}"
    return out


# -------------------------------------------------------------------------- client
PostFn = Callable[[str, bytes, dict, float], tuple[int, bytes]]


def _urllib_post(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""


class DeepSeekClient:
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com",
                 model: str = "deepseek-v4-pro", thinking: bool = True, reasoning_effort: str = "high",
                 timeout: float = 600, max_retries: int = 4, post: PostFn | None = None,
                 sleep: Callable[[float], None] = time.sleep, send_deepseek_params: bool = True):
        if not api_key:
            raise AuthError("No API key configured.")
        self.api_key = api_key
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.max_retries = max_retries
        self._post = post or _urllib_post
        self._sleep = sleep
        # DeepSeek-only fields (thinking, reasoning_effort, reasoning_content) are not sent to other providers.
        self.send_deepseek_params = send_deepseek_params

    def build_payload(self, messages: list[dict], tools: list[dict] | None,
                      max_tokens: int | None, thinking: bool | None) -> dict:
        think = self.thinking if thinking is None else thinking
        if not self.send_deepseek_params:
            messages = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in messages]
        payload = {"model": self.model, "messages": messages, "stream": False}
        if self.send_deepseek_params:
            payload["thinking"] = {"type": "enabled" if think else "disabled"}
            if think:
                payload["reasoning_effort"] = self.reasoning_effort
        if tools:
            payload["tools"] = tools
        if max_tokens:
            payload["max_tokens"] = max_tokens
        return payload

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             max_tokens: int | None = None, thinking: bool | None = None) -> tuple[dict, dict, str]:
        """Returns (assistant_message, usage, finish_reason)."""
        body = json.dumps(self.build_payload(messages, tools, max_tokens, thinking)).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        last_err = ""
        for attempt in range(self.max_retries + 1):
            try:
                status, raw = self._post(self.url, body, headers, self.timeout)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
                status, raw, last_err = -1, b"", f"network error: {e}"
            if status == 200:
                return self._parse(raw)
            if status == 401:
                raise AuthError("DeepSeek rejected the API key (HTTP 401). Check the key in Settings.")
            if status == 402:
                raise BalanceError("DeepSeek account balance is insufficient (HTTP 402). Top up and retry.")
            if status != -1:
                last_err = f"HTTP {status}: {self._error_text(raw)}"
            if status not in RETRYABLE_STATUS and status != -1:
                raise LLMError(last_err)
            if attempt < self.max_retries:
                # free tiers rate-limit per minute, so a 429 waits longer than a server error
                self._sleep(min(2 ** (attempt + 1) * (3 if status == 429 else 1), 60))
        raise LLMError(f"giving up after {self.max_retries + 1} attempts; last error: {last_err}")

    @staticmethod
    def _error_text(raw: bytes) -> str:
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            return str((data.get("error") or {}).get("message") or data)[:500]
        except (ValueError, AttributeError):
            return raw.decode("utf-8", "replace")[:500]

    @staticmethod
    def _parse(raw: bytes) -> tuple[dict, dict, str]:
        try:
            data = json.loads(raw.decode("utf-8"))
            choice = data["choices"][0]
            return choice["message"], data.get("usage") or {}, choice.get("finish_reason") or ""
        except (ValueError, KeyError, IndexError) as e:
            raise LLMError(f"unexpected response format: {e}: {raw[:300]!r}") from e
