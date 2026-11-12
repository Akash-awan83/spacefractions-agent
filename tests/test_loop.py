"""End-to-end tests of the agent loop with a scripted fake model (no API, no cost)."""
import json
from pathlib import Path

from agent.config import DEFAULTS
from agent.llm import CostTracker
from agent.loop import Agent
from agent.pipeline import run_pipeline
from agent.tools import ToolBox

ROOT = Path(__file__).resolve().parents[1]
USAGE = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100,
         "completion_tokens": 200}


def call(name, args, cid):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def model_tools(model):
    return model.tools_seen[0]


class FakeModel:
    """Replays a list of assistant messages and records what it was sent."""
    def __init__(self, script, usage=USAGE):
        self.script = list(script)
        self.usage = usage
        self.received = []
        self.tools_seen = []

    def chat(self, messages, tools=None, **kw):
        self.received.append(json.loads(json.dumps(messages)))
        self.tools_seen.append(tools or [])
        msg = self.script.pop(0) if self.script else {"role": "assistant", "content": "done"}
        return msg, dict(self.usage), "stop"


def make_agent(tmp_path, script, **kw):
    model = FakeModel(script)
    events = []
    agent = Agent(llm=model, toolbox=ToolBox(root=tmp_path / "ws", test_timeout=60),
                  system_prompt="SYSTEM", tracker=CostTracker(DEFAULTS["pricing_per_million"]),
                  on_event=lambda k, d: events.append(k), **kw)
    return agent, model, events


MINI_PROJECT = [
    {"role": "assistant", "content": None, "reasoning_content": "plan first",
     "tool_calls": [call("todo_write", {"items": [{"content": "write code", "status": "in_progress"}]}, "c1")]},
    {"role": "assistant", "content": None, "reasoning_content": "write files",
     "tool_calls": [call("write_file", {"path": "frac.py", "content":
                                        "from fractions import Fraction\n\ndef add(a, b):\n    return Fraction(a) + Fraction(b)\n"}, "c2"),
                    call("write_file", {"path": "tests/__init__.py", "content": ""}, "c3"),
                    call("write_file", {"path": "tests/test_frac.py", "content":
                                        "import unittest\nfrom fractions import Fraction\nfrom frac import add\n\n"
                                        "class T(unittest.TestCase):\n    def test_add(self):\n"
                                        "        self.assertEqual(add('1/2', '1/3'), Fraction(5, 6))\n"}, "c4")]},
    {"role": "assistant", "content": None, "reasoning_content": "verify",
     "tool_calls": [call("check_syntax", {}, "c5"), call("run_tests", {}, "c6")]},
    {"role": "assistant", "content": "All tests pass.", "reasoning_content": "finish"},
]


def test_full_loop_writes_files_runs_tests_and_finishes(tmp_path):
    agent, model, events = make_agent(tmp_path, MINI_PROJECT)
    res = agent.run("build it")
    assert res.status == "completed" and res.final_message == "All tests pass."
    assert res.steps == 4
    assert {"frac.py", "tests/test_frac.py"} <= set(res.files_written)
    assert res.test_runs and res.test_runs[-1]["ok"] is True
    assert res.cost["calls"] == 4 and res.cost["cost_usd"] > 0
    assert "tool_call" in events and events[-1] == "done"
    log = (tmp_path / "ws" / ".agent")
    assert (log / "transcript.jsonl").stat().st_size > 0
    assert json.loads((log / "run_summary.json").read_text())["status"] == "completed"


def test_reasoning_content_is_sent_back(tmp_path):
    """DeepSeek returns HTTP 400 if reasoning_content is dropped during tool calls."""
    agent, model, _ = make_agent(tmp_path, MINI_PROJECT)
    agent.run("build it")
    second_request = model.received[1]
    assistants = [m for m in second_request if m["role"] == "assistant"]
    assert assistants[0]["reasoning_content"] == "plan first"
    tools = [m for m in second_request if m["role"] == "tool"]
    assert tools[0]["tool_call_id"] == "c1"


def test_budget_guard_stops_the_run(tmp_path):
    looping = [{"role": "assistant", "content": None,
                "tool_calls": [call("list_dir", {}, f"x{i}")]} for i in range(50)]
    agent, model, _ = make_agent(tmp_path, looping, budget_usd=0.0005)
    res = agent.run("go")
    assert res.status == "budget_exhausted" and res.steps < 50


def test_step_limit_and_notice(tmp_path):
    looping = [{"role": "assistant", "content": None,
                "tool_calls": [call("list_dir", {}, f"x{i}")]} for i in range(50)]
    agent, model, _ = make_agent(tmp_path, looping, max_steps=6)
    res = agent.run("go")
    assert res.status == "max_steps" and res.steps == 6
    assert "wrap up" in agent.messages[-1]["content"]


def test_bad_json_and_cancel(tmp_path):
    bad = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "b1", "type": "function", "function": {"name": "list_dir", "arguments": "{not json"}}]}]
    agent, model, _ = make_agent(tmp_path, bad)
    res = agent.run("go")
    assert "not valid JSON" in [m for m in agent.messages if m["role"] == "tool"][0]["content"]
    assert res.status == "completed"
    agent2, _, _ = make_agent(tmp_path / "b", MINI_PROJECT)
    agent2.cancel()
    assert agent2.run("go").status == "cancelled"


def test_compaction_elides_old_tool_output(tmp_path):
    script = [{"role": "assistant", "content": None,
               "tool_calls": [call("write_file", {"path": f"f{i}.txt", "content": "x" * 2000}, f"w{i}")]}
              for i in range(3)]
    script += [{"role": "assistant", "content": None,
                "tool_calls": [call("read_file", {"path": f"f{i}.txt"}, f"r{i}")]} for i in range(12)]
    agent, model, events = make_agent(tmp_path, script, compact_at_tokens=500)
    agent.run("go")
    assert "compacted" in events
    assert any("snipped" in m.get("content", "") for m in agent.messages if m["role"] == "tool")


def test_pipeline_with_real_inputs_and_fake_model(tmp_path):
    from agent.preprocess import build
    from fake_projects import script_writing, valid_files
    cfg = dict(DEFAULTS)
    arch = build(ROOT / "inputs/Architecture_Documentation.md", ROOT / "inputs/Architecture_View.md")
    model = FakeModel(MINI_PROJECT[:1] + script_writing(valid_files(arch)))
    res = run_pipeline(cfg, [ROOT / "inputs/Architecture_Documentation.md", ROOT / "inputs/Architecture_View.md"],
                       tmp_path / "out", llm=model)
    assert res.status == "completed" and res.verified is True
    assert any(t["name"] == "verify" for t in [s["function"] for s in model_tools(model)])
    system = model.received[0][0]["content"]
    assert "MAAD" in system and '"conflicts"' in system and "deepseek-v4-pro" in system
    assert (tmp_path / "out" / ".agent" / "architecture.json").exists()
    summary = json.loads((tmp_path / "out" / ".agent" / "run_summary.json").read_text())
    assert summary["meta"]["provider"] == "deepseek" and summary["meta"]["model"] == "deepseek-v4-pro"
    # the system prompt is byte-identical on every call -> DeepSeek prefix cache hits
    assert all(r[0]["content"] == system for r in model.received)


def test_resume_lists_existing_files_and_batching_rule(tmp_path):
    from agent.context import DEFAULT_TASK as TASK_PROMPT
    from agent.pipeline import task_for
    out = tmp_path / "o"
    assert task_for(out) == TASK_PROMPT
    (out / ".agent").mkdir(parents=True)
    (out / ".agent" / "transcript.jsonl").write_text("x")
    assert task_for(out) == TASK_PROMPT            # agent logs alone do not count
    (out / "spacefractions" / "question").mkdir(parents=True)
    (out / "spacefractions" / "question" / "models.py").write_text("x")
    t = task_for(out)
    assert "RESUME" in t and "spacefractions/question/models.py" in t
    model = FakeModel([{"role": "assistant", "content": "ok"}])
    run_pipeline(dict(DEFAULTS), [ROOT / "inputs/Architecture_Documentation.md",
                 ROOT / "inputs/Architecture_View.md"], out, llm=model)
    first_user = [m for m in model.received[0] if m["role"] == "user"][0]["content"]
    assert "spacefractions/question/models.py" in first_user
    assert "BATCH" in model.received[0][0]["content"]


def test_budget_guard_keeps_a_reserve_for_the_next_call(tmp_path):
    """Cost per step c, budget 2.5c: calls 1 and 2 fit (2c); a 3rd would reach 3c, so it never starts."""
    looping = [{"role": "assistant", "content": None,
                "tool_calls": [call("list_dir", {}, f"x{i}")]} for i in range(20)]
    probe = CostTracker(DEFAULTS["pricing_per_million"])
    c = probe.add(dict(USAGE))["cost_usd"]
    agent, model, _ = make_agent(tmp_path, looping, budget_usd=2.5 * c)
    agent.tracker.clock = probe.clock
    res = agent.run("go")
    assert res.status == "budget_exhausted"
    assert res.cost["cost_usd"] <= 2.5 * c + 1e-12
    assert res.steps == 2


# ---- Version 2: parallel reads, recovery strategies ------------------------------------------
def test_read_only_calls_in_one_reply_run_in_parallel(tmp_path):
    import threading
    import time as _t
    agent, model, _ = make_agent(tmp_path, [
        {"role": "assistant", "content": None, "tool_calls": [call("read_file", {"path": f"f{i}.txt"}, f"r{i}")
                                                              for i in range(4)]},
        {"role": "assistant", "content": "done"}])
    for i in range(4):
        (agent.tools.root / f"f{i}.txt").write_text("x")
    active, peak = [0], [0]
    lock = threading.Lock()
    original = agent.tools.execute

    def slow(name, args):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        _t.sleep(0.2)
        with lock:
            active[0] -= 1
        return original(name, args)

    agent.tools.execute = slow
    agent.run("go")
    assert peak[0] > 1                     # several reads were running at the same time
    tool_ids = [m["tool_call_id"] for m in agent.messages if m["role"] == "tool"]
    assert tool_ids == ["r0", "r1", "r2", "r3"]   # results stay in order


def test_empty_reply_gets_one_nudge(tmp_path):
    agent, model, _ = make_agent(tmp_path, [{"role": "assistant", "content": ""},
                                            {"role": "assistant", "content": "finished"}])
    res = agent.run("go")
    assert res.status == "completed" and res.final_message == "finished"
    assert any("reply was empty" in (m.get("content") or "") for m in agent.messages if m["role"] == "user")


def test_context_overflow_is_recovered_by_compressing(tmp_path):
    from agent.llm import LLMError

    class Overflow(FakeModel):
        calls = 0

        def chat(self, messages, tools=None, **kw):
            Overflow.calls += 1
            if Overflow.calls == 1:
                raise LLMError("HTTP 400: This model's maximum context length is 131072 tokens")
            if tools is None:           # the summariser
                return {"role": "assistant", "content": "summary"}, dict(self.usage), "stop"
            return super().chat(messages, tools, **kw)

    model = Overflow([{"role": "assistant", "content": "done after recovery"}])
    agent = Agent(llm=model, toolbox=ToolBox(root=tmp_path / "ws"), system_prompt="S",
                  tracker=CostTracker(DEFAULTS["pricing_per_million"]))
    res = agent.run("go")
    assert res.status == "completed" and res.final_message == "done after recovery"


def test_session_file_is_saved_each_step(tmp_path):
    agent, model, _ = make_agent(tmp_path, MINI_PROJECT)
    agent.run("go")
    saved = json.loads((tmp_path / "ws" / ".agent" / "session.json").read_text())
    assert saved[0]["role"] == "system" and any(m["role"] == "tool" for m in saved)


def test_general_task_without_inputs(tmp_path):
    """A plain natural-language task with no input files - the agent is not tied to one project."""
    cfg = dict(DEFAULTS, verify_before_finish=False, target="any")
    model = FakeModel([{"role": "assistant", "content": None, "tool_calls": [
        call("write_file", {"path": "hello.py", "content": "print('hi')\n"}, "h1")]},
        {"role": "assistant", "content": "Wrote hello.py"}])
    res = run_pipeline(cfg, [], tmp_path / "out", task="Write hello.py that prints hi", llm=model)
    assert res.status == "completed" and (tmp_path / "out" / "hello.py").is_file()
    system, first_user = model.received[0][0]["content"], model.received[0][1]["content"]
    assert first_user == "Write hello.py that prints hi"
    assert "Attached input" not in system and "Verification contract" not in system
    assert res.meta["permission_mode"] == "workspace-write"
