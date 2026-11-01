"""One function that does a full run. The CLI uses it now; the GUI (Step 5) will too."""
from __future__ import annotations

import json
from pathlib import Path

from .config import make_client, provider_of, resolve_model, resolve_pricing
from .llm import CostTracker
from .loop import Agent, EventFn, RunResult
from .preprocess import build as preprocess_build
from .prompts import build_system_prompt, build_task_prompt
from .tools import ToolBox, _fn
from .verify import Verifier


def prepare(doc: Path, views: Path, out_dir: Path) -> dict:
    """Step 1 (no tokens): parse the inputs and save the JSON next to the agent logs."""
    arch = preprocess_build(Path(doc), Path(views), target="desktop")
    log_dir = Path(out_dir) / ".agent"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "architecture.json").write_text(json.dumps(arch, indent=2, ensure_ascii=False), encoding="utf-8")
    return arch


def create_agent(cfg: dict, arch: dict, out_dir: Path, on_event: EventFn | None = None,
                 llm=None) -> Agent:
    """Wire the parts together. `llm` can be injected (tests use a fake model)."""
    tracker = CostTracker(pricing=resolve_pricing(cfg))
    if llm is None:
        llm = make_client(cfg)
    model = resolve_model(cfg)
    toolbox = ToolBox(root=Path(out_dir))
    verifier = Verifier(root=toolbox.root, arch=arch, run_tests=toolbox.run_tests_raw)
    agent = Agent(llm=llm, toolbox=toolbox, system_prompt=build_system_prompt(arch, model),
                  tracker=tracker, max_steps=int(cfg["max_steps"]), budget_usd=float(cfg["budget_usd"]),
                  on_event=on_event, verifier=verifier,
                  meta={"provider": provider_of(cfg), "model": model,
                        "note": "" if provider_of(cfg) == "deepseek" else "TEST RUN on a free provider - not the "
                                                                          "DeepSeek run required by the task"})
    # the model can also run the same gates itself before finishing
    toolbox.register(VERIFY_SCHEMA, lambda: agent._verify("tool").format())
    return agent


VERIFY_SCHEMA = _fn("verify", "Run ALL quality gates (syntax, imports, architecture rules, security, headers, "
                              "DECISIONS.md, traceability, tests). The same gates run automatically when you "
                              "finish; call this before finishing.", {}, [])


SKIP = {".agent", "__pycache__", ".pytest_cache"}


def existing_files(out_dir: Path) -> list[str]:
    """Files left by an earlier (interrupted) run, excluding the agent's own logs."""
    root = Path(out_dir)
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and not any(part in SKIP for part in p.relative_to(root).parts))


def task_for(out_dir: Path) -> str:
    return build_task_prompt(existing_files(out_dir))


def run_pipeline(cfg: dict, doc: Path, views: Path, out_dir: Path,
                 on_event: EventFn | None = None, llm=None, task: str | None = None) -> RunResult:
    task = task or task_for(out_dir)     # decided before prepare() writes .agent/
    arch = prepare(doc, views, out_dir)
    if on_event:
        on_event("preprocessed", {"conflicts": len(arch["conflicts"]), "diagrams": arch["views"]["count"]})
    agent = create_agent(cfg, arch, out_dir, on_event, llm)
    return agent.run(task)
