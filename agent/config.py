"""Settings for the agent.

The config file lives in the user's profile, never inside the repository:
    Windows: %APPDATA%\\SpaceFractionsAgent\\config.json
    others : ~/.config/SpaceFractionsAgent/config.json
so the DeepSeek API key cannot be committed to GitHub by accident.
The environment variable DEEPSEEK_API_KEY, if set, overrides the stored key.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

APP_NAME = "SpaceFractionsAgent"

# Official DeepSeek prices (USD per 1M tokens) for deepseek-v4-pro at the time of
# writing. Verify on the DeepSeek pricing page; the values are editable in config.json.
DEFAULT_PRICING = {"cache_hit_input": 0.003625, "cache_miss_input": 0.435, "output": 0.87}

FREE_PRICING = {"cache_hit_input": 0.0, "cache_miss_input": 0.0, "output": 0.0}

# Providers. DeepSeek is the one the task requires; the others exist only so the agent can be
# exercised on a free tier during development. Final runs, cost evidence and the demo use DeepSeek.
PROVIDERS = {
    "deepseek": {"label": "DeepSeek (official - use for final runs)",
                 "base_url": "https://api.deepseek.com", "model": "deepseek-v4-pro",
                 "env": "DEEPSEEK_API_KEY", "deepseek_params": True, "max_retries": 4},
    "gemini": {"label": "Google Gemini free tier (testing only)",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-3.8-flash",
               "env": "GEMINI_API_KEY", "deepseek_params": False, "max_retries": 8},
    "openrouter": {"label": "OpenRouter free models (testing only)",
                   "base_url": "https://openrouter.ai/api/v1", "model": "",
                   "env": "OPENROUTER_API_KEY", "deepseek_params": False, "max_retries": 8},
}

DEFAULTS = {
    "provider": "deepseek",
    "api_key": "",               # DeepSeek key
    "api_keys": {},              # keys of the testing providers, by provider name
    "models": {},                # model ids of the testing providers, by provider name
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-pro",
    "thinking": True,            # DeepSeek thinking mode (reasoning before answering)
    "reasoning_effort": "high",
    "budget_usd": 0.90,          # hard stop; the evaluation limit is $1.00
    "max_steps": 80,             # max LLM calls per run
    "request_timeout": 600,      # seconds; thinking can be slow
    "pricing_per_million": DEFAULT_PRICING,
    "last_doc": "",
    "last_views": "",
    "last_output": "",
}


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config(path: Path | None = None) -> dict:
    path = path or config_path()
    cfg = copy.deepcopy(DEFAULTS)
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
            if cfg.get("provider") not in PROVIDERS:
                cfg["provider"] = "deepseek"
        except (OSError, json.JSONDecodeError):
            pass  # a broken file falls back to defaults instead of crashing the app
    return cfg


def save_config(cfg: dict, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    path.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # owner-only on POSIX; harmless on Windows
    except OSError:
        pass
    return path


def provider_of(cfg: dict, provider: str | None = None) -> str:
    p = provider or cfg.get("provider") or "deepseek"
    return p if p in PROVIDERS else "deepseek"


def resolve_api_key(cfg: dict, provider: str | None = None) -> str:
    p = provider_of(cfg, provider)
    env = os.environ.get(PROVIDERS[p]["env"], "").strip()
    stored = cfg.get("api_key", "") if p == "deepseek" else (cfg.get("api_keys") or {}).get(p, "")
    return env or str(stored).strip()


def set_api_key(cfg: dict, key: str, provider: str | None = None) -> None:
    p = provider_of(cfg, provider)
    if p == "deepseek":
        cfg["api_key"] = key
    else:
        cfg["api_keys"] = dict(cfg.get("api_keys") or {}, **{p: key})


def resolve_model(cfg: dict, provider: str | None = None) -> str:
    p = provider_of(cfg, provider)
    if p == "deepseek":
        return cfg.get("model") or PROVIDERS[p]["model"]
    return (cfg.get("models") or {}).get(p) or PROVIDERS[p]["model"]


def set_model(cfg: dict, model: str, provider: str | None = None) -> None:
    p = provider_of(cfg, provider)
    if p == "deepseek":
        cfg["model"] = model
    else:
        cfg["models"] = dict(cfg.get("models") or {}, **{p: model})


def resolve_base_url(cfg: dict, provider: str | None = None) -> str:
    p = provider_of(cfg, provider)
    if p == "deepseek":
        return cfg.get("base_url") or PROVIDERS[p]["base_url"]
    return PROVIDERS[p]["base_url"]


def resolve_pricing(cfg: dict, provider: str | None = None) -> dict:
    return cfg["pricing_per_million"] if provider_of(cfg, provider) == "deepseek" else dict(FREE_PRICING)


def make_client(cfg: dict, provider: str | None = None, **overrides):
    """Build the chat client for the selected provider."""
    from .llm import DeepSeekClient
    p = provider_of(cfg, provider)
    preset = PROVIDERS[p]
    kw = dict(api_key=resolve_api_key(cfg, p), base_url=resolve_base_url(cfg, p), model=resolve_model(cfg, p),
              thinking=cfg["thinking"], reasoning_effort=cfg["reasoning_effort"], timeout=cfg["request_timeout"],
              send_deepseek_params=preset["deepseek_params"], max_retries=preset["max_retries"])
    kw.update(overrides)
    return DeepSeekClient(**kw)


def mask_key(key: str) -> str:
    """Show only enough of the key to recognise it: sk-...a1b2"""
    if not key:
        return "(not set)"
    return f"{key[:3]}...{key[-4:]}" if len(key) > 8 else "***"
