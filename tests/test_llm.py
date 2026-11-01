"""Tests for agent.llm - no network: the HTTP layer is replaced by a fake."""
import json

import pytest

from agent.config import DEFAULT_PRICING, load_config, mask_key, save_config
from agent.llm import (AuthError, BalanceError, CostTracker, DeepSeekClient, LLMError,
                       clean_assistant_message)


def ok_body(content="hi", tool_calls=None, reasoning="because", usage=None):
    msg = {"role": "assistant", "content": content, "reasoning_content": reasoning}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return json.dumps({"choices": [{"message": msg, "finish_reason": "stop"}],
                       "usage": usage or {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80,
                                          "prompt_cache_miss_tokens": 20, "completion_tokens": 10}}).encode()


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def __call__(self, url, body, headers, timeout):
        self.sent.append(json.loads(body))
        return self.responses.pop(0)


def client(fake, **kw):
    return DeepSeekClient(api_key="sk-test", post=fake, sleep=lambda s: None, **kw)


def test_payload_follows_deepseek_thinking_contract():
    fake = FakeHTTP([(200, ok_body())])
    client(fake).chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
    p = fake.sent[0]
    assert p["model"] == "deepseek-v4-pro"
    assert p["thinking"] == {"type": "enabled"} and p["reasoning_effort"] == "high"
    assert "temperature" not in p and p["tools"]


def test_thinking_can_be_disabled_per_call():
    fake = FakeHTTP([(200, ok_body())])
    client(fake).chat([{"role": "user", "content": "x"}], thinking=False, max_tokens=5)
    assert fake.sent[0]["thinking"] == {"type": "disabled"} and "reasoning_effort" not in fake.sent[0]


def test_retries_then_succeeds():
    fake = FakeHTTP([(503, b"busy"), (429, b"slow down"), (200, ok_body("done"))])
    msg, usage, finish = client(fake).chat([{"role": "user", "content": "x"}])
    assert msg["content"] == "done" and len(fake.sent) == 3


@pytest.mark.parametrize("status,exc", [(401, AuthError), (402, BalanceError), (400, LLMError)])
def test_error_mapping(status, exc):
    fake = FakeHTTP([(status, json.dumps({"error": {"message": "nope"}}).encode())])
    with pytest.raises(exc):
        client(fake).chat([{"role": "user", "content": "x"}])
    assert len(fake.sent) == 1  # not retried


def test_gives_up_after_max_retries():
    fake = FakeHTTP([(500, b"x")] * 3)
    with pytest.raises(LLMError, match="giving up"):
        client(fake, max_retries=2).chat([{"role": "user", "content": "x"}])


def test_missing_key_is_rejected():
    with pytest.raises(AuthError):
        DeepSeekClient(api_key="")


def test_clean_message_keeps_reasoning_content():
    raw = {"role": "assistant", "content": None, "reasoning_content": "r",
           "tool_calls": [{"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
           "extra": "dropped"}
    clean = clean_assistant_message(raw)
    assert clean["reasoning_content"] == "r" and "extra" not in clean and clean["tool_calls"][0]["id"] == "1"


def test_cost_tracker_uses_cache_prices():
    t = CostTracker(DEFAULT_PRICING)
    d = t.add({"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 900_000,
               "prompt_cache_miss_tokens": 100_000, "completion_tokens": 100_000})
    expected = (900_000 * 0.003625 + 100_000 * 0.435 + 100_000 * 0.87) / 1_000_000
    assert d["cost_usd"] == pytest.approx(expected)
    assert t.cache_hit_rate == pytest.approx(0.9)


def test_config_roundtrip_and_mask(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    p = tmp_path / "c.json"
    cfg = load_config(p)
    cfg["api_key"] = "sk-abcdef123456"
    save_config(cfg, p)
    assert load_config(p)["api_key"] == "sk-abcdef123456"
    assert mask_key("sk-abcdef123456") == "sk-...3456"
    p.write_text("{broken")
    assert load_config(p)["model"] == "deepseek-v4-pro"  # broken file -> defaults


# ---- testing providers (Gemini / OpenRouter) ------------------------------------------------
def test_non_deepseek_payload_omits_deepseek_fields():
    fake = FakeHTTP([(200, ok_body())])
    c = client(fake, send_deepseek_params=False, model="gemini-2.5-flash")
    c.chat([{"role": "user", "content": "x"},
            {"role": "assistant", "content": None, "reasoning_content": "r", "tool_calls": []}])
    p = fake.sent[0]
    assert "thinking" not in p and "reasoning_effort" not in p
    assert all("reasoning_content" not in m for m in p["messages"])


def test_missing_or_duplicate_tool_ids_are_repaired():
    raw = {"role": "assistant", "content": None, "tool_calls": [
        {"function": {"name": "a", "arguments": "{}"}},
        {"id": "x", "function": {"name": "b", "arguments": "{}"}},
        {"id": "x", "function": {"name": "c"}}]}
    ids = [tc["id"] for tc in clean_assistant_message(raw)["tool_calls"]]
    assert len(set(ids)) == 3 and all(ids)


def test_provider_keys_and_models_are_separate(tmp_path, monkeypatch):
    from agent.config import (make_client, resolve_api_key, resolve_model, resolve_pricing,
                              set_api_key, set_model)
    for env in ("DEEPSEEK_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    p = tmp_path / "c.json"
    cfg = load_config(p)
    set_api_key(cfg, "sk-deep-1234", "deepseek")
    set_api_key(cfg, "AIza-gem-5678", "gemini")
    set_model(cfg, "my/free-model:free", "openrouter")
    save_config(cfg, p)
    cfg = load_config(p)
    assert resolve_api_key(cfg, "deepseek") == "sk-deep-1234"
    assert resolve_api_key(cfg, "gemini") == "AIza-gem-5678"
    assert resolve_model(cfg, "gemini") == "gemini-3.8-flash"
    assert resolve_model(cfg, "openrouter") == "my/free-model:free"
    assert resolve_pricing(cfg, "gemini")["output"] == 0.0
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-env")
    assert resolve_api_key(cfg, "gemini") == "AIza-env"
    cfg["provider"] = "gemini"
    c = make_client(cfg)
    assert c.send_deepseek_params is False and "generativelanguage" in c.url and c.max_retries == 8
    cfg["provider"] = "deepseek"
    d = make_client(cfg)
    assert d.send_deepseek_params is True and d.url == "https://api.deepseek.com/chat/completions"


def test_rate_limit_waits_longer():
    waits = []
    fake = FakeHTTP([(429, b"slow"), (503, b"busy"), (200, ok_body())])
    DeepSeekClient(api_key="k", post=fake, sleep=waits.append).chat([{"role": "user", "content": "x"}])
    assert waits == [6, 4]


def test_gemini_thought_signature_is_echoed_back():
    """Gemini 3 rejects the next request if extra_content.google.thought_signature is dropped."""
    sig = {"google": {"thought_signature": "CvcQAdHt=="}}
    raw = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function", "extra_content": sig, "function": {"name": "list_dir", "arguments": "{}"}}]}
    clean = clean_assistant_message(raw)
    assert clean["tool_calls"][0]["extra_content"] == sig
    fake = FakeHTTP([(200, ok_body())])
    client(fake, send_deepseek_params=False).chat([{"role": "user", "content": "x"}, clean])
    assert fake.sent[0]["messages"][1]["tool_calls"][0]["extra_content"] == sig
    plain = clean_assistant_message({"role": "assistant", "content": None, "tool_calls": [
        {"id": "b", "function": {"name": "f", "arguments": "{}"}}]})
    assert "extra_content" not in plain["tool_calls"][0]   # DeepSeek messages stay unchanged
