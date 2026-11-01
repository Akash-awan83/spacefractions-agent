"""The tools the model can call, modelled on Claude Code's core tool set
(Read, Write, Edit, Glob/Grep, Bash-for-tests, TodoWrite) but sandboxed:

  * every path is resolved inside ONE workspace folder (the output directory);
    '..', absolute paths and symlinks that escape it are refused;
  * there is no general shell: the only things that execute are Python's own
    compiler (syntax check) and the isolated unittest runner (runner.py).

This is the same idea as a safety layer between an AI agent and the OS:
the model proposes, the tool layer decides what is allowed.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .runner import runner_command

MAX_OUTPUT = 12_000          # chars returned to the model per tool call
MAX_WRITE = 300_000          # chars per file
MAX_READ_LINES = 600
SKIP_DIRS = {"__pycache__", ".git", ".agent", ".venv", "venv", "node_modules", ".pytest_cache"}
PROTECTED_DIRS = {".agent"}  # the agent's own logs; the model may not touch them


class ToolError(Exception):
    """A problem the model should see and fix (bad path, ambiguous edit, ...)."""


@dataclass
class ToolResult:
    ok: bool
    content: str


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit - 300]
    return f"{head}\n... [truncated {len(text) - len(head)} chars] ...\n{text[-250:]}"


# --------------------------------------------------------------------------- schemas
def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    params = {"type": "object", "properties": properties}
    if required:  # an empty "required" list is rejected by some stricter schema validators
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


TOOL_SCHEMAS = [
    _fn("list_dir", "List files and folders in the workspace (2 levels deep by default).",
        {"path": {"type": "string", "description": "Folder relative to the workspace root. Default '.'"},
         "depth": {"type": "integer", "description": "1-4, default 2"}}, []),
    _fn("read_file", "Read a text file with line numbers. Use start_line/end_line for long files.",
        {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
        ["path"]),
    _fn("write_file", "Create or overwrite a file with the COMPLETE content. Parent folders are created.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("edit_file", "Replace an exact snippet in a file. old_str must occur exactly once "
                     "(unless replace_all is true). Include enough context to make it unique.",
        {"path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"},
         "replace_all": {"type": "boolean"}}, ["path", "old_str", "new_str"]),
    _fn("search", "Regex search across workspace files. Returns path:line: text.",
        {"pattern": {"type": "string"}, "path": {"type": "string", "description": "Default '.'"},
         "glob": {"type": "string", "description": "File name filter, default '*.py'"}}, ["pattern"]),
    _fn("check_syntax", "Compile Python files without running them and report syntax errors.",
        {"paths": {"type": "array", "items": {"type": "string"},
                   "description": "Files to check; omit to check every .py file"}}, []),
    _fn("run_tests", "Run the project's unittest suite in an isolated process and report results.",
        {"start_dir": {"type": "string", "description": "Default 'tests'"},
         "pattern": {"type": "string", "description": "Default 'test*.py'"}}, []),
    _fn("todo_write", "Replace the task plan. Keep exactly one item in_progress while working.",
        {"items": {"type": "array", "items": {"type": "object", "properties": {
            "content": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}},
            "required": ["content", "status"]}}}, ["items"]),
]


# ----------------------------------------------------------------------------- toolbox
@dataclass
class ToolBox:
    root: Path
    test_timeout: int = 120
    on_file_written: Callable[[str], None] | None = None
    todos: list = field(default_factory=list)
    files_written: set = field(default_factory=set)
    test_runs: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        self.root = Path(self.root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def schemas(self) -> list[dict]:
        return TOOL_SCHEMAS + [schema for schema, _ in self.extra.values()]

    # ---- sandbox -------------------------------------------------------------------
    def resolve(self, rel: str | None, for_write: bool = False) -> Path:
        rel = (rel or ".").strip().replace("\\", "/")
        if not rel:
            rel = "."
        if os.path.isabs(rel) or re.match(r"^[A-Za-z]:", rel):
            raise ToolError(f"absolute paths are not allowed: {rel!r}; use a path relative to the workspace")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"path escapes the workspace: {rel!r}")
        parts = p.relative_to(self.root).parts
        if for_write and parts and parts[0] in PROTECTED_DIRS:
            raise ToolError(f"{parts[0]}/ is reserved for the agent's own logs")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    # ---- dispatch ------------------------------------------------------------------
    def execute(self, name: str, args: dict) -> ToolResult:
        if name in self.extra:
            fn = self.extra[name][1]
        else:
            fn = getattr(self, f"tool_{name}", None)
            if fn is None or name not in {s["function"]["name"] for s in TOOL_SCHEMAS}:
                return ToolResult(False, f"ERROR: unknown tool {name!r}")
        try:
            return ToolResult(True, _clip(fn(**(args or {}))))
        except ToolError as e:
            return ToolResult(False, f"ERROR: {e}")
        except TypeError as e:  # wrong/missing arguments from the model
            return ToolResult(False, f"ERROR: bad arguments for {name}: {e}")
        except OSError as e:
            return ToolResult(False, f"ERROR: {type(e).__name__}: {e}")

    # ---- tools ---------------------------------------------------------------------
    def tool_list_dir(self, path: str = ".", depth: int = 2) -> str:
        base = self.resolve(path)
        if not base.is_dir():
            raise ToolError(f"not a directory: {path}")
        depth = max(1, min(int(depth or 2), 4))
        lines = []

        def walk(d: Path, level: int):
            entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            for e in entries:
                if e.name in SKIP_DIRS:
                    continue
                indent = "  " * level
                if e.is_dir():
                    lines.append(f"{indent}{e.name}/")
                    if level + 1 < depth:
                        walk(e, level + 1)
                else:
                    lines.append(f"{indent}{e.name} ({e.stat().st_size} B)")

        walk(base, 0)
        return "\n".join(lines) if lines else "(empty)"

    def tool_read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"file not found: {path}")
        text = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start_line or 1))
        end = min(len(text), int(end_line or start + MAX_READ_LINES - 1), start + MAX_READ_LINES - 1)
        body = "\n".join(f"{i:>5}  {text[i - 1]}" for i in range(start, end + 1))
        more = f"\n[lines {start}-{end} of {len(text)}]" if (start > 1 or end < len(text)) else ""
        return (body or "(empty file)") + more

    def tool_write_file(self, path: str, content: str) -> str:
        if len(content) > MAX_WRITE:
            raise ToolError(f"content too large ({len(content)} chars); split the module")
        p = self.resolve(path, for_write=True)
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        self.files_written.add(self.rel(p))
        if self.on_file_written:
            self.on_file_written(self.rel(p))
        n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        return f"{'Overwrote' if existed else 'Created'} {self.rel(p)} ({n} lines)"

    def tool_edit_file(self, path: str, old_str: str, new_str: str, replace_all: bool = False) -> str:
        p = self.resolve(path, for_write=True)
        if not p.is_file():
            raise ToolError(f"file not found: {path}")
        if not old_str:
            raise ToolError("old_str is empty; use write_file to create content")
        text = p.read_text(encoding="utf-8")
        count = text.count(old_str)
        if count == 0:
            raise ToolError("old_str not found. Re-read the file; whitespace and indentation must match exactly")
        if count > 1 and not replace_all:
            raise ToolError(f"old_str occurs {count} times; add surrounding lines to make it unique")
        text = text.replace(old_str, new_str) if replace_all else text.replace(old_str, new_str, 1)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        self.files_written.add(self.rel(p))
        if self.on_file_written:
            self.on_file_written(self.rel(p))
        return f"Edited {self.rel(p)} ({count if replace_all else 1} replacement(s))"

    def _iter_files(self, base: Path, glob: str):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fnmatch.fnmatch(fn, glob):
                    yield Path(dirpath) / fn

    def tool_search(self, pattern: str, path: str = ".", glob: str = "*.py") -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"invalid regex: {e}")
        hits = []
        for f in self._iter_files(self.resolve(path), glob or "*"):
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{self.rel(f)}:{i}: {line.strip()[:200]}")
                        if len(hits) >= 80:
                            return "\n".join(hits) + "\n[stopped at 80 matches]"
            except OSError:
                continue
        return "\n".join(hits) if hits else "no matches"

    def tool_check_syntax(self, paths: list | None = None) -> str:
        files = [self.resolve(p) for p in paths] if paths else list(self._iter_files(self.root, "*.py"))
        errors = []
        for f in files:
            if not f.is_file():
                errors.append(f"{self.rel(f)}: file not found")
                continue
            try:
                compile(f.read_text(encoding="utf-8"), str(f), "exec")
            except SyntaxError as e:
                errors.append(f"{self.rel(f)}:{e.lineno}:{e.offset}: {e.msg}")
            except ValueError as e:
                errors.append(f"{self.rel(f)}: {e}")
        if errors:
            return f"SYNTAX ERRORS in {len(errors)} file(s):\n" + "\n".join(errors)
        return f"OK: {len(files)} Python file(s) compile"

    def run_tests_raw(self, start_dir: str = "tests", pattern: str = "test*.py") -> dict:
        """Run the unittest suite in an isolated process; return the runner's result dict."""
        self.resolve(start_dir)  # sandbox check only
        fd, out_json = tempfile.mkstemp(suffix=".json", prefix="sf_tests_")
        os.close(fd)
        cmd = runner_command() + [str(self.root), start_dir or "tests", pattern or "test*.py", out_json]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SF_HEADLESS="1")
        if not getattr(sys, "frozen", False):
            # `python -m agent.runner` runs with cwd = workspace, so put our package on the path.
            pkg_parent = str(Path(__file__).resolve().parent.parent)
            env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
        try:
            proc = subprocess.run(cmd, cwd=str(self.root), timeout=self.test_timeout, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags)
            text = Path(out_json).read_text(encoding="utf-8")
            if text.strip():
                res = json.loads(text)
            else:
                err = proc.stderr.decode("utf-8", "replace")[-2000:]
                res = {"ok": False, "fatal": f"runner produced no result (exit {proc.returncode}):\n{err}",
                       "tests_run": 0}
        except subprocess.TimeoutExpired:
            res = {"ok": False, "fatal": f"TIMEOUT after {self.test_timeout}s - a test probably started "
                                         f"a Tk mainloop or an infinite loop", "tests_run": 0}
        except (ValueError, OSError) as e:
            res = {"ok": False, "fatal": f"runner failed: {e}", "tests_run": 0}
        finally:
            try:
                os.remove(out_json)
            except OSError:
                pass
        self.test_runs.append({k: res.get(k) for k in ("ok", "tests_run", "skipped", "seconds")}
                              | {"failures": len(res.get("failures", [])), "errors": len(res.get("errors", []))})
        return res

    def tool_run_tests(self, start_dir: str = "tests", pattern: str = "test*.py") -> str:
        return format_test_result(self.run_tests_raw(start_dir, pattern))

    # ---- extra tools registered at runtime (e.g. `verify`) ---------------------------
    def register(self, schema: dict, fn: Callable[..., str]) -> None:
        self.extra[schema["function"]["name"]] = (schema, fn)

    def tool_todo_write(self, items: list) -> str:
        clean = []
        for it in items or []:
            status = it.get("status", "pending")
            if status not in ("pending", "in_progress", "done"):
                raise ToolError(f"bad status {status!r}")
            clean.append({"content": str(it.get("content", "")).strip(), "status": status})
        self.todos = clean
        return render_todos(clean)


def render_todos(items: list) -> str:
    mark = {"done": "[x]", "in_progress": "[>]", "pending": "[ ]"}
    done = sum(i["status"] == "done" for i in items)
    return f"Plan ({done}/{len(items)} done):\n" + "\n".join(f"{mark[i['status']]} {i['content']}" for i in items)


def format_test_result(res: dict) -> str:
    if res.get("fatal"):
        return f"TESTS DID NOT RUN:\n{res['fatal']}"
    fails, errs = res.get("failures", []), res.get("errors", [])
    head = (f"Ran {res.get('tests_run', 0)} test(s) in {res.get('seconds', 0)}s: "
            f"{len(fails)} failure(s), {len(errs)} error(s), {res.get('skipped', 0)} skipped")
    if res.get("tests_run", 0) == 0:
        return head + "\nNO TESTS FOUND. Put unittest files named test_*.py in tests/ with tests/__init__.py"
    if res.get("ok"):
        return "PASS. " + head
    parts = ["FAIL. " + head]
    for kind, items in (("FAILURE", fails), ("ERROR", errs)):
        for it in items[:6]:
            parts.append(f"--- {kind}: {it['test']}\n{it['trace'][-1500:]}")
    return "\n".join(parts)
