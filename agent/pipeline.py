"""Session setup (the "QueryEngine" of how-claude-code-works, simplified): one function turns a
task + input files + settings into a ready agent. The CLI and the GUI both use it."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .config import make_client, provider_of, resolve_model, resolve_pricing
from .context import DEFAULT_TASK, build_system_prompt, build_task_message, find_memory_files
from .llm import CostTracker
from .loop import Agent, EventFn, RunResult
from .permissions import PermissionPolicy
from .preprocess import build as preprocess_build
from .tools import ToolBox, _fn
from .verify import Verifier, derive_contract

SKIP = {".agent", "__pycache__", ".pytest_cache"}
Confirm = Callable[[str, str, dict], bool]

VERIFY_SCHEMA = _fn("verify", "Run all quality gates (syntax, imports, architecture rules, security, headers, "
                              "documentation, traceability, tests). The same gates run automatically when you "
                              "finish; call this before finishing.", {}, [])


def existing_files(out_dir: Path) -> list[str]:
    """Files left by an earlier (interrupted) run, excluding the agent's own logs."""
    root = Path(out_dir)
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and not any(part in SKIP for part in p.relative_to(root).parts))


def task_for(out_dir: Path, task: str | None = None) -> str:
    return build_task_message(task or DEFAULT_TASK, existing_files(out_dir))


def read_inputs(paths: list[Path], out_dir: Path, target: str) -> tuple[dict | None, list[tuple[str, str]]]:
    """Architecture documents (a Markdown file plus a file with @startuml diagrams) are preprocessed
    to JSON; every other input is attached as text."""
    texts = {}
    for p in [Path(x) for x in paths]:
        try:
            texts[p] = p.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable: skipped
    views = next((p for p, t in texts.items() if "@startuml" in t), None)
    doc = next((p for p, t in texts.items() if p != views and p.suffix.lower() == ".md"
                and ("Requirement ID" in t or "Traceability" in t or "Architecture" in t)), None)
    arch = None
    raw: list[tuple[str, str]] = []
    if views and doc:
        arch = preprocess_build(doc, views, target=target)
        log_dir = Path(out_dir) / ".agent"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "architecture.json").write_text(json.dumps(arch, indent=2, ensure_ascii=False), encoding="utf-8")
    for p, t in texts.items():
        if arch is not None and p in (views, doc):
            continue
        raw.append((p.name, t))
    return arch, raw


def create_agent(cfg: dict, out_dir: Path, *, inputs: list[Path] | None = None, on_event: EventFn | None = None,
                 llm=None, confirm: Confirm | None = None) -> tuple[Agent, dict | None]:
    out = Path(out_dir)
    target = cfg.get("target", "desktop")
    arch, raw = read_inputs(list(inputs or []), out, target)
    if on_event and arch:
        on_event("preprocessed", {"conflicts": len(arch["conflicts"]), "diagrams": arch["views"]["count"]})
    # workspace trust for project memory (CLAUDE.md / CLAW.md / AGENTS.md)
    trusted = str(out.resolve()) in cfg.get("trusted_folders", [])
    if not trusted and find_memory_files(out) and confirm is not None:
        if confirm("trust_folder", "The output folder contains CLAUDE.md/CLAW.md/AGENTS.md. Load it as project "
                                   "memory (instructions for the agent)?", {"path": str(out)}):
            trusted = True
            cfg.setdefault("trusted_folders", []).append(str(out.resolve()))
    tracker = CostTracker(pricing=resolve_pricing(cfg))
    if llm is None:
        llm = make_client(cfg)
    model = resolve_model(cfg)
    policy = PermissionPolicy(workspace=out, mode=cfg.get("permission_mode", "workspace-write"),
                              allow_rules=list(cfg.get("allow_rules", [])), deny_rules=list(cfg.get("deny_rules", [])),
                              ask_rules=list(cfg.get("ask_rules", [])), confirm=confirm,
                              log_path=out / ".agent" / "permissions.jsonl")
    toolbox = ToolBox(root=out, policy=policy)
    verifier = None
    contract_text = ""
    if cfg.get("verify_before_finish", True):
        contract = derive_contract(arch, target)
        verifier = Verifier(root=toolbox.root, arch=arch, run_tests=toolbox.run_tests_raw, contract=contract)
        contract_text = contract.describe()
    system = build_system_prompt(workspace=out, model=model, mode=policy.mode, arch=arch, raw_files=raw,
                                 contract_text=contract_text, trusted_memory=trusted)
    meta = {"provider": provider_of(cfg), "model": model, "permission_mode": policy.mode, "target": target,
            "inputs": [Path(p).name for p in (inputs or [])],
            "note": "" if provider_of(cfg) == "deepseek" else
            "TEST RUN on a free provider - not the DeepSeek run required by the task"}
    agent = Agent(llm=llm, toolbox=toolbox, system_prompt=system, tracker=tracker,
                  max_steps=int(cfg["max_steps"]), budget_usd=float(cfg["budget_usd"]),
                  on_event=on_event, verifier=verifier, meta=meta)
    if verifier is not None:
        toolbox.register(VERIFY_SCHEMA, lambda: agent._verify("tool").format())
    return agent, arch


def run_pipeline(cfg: dict, inputs: list[Path], out_dir: Path, task: str | None = None,
                 on_event: EventFn | None = None, llm=None, confirm: Confirm | None = None) -> RunResult:
    message = task_for(out_dir, task)            # decided before any .agent/ file is written
    agent, _arch = create_agent(cfg, out_dir, inputs=inputs, on_event=on_event, llm=llm, confirm=confirm)
    agent.meta["task"] = (task or DEFAULT_TASK)[:500]
    return agent.run(message)
