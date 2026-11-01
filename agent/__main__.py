"""Command line:

    python -m agent set-key                      store the DeepSeek key (typed hidden)
    python -m agent ping                         one tiny API call to test key + model
    python -m agent run --out <dir> [--doc ..] [--views ..] [--budget 0.9] [--max-steps 80]
    python -m agent preprocess --doc .. --views .. --out build/architecture.json
    python -m agent gui                          the desktop window (same as the .exe)
"""
from __future__ import annotations

import argparse
import getpass
import sys
import threading
import time
from pathlib import Path

from . import preprocess
from .events import format_event
from .config import (PROVIDERS, config_path, load_config, make_client, mask_key, resolve_api_key,
                     resolve_model, resolve_pricing, save_config, set_api_key, set_model)
from .llm import CostTracker, LLMError

DEFAULT_DOC = Path("inputs/Architecture_Documentation.md")
DEFAULT_VIEWS = Path("inputs/Architecture_View.md")


class _Ticker:
    """Shows 'waiting for the model... 25s' while a model call is in flight,
    so a slow free-tier response does not look like a frozen program."""
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self.stop()
        self._stop = threading.Event()
        stop, t0 = self._stop, time.time()

        def run():
            while not stop.wait(5):
                sys.stdout.write(f"\r   waiting for the model... {int(time.time() - t0)}s ")
                sys.stdout.flush()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread:
            self._stop.set()
            self._thread.join()
            self._thread = None
            sys.stdout.write("\r" + " " * 48 + "\r")
            sys.stdout.flush()


_ticker = _Ticker()


def _print_event(kind: str, data: dict) -> None:
    if kind != "thinking":
        _ticker.stop()
    line = format_event(kind, data)
    if line:
        print(line, flush=True)
    if kind == "thinking":
        _ticker.start()


def _apply_provider(cfg: dict, args) -> str:
    p = getattr(args, "provider", None) or cfg["provider"]
    cfg["provider"] = p
    if getattr(args, "model", None):
        set_model(cfg, args.model, p)
    return p


def cmd_set_key(args) -> int:
    cfg = load_config()
    p = args.provider or "deepseek"
    key = getpass.getpass(f"{PROVIDERS[p]['label']} API key (input hidden): ").strip()
    if not key:
        print("No key entered; nothing changed.")
        return 1
    set_api_key(cfg, key, p)
    path = save_config(cfg)
    print(f"Saved {p} key {mask_key(key)} to {path}")
    return 0


def cmd_ping(args) -> int:
    cfg = load_config()
    p = _apply_provider(cfg, args)
    key = resolve_api_key(cfg)
    print(f"provider={p} model={resolve_model(cfg) or '(not set)'} key={mask_key(key)} config={config_path()}")
    try:
        client = make_client(cfg, timeout=120, max_retries=1)
        msg, usage, _ = client.chat([{"role": "user", "content": "Reply with exactly: pong"}],
                                    thinking=False, max_tokens=10)
    except LLMError as e:
        print("FAILED:", e)
        return 1
    tracker = CostTracker(resolve_pricing(cfg))
    delta = tracker.add(usage)
    print(f"reply={msg.get('content')!r}  tokens in/out={delta['prompt']}/{delta['output']}  "
          f"cost=${delta['cost_usd']:.6f}")
    return 0


def cmd_run(args) -> int:
    from .pipeline import run_pipeline
    cfg = load_config()
    if args.budget is not None:
        cfg["budget_usd"] = args.budget
    if args.max_steps is not None:
        cfg["max_steps"] = args.max_steps
    p = _apply_provider(cfg, args)
    if not resolve_api_key(cfg):
        print(f"No {p} API key. Run:  python -m agent set-key --provider {p}")
        return 1
    if not resolve_model(cfg):
        print(f"No model set for {p}. Pass --model <id>.")
        return 1
    if p != "deepseek":
        print(f"NOTE: {p} is for testing only; the task requires DeepSeek for the real run.")
    out = Path(args.out)
    if out.exists() and any(p.name != ".agent" for p in out.iterdir()) and not (args.force or args.resume):
        print(f"Output folder {out} already contains files.\n"
              f"  --resume  continue the earlier (interrupted) run in this folder\n"
              f"  or choose an empty folder.")
        return 1
    print(f"Running {p}:{resolve_model(cfg)} | budget ${cfg['budget_usd']:.2f} | max {cfg['max_steps']} steps | out {out}")
    try:
        result = run_pipeline(cfg, Path(args.doc), Path(args.views), out, on_event=_print_event)
    except KeyboardInterrupt:
        _ticker.stop()
        print("\nInterrupted.")
        return 130
    _ticker.stop()
    c = result.cost
    print(f"\nStatus: {result.status}  |  steps {result.steps}  |  time {result.seconds}s")
    print(f"Tokens: prompt {c['prompt_tokens']} (cache hit rate {c['cache_hit_rate']:.0%}), "
          f"output {c['completion_tokens']} (reasoning {c['reasoning_tokens']})")
    print(f"COST: ${c['cost_usd']:.4f}  ({result.meta.get('provider')}:{result.meta.get('model')})")
    if result.meta.get("note"):
        print("NOTE:", result.meta["note"])
    print(f"Files written: {len(result.files_written)}  |  logs: {out / '.agent'}")
    if result.verified is not None:
        print(f"Verification: {'PASSED' if result.verified else 'NOT PASSED'} "
              f"(final-gate rounds used: {result.gate_rounds}; see .agent/bug_report.json)")
    if result.error:
        print("Error:", result.error)
    return 0 if result.status == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--run-tests"]:          # hook used by the frozen .exe
        from .runner import main as runner_main
        return runner_main(argv[1:])
    if argv[:1] == ["--run-script"]:
        from .runner import run_script
        return run_script(argv[1])
    ap = argparse.ArgumentParser(prog="python -m agent", description="SpaceFractions Code Agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    providers = list(PROVIDERS)
    k = sub.add_parser("set-key", help="store an API key (DeepSeek by default)")
    k.add_argument("--provider", choices=providers)
    pg = sub.add_parser("ping", help="test the API key with one tiny call")
    pg.add_argument("--provider", choices=providers)
    pg.add_argument("--model")
    r = sub.add_parser("run", help="generate the project")
    r.add_argument("--provider", choices=providers, help="deepseek (default) or a free provider for testing")
    r.add_argument("--model")
    r.add_argument("--doc", default=str(DEFAULT_DOC))
    r.add_argument("--views", default=str(DEFAULT_VIEWS))
    r.add_argument("--out", required=True)
    r.add_argument("--budget", type=float)
    r.add_argument("--max-steps", type=int)
    r.add_argument("--resume", action="store_true", help="continue an interrupted run in the same folder")
    r.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("preprocess", help="only run Step 1", add_help=False)
    sub.add_parser("gui", help="open the desktop window")
    if argv[:1] == ["preprocess"]:
        return preprocess.main(argv[1:])
    args = ap.parse_args(argv)
    if args.cmd == "gui":
        from .gui import main as gui_main
        return gui_main()
    return {"set-key": cmd_set_key, "ping": cmd_ping, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
