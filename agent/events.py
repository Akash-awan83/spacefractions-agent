"""Turns agent events into one-line log messages (used by both the CLI and the GUI)."""
from __future__ import annotations


def format_event(kind: str, data: dict) -> str | None:
    if kind == "preprocessed":
        return f"[prep] {data['diagrams']} diagrams parsed, {data['conflicts']} design conflicts detected"
    if kind == "thinking":
        return f"[step {data['step']}] model is thinking ..."
    if kind == "usage":
        return (f"   tokens in {data['prompt']} (cache hit {data['hit']}), out {data['output']}"
                f"  |  step ${data['cost_usd']:.4f}  |  total ${data['total_usd']:.4f}")
    if kind == "assistant":
        text = data["text"].strip().replace("\n", " ")
        return f"   model: {text[:300]}{'...' if len(text) > 300 else ''}"
    if kind == "tool_call":
        args = ", ".join(f"{k}={v}" for k, v in data["args"].items())
        return f"   -> {data['name']}({args})"
    if kind == "tool_result":
        first = data["preview"].splitlines()[0][:160] if data["preview"] else ""
        return f"   <- {data['name']} [{'ok' if data['ok'] else 'ERROR'}] {first}"
    if kind == "compacted":
        return f"   (context compacted: {data['messages']} old tool outputs shortened)"
    if kind == "error":
        return f"   ERROR: {data['message']}"
    if kind == "verify":
        if data["passed"]:
            return (f"[verify #{data['round']}] PASSED - {data['tests_run']} tests green"
                    + (f", {data['warnings']} warning(s)" if data["warnings"] else ""))
        return (f"[verify #{data['round']}] FAILED - {data['blocking']} blocking issue(s), "
                f"{data['warnings']} warning(s); sent back to the model")
    if kind == "done":
        return f"[done] status={data['status']}  steps={data['steps']}  cost=${data['cost_usd']:.4f}"
    return None  # todo/file events are shown elsewhere in the GUI
