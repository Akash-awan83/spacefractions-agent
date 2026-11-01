"""Runs the generated project's unit tests in a SEPARATE process.

Why a separate process?  Generated code may hang, crash or leave global state
behind; it must never take the agent (or its GUI) down with it.

Why unittest and not pytest?  unittest ships with Python, so the frozen .exe can
run the tests on a machine with no Python installed: the .exe re-launches itself
with `--run-tests` (see runner_command) and this module does the work.

Results are written to a JSON file, not stdout, because a windowed (no-console)
.exe has no usable stdout.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import traceback
import unittest
from pathlib import Path

try:  # make PyInstaller bundle what the generated game may import (see _bundle.py)
    from . import _bundle  # noqa: F401
except ImportError:  # e.g. a Linux box without tkinter: tests can still run
    pass


def runner_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-tests"]
    return [sys.executable, "-m", "agent.runner"]


def run_tests(project_dir: str, start_dir: str = "tests", pattern: str = "test*.py") -> dict:
    project = Path(project_dir).resolve()
    start = (project / start_dir).resolve()
    os.chdir(project)
    sys.path.insert(0, str(project))
    os.environ.setdefault("SF_HEADLESS", "1")  # generated UI code may check this to avoid windows
    t0 = time.time()
    if not start.is_dir():
        return {"ok": False, "fatal": f"test directory not found: {start_dir}", "tests_run": 0}
    # A tests/ folder without __init__.py is not importable as a package; discover it as top level.
    top = project if (start / "__init__.py").exists() else start
    try:
        suite = unittest.TestLoader().discover(str(start), pattern=pattern, top_level_dir=str(top))
    except Exception:  # noqa: BLE001 - report anything discovery throws
        return {"ok": False, "fatal": traceback.format_exc(limit=5), "tests_run": 0}
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)

    def pack(items):
        return [{"test": str(t), "trace": tb[-3000:]} for t, tb in items]

    return {"ok": result.wasSuccessful() and result.testsRun > 0,
            "tests_run": result.testsRun,
            "failures": pack(result.failures),
            "errors": pack(result.errors),
            "skipped": len(result.skipped),
            "seconds": round(time.time() - t0, 2)}


def run_script(script: str) -> int:
    """Run a generated program (e.g. the game's main.py) with this interpreter.
    Used by the .exe's --run-script hook so the game runs without installed Python."""
    import runpy
    path = Path(script).resolve()
    os.chdir(path.parent)
    sys.path.insert(0, str(path.parent))
    sys.argv = [str(path)]
    runpy.run_path(str(path), run_name="__main__")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 4:
        print("usage: runner <project_dir> <start_dir> <pattern> <out_json>", file=sys.stderr)
        return 2
    project_dir, start_dir, pattern, out_json = argv[:4]
    try:
        res = run_tests(project_dir, start_dir, pattern)
    except BaseException:  # noqa: BLE001 - even SystemExit from generated code must be reported
        res = {"ok": False, "fatal": traceback.format_exc(limit=5), "tests_run": 0}
    Path(out_json).write_text(json.dumps(res), encoding="utf-8")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
