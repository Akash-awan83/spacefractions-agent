"""Tests for agent.context (layered prompt, project memory, compression) and for generality:
nothing about one particular project may be built into the agent."""
from pathlib import Path

from agent.context import (DEFAULT_TASK, build_system_prompt, build_task_message, compress, estimate_tokens,
                           memory_layer)
from agent.pipeline import read_inputs
from agent.preprocess import build
from agent.verify import derive_contract

ROOT = Path(__file__).resolve().parents[1]
AGENT_SOURCES = sorted((ROOT / "agent").glob("*.py"))

LIBRARY_DOC = """# A. Executive Summary
The Library Manager system lets members borrow books.

# B. Traceability & Rationale
| Requirement ID | Short Text | Diagram(s) | Component(s) | Artifact filename(s) | Rationale |
| --- | --- | --- | --- | --- | --- |
| FR-1 | Borrow book | UseCaseDiagram | LoanComponent | loans.py | Members borrow books |

# C. Architecture Overview
* CatalogComponent: keeps the books
* LoanComponent: handles borrowing
"""
LIBRARY_VIEWS = """## DevelopmentView
1. Component — Development View: Component Diagram
```plantuml
@startuml ComponentDiagram
artifact CatalogComponent
artifact LoanComponent
CatalogComponent -- LoanComponent
@enduml
```
"""


def test_agent_code_contains_nothing_specific_to_space_fractions():
    """The professor's point: a general Code Agent. The project must come from the inputs."""
    for f in AGENT_SOURCES:
        if f.name == "preprocess.py":      # its docstring explains the input format, no rules
            continue
        text = f.read_text(encoding="utf-8").lower()
        assert "space fraction" not in text and "spacefractions" not in text, f.name


def test_prompt_without_inputs_is_generic(tmp_path):
    p = build_system_prompt(workspace=tmp_path, model="deepseek-v4-pro", mode="workspace-write")
    for layer in ("You are Simple Code Agent", "# How to work", "# Tools and permissions", "# Environment"):
        assert layer in p
    assert "workspace-write" in p and "Space" not in p and "conflicts" not in p


def test_a_different_architecture_gives_a_different_contract(tmp_path):
    (tmp_path / "doc.md").write_text(LIBRARY_DOC, encoding="utf-8")
    (tmp_path / "views.md").write_text(LIBRARY_VIEWS, encoding="utf-8")
    arch, raw = read_inputs([tmp_path / "doc.md", tmp_path / "views.md"], tmp_path / "out", "any")
    assert arch and arch["project"]["name"] == "Library Manager" and raw == []
    c = derive_contract(arch, "any")
    assert c.root_package == "librarymanager" and c.component_packages == ("catalog", "loan")
    assert c.target == "any" and "main.py" not in c.required_files
    prompt = build_system_prompt(workspace=tmp_path, model="m", mode="workspace-write", arch=arch,
                                 contract_text=c.describe())
    assert "librarymanager/catalog" in prompt and "spacefractions" not in prompt


def test_sfs_contract_comes_from_its_documents():
    arch = build(ROOT / "inputs/Architecture_Documentation.md", ROOT / "inputs/Architecture_View.md")
    c = derive_contract(arch, "desktop")
    assert c.root_package == "spacefractions" and c.component_packages == ("game", "question", "user", "admin")
    assert c.require_decisions and c.require_traceability and "main.py" in c.required_files


def test_non_architecture_inputs_are_attached_as_text(tmp_path):
    (tmp_path / "notes.txt").write_text("Build a calculator.", encoding="utf-8")
    arch, raw = read_inputs([tmp_path / "notes.txt"], tmp_path / "out", "any")
    assert arch is None and raw == [("notes.txt", "Build a calculator.")]
    p = build_system_prompt(workspace=tmp_path, model="m", mode="read-only", raw_files=raw)
    assert "# Attached input: notes.txt" in p and "Build a calculator." in p


def test_project_memory_needs_trust(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Always use tabs.", encoding="utf-8")
    assert "Always use tabs." in memory_layer(tmp_path, trusted=True)
    untrusted = memory_layer(tmp_path, trusted=False)
    assert "Always use tabs." not in untrusted and "not trusted" in untrusted
    assert memory_layer(tmp_path / "nothing", trusted=True) == ""


def test_task_message_default_and_resume():
    assert build_task_message("", []) == DEFAULT_TASK
    m = build_task_message("Fix the bug", ["a.py"])
    assert m.startswith("Fix the bug") and "RESUME" in m and "- a.py" in m


# ---- compression pipeline ------------------------------------------------------------------
def _session(n_tools=14, size=2000, same=False):
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "task"}]
    for i in range(n_tools):
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": ("x" if same else str(i)) * size})
    return msgs


def test_level1_snips_old_outputs_but_keeps_the_tail():
    msgs = _session()
    stats = compress(msgs, keep_tail=4)
    assert stats["snipped"] > 0
    assert "snipped" in msgs[3]["content"] and "snipped" not in msgs[-1]["content"]


def test_level2_dedupes_identical_outputs():
    msgs = _session(n_tools=6, size=300, same=True)
    stats = compress(msgs, keep_tail=2)
    assert stats["deduped"] >= 1 and any("duplicate" in (m.get("content") or "") for m in msgs)


def test_level3_summarises_and_keeps_tool_pairs_intact():
    msgs = _session()
    before = estimate_tokens(msgs)
    stats = compress(msgs, keep_tail=4, summarize=lambda text: "made 14 reads", summarize_above=10,
                     recent_files=["a.py", "b.py"])
    assert stats["summarized"] > 0 and estimate_tokens(msgs) < before
    assert msgs[2]["role"] == "user" and "made 14 reads" in msgs[2]["content"] and "a.py, b.py" in msgs[2]["content"]
    # every tool result still follows the assistant message that called it
    for i, m in enumerate(msgs):
        if m["role"] == "tool":
            prev = msgs[i - 1]
            assert prev["role"] in ("assistant", "tool")
