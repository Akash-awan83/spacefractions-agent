"""The agent loop - the heart of every Claude-Code-style agent:

    while True:
        reply = model(messages, tools)
        if reply has no tool calls:  done
        for each tool call: run the tool, append its result
        (repeat)

Around that core sit the safeguards an unattended agent needs:
  * budget guard   - stops before the dollar limit is crossed: a new call starts only if
                     the spend so far plus the most expensive call so far stays under it
  * step limit     - stops a model that never finishes
  * cancel flag    - lets the GUI stop a run
  * compression    - context.py's pipeline (snip -> dedupe -> summarise) when the context grows
  * recovery       - "continue" strategies instead of crashing: API retries (llm.py), a cut-off
                     reply is continued, an empty reply is nudged, a context-overflow error is
                     answered by compressing and retrying, bad tool arguments go back to the model
  * parallel reads - read-only tool calls in one reply run concurrently; writes run in order
  * session file   - the API messages are saved after every step (.agent/session.json)
  * transcript     - every message is logged to <output>/.agent/ for review
  * final gate     - when the model says it is done, the verifier (verify.py) runs; blocking
                     issues are sent back and the model must fix them (max `max_gate_rounds`)
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .context import compress, estimate_tokens
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
                 verifier: Callable | None = None, max_gate_rounds: int = 3, meta: dict | None = None,
                 summarize_above: int = 150_000):
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
        self.summarize_above = summarize_above
        self.recent_files: list[str] = []
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

    def _compact(self, force_summary: bool = False):
        """Compression pipeline (context.py). Changing earlier messages costs one cache miss, so it
        only runs when the context is already large."""
        stats = compress(self.messages, summarize=self._summarize,
                         summarize_above=1 if force_summary else self.summarize_above,
                         recent_files=self.recent_files)
        if any(stats.values()):
            self._emit("compacted", messages=sum(stats.values()), **stats)
        return stats

    def _summarize(self, transcript: str) -> str:
        msg, usage, _ = self.llm.chat(
            [{"role": "system", "content": "Summarise this coding-agent transcript for the agent itself: the "
                                           "task, decisions taken, files created and their purpose, test status, "
                                           "and what remains. Be concise and factual."},
             {"role": "user", "content": transcript}], tools=None)
        self.tracker.add(usage)
        return msg.get("content") or ""

    def _save_session(self):
        try:
            (self.log_dir / "session.json").write_text(json.dumps(self._api_messages(), ensure_ascii=False),
                                                       encoding="utf-8")
        except OSError:
            pass

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
                if self.tracker.cost_usd + self.tracker.max_step_cost >= self.budget_usd:
                    status = "budget_exhausted"
                    break
                step += 1
                self._emit("thinking", step=step)
                try:
                    msg, usage, finish = self.llm.chat(self._api_messages(), tools=self.tools.schemas())
                except LLMError as e:
                    # recovery: the context is too long -> compress hard and try this step again
                    if _is_context_overflow(str(e)) and not getattr(self, "_overflow_retry", False):
                        self._overflow_retry = True
                        self._emit("error", message="context too long - compressing and retrying")
                        self._compact(force_summary=True)
                        step -= 1
                        continue
                    raise
                self._overflow_retry = False
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
                    if not (assistant.get("content") or "").strip() and not getattr(self, "_nudged", False):
                        self._nudged = True    # recovery: an empty reply gets one nudge
                        self._append({"role": "user", "content": "Your reply was empty. Continue the task, "
                                                                 "or finish with a short summary."})
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
                self._run_calls(calls, step)
                if delta["prompt"] > self.compact_at_tokens:
                    self._compact()
                self._save_session()
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

    def _run_calls(self, calls: list[dict], step: int):
        """Read-only calls in one reply run in parallel (as in Claude Code's tool engine);
        anything that writes or executes runs one at a time, in order."""
        parsed = [self._parse_call(tc) for tc in calls]
        parallel = len(parsed) > 1 and all(p[2] is not None and self.tools.is_read_only(p[0]) for p in parsed)
        if parallel:
            for name, _tc, args, _err in parsed:
                self._emit("tool_call", name=name, args=_summarise_args(args))
            with ThreadPoolExecutor(max_workers=min(4, len(parsed))) as pool:
                results = list(pool.map(lambda p: self.tools.execute(p[0], p[2]), parsed))
            for (name, tc, args, _err), res in zip(parsed, results):
                self._finish_call(name, tc, args, res.ok, res.content, step)
            return
        for name, tc, args, err in parsed:
            if self.cancel_flag.is_set():
                break
            if args is None:
                self._finish_call(name, tc, {}, False, err, step)
                continue
            self._emit("tool_call", name=name, args=_summarise_args(args))
            res = self.tools.execute(name, args)
            self._finish_call(name, tc, args, res.ok, res.content, step)

    @staticmethod
    def _parse_call(tc: dict):
        name = tc["function"]["name"]
        raw = tc["function"].get("arguments") or "{}"
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
            return name, tc, args, ""
        except ValueError as e:
            return name, tc, None, f"ERROR: arguments are not valid JSON ({e}). Send a JSON object."

    def _finish_call(self, name: str, tc: dict, args: dict, res_ok: bool, content: str, step: int):
        if name == "todo_write" and res_ok:
            self._emit("todo", items=list(self.tools.todos))
        if name in ("write_file", "edit_file") and res_ok:
            path = args.get("path", "")
            self._emit("file", path=path)
            if path in self.recent_files:
                self.recent_files.remove(path)
            self.recent_files.append(path)
        left = self.max_steps - step
        if left <= 5:
            content += f"\n[agent notice: only {left} model call(s) left - wrap up and finish]"
        remaining = self.budget_usd - self.tracker.cost_usd
        if remaining < 0.15 * self.budget_usd:
            content += f"\n[agent notice: budget nearly used (${remaining:.3f} left) - finish now]"
        self._emit("tool_result", name=name, ok=res_ok, preview=content[:300])
        self._append({"role": "tool", "tool_call_id": tc["id"], "content": content})


def _is_context_overflow(text: str) -> bool:
    t = text.lower()
    return ("context" in t and ("length" in t or "too long" in t or "maximum" in t)) or "too many tokens" in t


def _summarise_args(args: dict) -> dict:
    """Short version of the arguments for the UI log (no full file contents)."""
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 80:
            out[k] = f"<{len(v)} chars>"
        else:
            out[k] = v
    return out
