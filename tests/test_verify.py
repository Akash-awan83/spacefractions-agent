"""Tests for agent.verify (quality gates) and the final verification gate in the loop."""
import json
from pathlib import Path

import pytest

from agent.config import DEFAULTS
from agent.llm import CostTracker
from agent.loop import Agent
from agent.preprocess import build
from agent.tools import ToolBox
from agent.verify import Verifier, classify_test_problem
from fake_projects import H, script_writing, valid_files, write
from test_loop import FakeModel

ROOT = Path(__file__).resolve().parents[1]
ARCH = build(ROOT / "inputs/Architecture_Documentation.md", ROOT / "inputs/Architecture_View.md")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    write(root, valid_files(ARCH))
    box = ToolBox(root=root, test_timeout=60)
    return root, Verifier(root, ARCH, run_tests=box.run_tests_raw)


def issues(rep, category):
    return [i for i in rep.issues if i.category == category]


def test_valid_project_passes(project):
    root, v = project
    rep = v()
    assert rep.passed, rep.format()
    assert rep.tests_run >= 8 and rep.issues == []


@pytest.mark.parametrize("rel,content,category,needle", [
    ("spacefractions/user/__init__.py", H.format(t="x") + "def f(:\n", "Syntax", ""),
    ("spacefractions/user/__init__.py", H.format(t="x") + "import requests\n", "Hallucination", "requests"),
    ("spacefractions/user/__init__.py", H.format(t="x") + "import spacefractions.nothing\n", "Hallucination", "does not exist"),
    ("spacefractions/user/__init__.py", H.format(t="x") + "from spacefractions.question import subtract\n",
     "Hallucination", "'subtract' is not defined"),
    ("spacefractions/user/__init__.py", H.format(t="x") + "from spacefractions.ui import tk\n",
     "Design conformance", "UI layer"),
    ("spacefractions/user/__init__.py", "x = 1\n", "Code style & standards", "header"),
    ("spacefractions/user/__init__.py", H.format(t="x") + "y = eval('1')\n", "Security", "eval"),
    ("spacefractions/storage/__init__.py", H.format(t="x") +
     "def q(c, n):\n    return c.execute(f'SELECT * FROM t WHERE n={n}')\n", "Security", "SQL"),
])
def test_each_defect_is_caught(project, rel, content, category, needle):
    root, v = project
    (root / rel).write_text(content, encoding="utf-8")
    rep = v()
    hits = [i for i in issues(rep, category) if needle in i.message]
    assert hits and not rep.passed, rep.format()


def test_desktop_rules(project):
    root, v = project
    (root / "spacefractions/ui/__init__.py").write_text(H.format(t="x") + "from spacefractions.game import Game\n")
    msgs = [i.message for i in issues(v(), "Design conformance")]
    assert any("tkinter" in m for m in msgs)
    (root / "spacefractions/ui/__init__.py").write_text(H.format(t="x") + "import tkinter\nimport http.server\n")
    msgs = [i.message for i in issues(v(), "Design conformance")]
    assert any("web framework" in m for m in msgs)
    (root / "spacefractions/ui/__init__.py").write_text(H.format(t="x") + "import tkinter\n")
    (root / "main.py").write_text(H.format(t="x") + "print('hi')\n")
    assert any("main.py does not import" in i.message for i in v().issues)


def test_import_guarded_by_import_error_is_allowed(project):
    root, v = project
    (root / "spacefractions/user/__init__.py").write_text(
        H.format(t="x") + "try:\n    import numpy\nexcept ImportError:\n    numpy = None\n", encoding="utf-8")
    assert v().passed


def test_relative_imports_are_resolved(project):
    root, v = project
    (root / "spacefractions/question/model.py").write_text(H.format(t="x") + "class Q:\n    pass\n", encoding="utf-8")
    (root / "spacefractions/question/bank.py").write_text(H.format(t="x") + "from .model import Q\n", encoding="utf-8")
    assert v().passed
    (root / "spacefractions/question/bank.py").write_text(H.format(t="x") + "from .model import Missing\n", encoding="utf-8")
    assert any("Missing" in i.message for i in issues(v(), "Hallucination"))


def test_docs_and_traceability_rules(project):
    root, v = project
    (root / "DECISIONS.md").write_text("# Decisions\n## C01\n", encoding="utf-8")
    (root / "traceability_matrix.csv").write_text(
        "requirement_id,requirement,component,code_files,test_files\nFR-1,x,G,spacefractions/ghost.py,tests/test_core.py\n",
        encoding="utf-8")
    msgs = " ".join(i.message for i in issues(v(), "Traceability & documentation"))
    assert "C02" in msgs and "ASR-2" in msgs and "ghost.py" in msgs


def test_missing_packages_and_files(project):
    root, v = project
    (root / "spacefractions/admin/__init__.py").unlink()
    (root / "README.md").unlink()
    (root / "Dockerfile").unlink()
    (root / "AGENTS.md").unlink()
    rep = v()
    assert any("admin" in i.message for i in issues(rep, "Design conformance"))
    assert any("README.md" in i.message for i in rep.issues)
    assert any("Dockerfile" in i.message for i in rep.issues)
    assert any("AGENTS.md" in i.message for i in rep.issues)


def test_failing_and_too_few_tests(project):
    root, v = project
    (root / "tests/test_core.py").write_text(
        H.format(t="x") + "import unittest\nclass T(unittest.TestCase):\n"
        "    def test_a(self):\n        self.assertEqual(1, 2)\n"
        "    def test_b(self):\n        import spacefractions.nope\n", encoding="utf-8")
    rep = v()
    cats = {i.category for i in rep.issues if i.check == "tests"}
    assert "Functional" in cats and "Hallucination" in cats
    assert any("at least 8" in i.message for i in rep.issues)


@pytest.mark.parametrize("trace,cat", [
    ("ModuleNotFoundError: No module named 'x'", "Hallucination"),
    ("AttributeError: 'Game' object has no attribute 'foo'", "Hallucination"),
    ("AssertionError: 1 != 2", "Functional"),
    ("RecursionError: maximum recursion depth exceeded", "System"),
    ("sqlite3.OperationalError: no such table", "Reliability"),
])
def test_test_failure_classification(trace, cat):
    assert classify_test_problem(trace) == cat


def test_bug_report_tracks_fixes(project):
    root, v = project
    good = (root / "spacefractions/user/__init__.py").read_text()
    (root / "spacefractions/user/__init__.py").write_text(H.format(t="x") + "import requests\n")
    assert not v().passed
    (root / "spacefractions/user/__init__.py").write_text(good)
    assert v().passed
    data = json.loads((root / ".agent/bug_report.json").read_text())
    assert data["rounds"] == 2 and data["final_passed"] is True
    assert data["by_category"]["Hallucination"] == {"found": 1, "fixed": 1}


# ---- the final gate inside the agent loop -------------------------------------------------
def _agent(tmp_path, script, max_gate_rounds=3):
    box = ToolBox(root=tmp_path / "ws", test_timeout=60)
    v = Verifier(box.root, ARCH, run_tests=box.run_tests_raw)
    model = FakeModel(script)
    events = []
    agent = Agent(llm=model, toolbox=box, system_prompt="S", tracker=CostTracker(DEFAULTS["pricing_per_million"]),
                  verifier=v, max_gate_rounds=max_gate_rounds, on_event=lambda k, d: events.append((k, d)))
    return agent, model, events


def test_gate_sends_issues_back_then_accepts_the_fix(tmp_path):
    files = valid_files(ARCH)
    broken = dict(files)
    broken["spacefractions/user/__init__.py"] = H.format(t="x") + "import requests\n"
    script = script_writing(broken, "Done!") + script_writing(
        {"spacefractions/user/__init__.py": files["spacefractions/user/__init__.py"]}, "Fixed.")
    agent, model, events = _agent(tmp_path, script)
    res = agent.run("go")
    assert res.status == "completed" and res.verified is True and res.gate_rounds == 1
    gate_msgs = [m["content"] for m in agent.messages if m["role"] == "user" and "final gate" in m["content"]]
    assert len(gate_msgs) == 1 and "requests" in gate_msgs[0]
    assert [d["passed"] for k, d in events if k == "verify"] == [False, True]


def test_gate_gives_up_after_max_rounds(tmp_path):
    broken = dict(valid_files(ARCH))
    broken["README.md"] = "x"
    del broken["DECISIONS.md"]
    agent, model, _ = _agent(tmp_path, script_writing(broken), max_gate_rounds=2)
    res = agent.run("go")
    assert res.status == "completed_with_issues" and res.verified is False and res.gate_rounds == 2
