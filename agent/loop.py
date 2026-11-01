"""The agent loop - the heart of every Claude-Code-style agent:

    while True:
        reply = model(messages, tools)
        if reply has no tool calls:  done
        for each tool call: run the tool, append its result
        (repeat)

Around that core sit the safeguards an unattended agent needs:
  * budget guard   - stops before the dollar limit is crossed
  * step limit     - stops a model that never finishes
  * cancel flag    - lets the GUI stop a run
  * compaction     - shrinks old tool outputs when the context grows too big
  * transcript     - every message is logged to <output>/.agent/ for review
  * final gate     - when the model says it is done, the verifier (verify.py) runs; blocking
                     issues are sent back and the model must fix them (max `max_gate_rounds`)
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .llm import CostTracker, LLMError, clean_assistant_message
from .tools import ToolBox

EventFn = Callable[[str, dict], None]


class ChatModel(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None, **kw) -> tuple[dict, dict, str]: ...


@dataclass
class RunResult:
    status: str     # completed | completed_with_issues | budget_exhausted | max_steps | cancelled | error
    final_message: str
    steps: int
    cost: dict
    files_written: list
    seconds: float
    error: str = ""
    test_runs: list = field(default_factory=list)
    verified: bool | None = None   # None = no verifier configured
    gate_rounds: int = 0
    meta: dict = field(default_factory=dict)   # provider and model used for this run


class Agent:
    def __init__(self, llm: ChatModel, toolbox: ToolBox, system_prompt: str, tracker: CostTracker,
                 max_steps: int = 80, budget_usd: float = 0.90, compact_at_tokens: int = 100_000,
                 on_event: EventFn | None = None, log_dir: Path | None = None,
                 verifier: Callable | None = None, max_gate_rounds: int = 3, meta: dict | None = None):
        self.llm = llm
        self.meta = dict(meta or {})
        self.verifier = verifier
        self.max_gate_rounds = max_gate_rounds
        self.gate_rounds = 0
        self.last_report = None
        self.tools = toolbox
        self.tracker = tracker
        self.max_steps = max_steps
        self.budget_usd = budget_usd
        self.compact_at_tokens = compact_at_tokens
        self.on_event = on_event or (lambda kind, data: None)
        self.cancel_flag = threading.Event()
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]
        self.log_dir = Path(log_dir) if log_dir else toolbox.root / ".agent"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._transcript = self.log_dir / "transcript.jsonl"
        self._transcript.write_text("", encoding="utf-8")
        self._log({"role": "system", "content": f"[system prompt, {len(system_prompt)} chars]"})

    # ------------------------------------------------------------------ helpers
    def cancel(self):
        self.cancel_flag.set()

    def _emit(self, kind: str, **data):
        try:
            self.on_event(kind, data)
        except Exception:  # noqa: BLE001 - a broken UI callback must not kill the run
            pass

    def _log(self, msg: dict):
        with open(self._transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time(), 2), **msg}, ensure_ascii=False) + "\n")

    def _append(self, msg: dict):
        self.messages.append(msg)
        self._log(msg)

    def _compact(self):
        """Elide the bodies of old tool results. This changes earlier messages, so the
        next call misses the cache once - acceptable, because it only happens when the
        context is already large and it makes every later call cheaper."""
        keep_tail = 10
        changed = 0
        for m in self.messages[1:-keep_tail]:
            if m["role"] == "tool" and len(m.get("content", "")) > 500 and not m.get("_compacted"):
                m["content"] = m["content"][:300] + "\n[...older output elided to save context; re-run the tool if needed]"
                m["_compacted"] = True
                changed += 1
        if changed:
            self._emit("compacted", messages=changed)

    def _api_messages(self) -> list[dict]:
        return [{k: v for k, v in m.items() if not k.startswith("_")} for m in self.messages]

    # ------------------------------------------------------------------ main loop
    def run(self, task: str) -> RunResult:
        t0 = time.time()
        self._append({"role": "user", "content": task})
        status, final, error, step = "error", "", "", 0
        try:
            while True:
                if self.cancel_flag.is_set():
                    status = "cancelled"
                    break
                if step >= self.max_steps:
                    status = "max_steps"
                    break
                if self.tracker.cost_usd >= self.budget_usd:
                    status = "budget_exhausted"
                    break
                step += 1
                self._emit("thinking", step=step)
                msg, usage, finish = self.llm.chat(self._api_messages(), tools=self.tools.schemas())
                delta = self.tracker.add(usage)
                self._emit("usage", step=step, **delta, total_usd=self.tracker.cost_usd,
                           cache_hit_rate=self.tracker.cache_hit_rate)
                assistant = clean_assistant_message(msg)
                self._append(assistant)
                if assistant.get("content"):
                    self._emit("assistant", text=assistant["content"])
                calls = assistant.get("tool_calls") or []
                if not calls:
                    if finish == "length":
                        self._append({"role": "user", "content": "Your reply was cut off. Continue."})
                        continue
                    final = assistant.get("content") or ""
                    if self.verifier is None:
                        status = "completed"
                        break
                    report = self._verify("final")
                    if report.passed:
                        status = "completed"
                        break
                    if self.gate_rounds >= self.max_gate_rounds:
                        status = "completed_with_issues"
                        break
                    self.gate_rounds += 1
                    self._append({"role": "user", "content":
                                  f"[final gate {self.gate_rounds}/{self.max_gate_rounds}] You tried to finish, but "
                                  f"automatic verification found problems.\n{report.format()}\n"
                                  f"Fix every blocking issue, then finish again with a short summary."})
                    continue
                for tc in calls:
                    self._run_tool(tc, step)
                    if self.cancel_flag.is_set():
                        break
                if delta["prompt"] > self.compact_at_tokens:
                    self._compact()
        except LLMError as e:
            status, error = "error", str(e)
            self._emit("error", message=error)
        result = RunResult(status=status, final_message=final, steps=step, cost=self.tracker.summary(),
                           files_written=sorted(self.tools.files_written),
                           seconds=round(time.time() - t0, 1), error=error, test_runs=self.tools.test_runs,
                           verified=None if self.verifier is None else bool(self.last_report and self.last_report.passed),
                           gate_rounds=self.gate_rounds, meta=self.meta)
        (self.log_dir / "run_summary.json").write_text(json.dumps(result.__dict__, indent=2), encoding="utf-8")
        self._emit("done", status=status, cost_usd=self.tracker.cost_usd, steps=step)
        return result

    def _verify(self, stage: str):
        report = self.verifier(stage)
        self.last_report = report
        self._emit("verify", stage=stage, passed=report.passed, blocking=len(report.blocking),
                   warnings=len(report.issues) - len(report.blocking), tests_run=report.tests_run,
                   round=report.round)
        return report

    def _run_tool(self, tc: dict, step: int):
        name = tc["function"]["name"]
        raw = tc["function"].get("arguments") or "{}"
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            res_ok, content = False, f"ERROR: arguments are not valid JSON ({e}). Send a JSON object."
        else:
            self._emit("tool_call", name=name, args=_summarise_args(args))
            res = self.tools.execute(name, args)
            res_ok, content = res.ok, res.content
            if name == "todo_write" and res.ok:
                self._emit("todo", items=list(self.tools.todos))
            if name in ("write_file", "edit_file") and res.ok:
                self._emit("file", path=args.get("path", ""))
        left = self.max_steps - step
        if left <= 5:
            content += f"\n[agent notice: only {left} model call(s) left - wrap up and finish]"
        remaining = self.budget_usd - self.tracker.cost_usd
        if remaining < 0.15 * self.budget_usd:
            content += f"\n[agent notice: budget nearly used (${remaining:.3f} left) - finish now]"
        self._emit("tool_result", name=name, ok=res_ok, preview=content[:300])
        self._append({"role": "tool", "tool_call_id": tc["id"], "content": content})


def _summarise_args(args: dict) -> dict:
    """Short version of the arguments for the UI log (no full file contents)."""
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 80:
            out[k] = f"<{len(v)} chars>"
        else:
            out[k] = v
    return out
