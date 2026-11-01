"""Tests for agent.tools (the sandboxed tool layer)."""
import pytest

from agent.tools import ToolBox, ToolError


@pytest.fixture
def box(tmp_path):
    return ToolBox(root=tmp_path / "ws", test_timeout=60)


# ---- sandbox ------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["../outside.txt", "a/../../x.txt", "/etc/passwd", "C:/Windows/x.txt", "C:\\x.txt"])
def test_paths_cannot_escape_workspace(box, bad):
    res = box.execute("write_file", {"path": bad, "content": "x"})
    assert not res.ok and "ERROR" in res.content


def test_agent_log_folder_is_protected(box):
    assert not box.execute("write_file", {"path": ".agent/hack.txt", "content": "x"}).ok


def test_unknown_tool_and_bad_args(box):
    assert "unknown tool" in box.execute("rm_rf", {}).content
    assert "bad arguments" in box.execute("read_file", {"nope": 1}).content


# ---- file tools -----------------------------------------------------------------------
def test_write_read_roundtrip_creates_folders(box):
    assert box.execute("write_file", {"path": "pkg/sub/m.py", "content": "a = 1\nb = 2\n"}).ok
    out = box.execute("read_file", {"path": "pkg/sub/m.py"}).content
    assert "    1  a = 1" in out and "    2  b = 2" in out
    assert "pkg/sub/m.py" in box.files_written


def test_read_range(box):
    box.execute("write_file", {"path": "f.txt", "content": "\n".join(f"l{i}" for i in range(1, 11))})
    out = box.execute("read_file", {"path": "f.txt", "start_line": 3, "end_line": 4}).content
    assert "l3" in out and "l4" in out and "l5" not in out and "[lines 3-4 of 10]" in out


def test_edit_unique_ambiguous_missing(box):
    box.execute("write_file", {"path": "m.py", "content": "x = 1\nx = 1\ny = 2\n"})
    assert "occurs 2 times" in box.execute("edit_file", {"path": "m.py", "old_str": "x = 1", "new_str": "x = 3"}).content
    assert "not found" in box.execute("edit_file", {"path": "m.py", "old_str": "z", "new_str": "q"}).content
    assert box.execute("edit_file", {"path": "m.py", "old_str": "y = 2", "new_str": "y = 5"}).ok
    assert box.execute("edit_file", {"path": "m.py", "old_str": "x = 1", "new_str": "x = 9", "replace_all": True}).ok
    assert (box.root / "m.py").read_text() == "x = 9\nx = 9\ny = 5\n"


def test_search_and_list(box):
    box.execute("write_file", {"path": "a/one.py", "content": "def play():\n    pass\n"})
    box.execute("write_file", {"path": "b/two.py", "content": "play()\n"})
    out = box.execute("search", {"pattern": r"play\("}).content
    assert "a/one.py:1:" in out and "b/two.py:1:" in out
    tree = box.execute("list_dir", {}).content
    assert "a/" in tree and "one.py" in tree


def test_check_syntax(box):
    box.execute("write_file", {"path": "ok.py", "content": "x = 1\n"})
    assert "OK: 1" in box.execute("check_syntax", {}).content
    box.execute("write_file", {"path": "bad.py", "content": "def f(:\n"})
    out = box.execute("check_syntax", {}).content
    assert "SYNTAX ERRORS" in out and "bad.py:1" in out


def test_todo_write(box):
    out = box.execute("todo_write", {"items": [{"content": "plan", "status": "done"},
                                               {"content": "code", "status": "in_progress"}]}).content
    assert "Plan (1/2 done)" in out and "[>] code" in out
    assert not box.execute("todo_write", {"items": [{"content": "x", "status": "maybe"}]}).ok


# ---- isolated test runner (real subprocess) ------------------------------------------------
def _project(box, test_body):
    box.execute("write_file", {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"})
    box.execute("write_file", {"path": "tests/__init__.py", "content": ""})
    box.execute("write_file", {"path": "tests/test_calc.py", "content":
                               "import unittest\nfrom calc import add\n\n"
                               "class T(unittest.TestCase):\n    def test_add(self):\n" + test_body})


def test_run_tests_pass(box):
    _project(box, "        self.assertEqual(add(1, 2), 3)\n")
    out = box.execute("run_tests", {}).content
    assert out.startswith("PASS") and "Ran 1 test" in out
    assert box.test_runs[-1]["ok"] is True


def test_run_tests_failure_is_reported(box):
    _project(box, "        self.assertEqual(add(1, 2), 4)\n")
    out = box.execute("run_tests", {}).content
    assert out.startswith("FAIL") and "AssertionError" in out


def test_run_tests_import_error_and_missing_dir(box):
    box.execute("write_file", {"path": "tests/__init__.py", "content": ""})
    box.execute("write_file", {"path": "tests/test_x.py", "content": "import does_not_exist_module\n"})
    out = box.execute("run_tests", {}).content
    assert "does_not_exist_module" in out
    assert "not found" in box.execute("run_tests", {"start_dir": "nope"}).content


def test_run_tests_timeout(tmp_path):
    box = ToolBox(root=tmp_path / "ws", test_timeout=3)
    _project(box, "        while True:\n            pass\n")
    assert "TIMEOUT" in box.execute("run_tests", {}).content
