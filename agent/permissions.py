"""Permission system - modelled on the references:

* claw-code's three permission modes: ``read-only``, ``workspace-write`` (default) and
  ``danger-full-access``; in workspace-write, file edits inside the workspace are allowed
  while shell execution sits behind an explicit decision.
* how-claude-code-works' layered defence (Permission & Security chapter), simplified:
    1. permission mode          - what kind of operation is possible at all
    2. rule layer               - user patterns: allow / deny / ask
    3. Bash analysis            - static checks on the command (tokenised, not one regex)
    4. tool-level checks        - each tool validates its own input (paths stay in the workspace)
    5. isolation                - commands run in the workspace folder, with a timeout
    6. user confirmation        - "ask" decisions go to the person (GUI dialog / CLI prompt)
  If any layer says no, the action does not run. Every decision is logged.
"""
from __future__ import annotations

import fnmatch
import json
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_MODE = "workspace-write"

READ_ONLY_TOOLS = {"read_file", "list_dir", "glob_search", "grep_search", "todo_write", "check_syntax"}
WRITE_TOOLS = {"write_file", "edit_file"}
EXEC_TOOLS = {"bash", "run_tests", "verify"}

# Bash commands that only read, or that run the project's own tests: allowed without asking
# in workspace-write mode (unless an analysis check objects).
SAFE_COMMANDS = {
    "ls", "dir", "pwd", "cd", "cat", "type", "head", "tail", "wc", "echo", "find", "findstr", "grep",
    "tree", "where", "which", "sort", "diff", "fc", "more", "whoami", "date", "ver",
}
SAFE_PREFIXES = (
    "python -m unittest", "python -m pytest", "python -m py_compile", "python --version", "py -m unittest",
    "python3 -m unittest", "python3 -m pytest", "git status", "git diff", "git log", "git show",
    "node --version", "npm test", "npm run test",
)
# Never run, whatever the mode or the rules.
HARD_DENY = [
    (r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|\*|[A-Za-z]:\\?)(\s|$)", "recursive delete of a root or home"),
    (r"\b(format|mkfs\S*|diskpart|fdisk)\b", "disk formatting"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "shutting the machine down"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"-(enc|encodedcommand)\b", "encoded PowerShell command"),
    (r"\breg\s+(add|delete)\b|\bsetx\b|\bschtasks\b|\bbcdedit\b", "changing system settings"),
]
DESTRUCTIVE = [
    (r"\brm\b.*\s-[a-zA-Z]*r", "recursive delete"),
    (r"\b(del|erase)\b.*\s/[sq]", "bulk delete"),
    (r"\b(rmdir|rd)\b.*\s/s", "recursive directory delete"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push\b.*--force)", "destructive git operation"),
]
NETWORK = [
    (r"\b(curl|wget|ssh|scp|sftp|ftp|nc|ncat|telnet)\b", "network access"),
    (r"\b(invoke-webrequest|invoke-restmethod|iwr|irm)\b", "network access"),
    (r"\bgit\s+(push|pull|fetch|clone)\b", "network git operation"),
    (r"\b(pip|pip3|npm|yarn|pnpm|choco|winget)\s+(install|add|i)\b", "installing packages from the internet"),
]
PRIVILEGE = [(r"\b(sudo|runas|su)\b", "privilege escalation")]
SECRETS = [
    (r"\b(printenv|env)\b\s*$", "printing all environment variables"),
    (r"^\s*set\s*$", "printing all environment variables"),
    (r"[%$]\{?\w*(KEY|TOKEN|SECRET|PASSWORD)\w*\}?%?", "reading a secret from the environment"),
]


@dataclass
class Decision:
    verdict: str            # allow | deny | ask
    reason: str
    checks: list = field(default_factory=list)


def split_segments(command: str) -> list[str]:
    """Split a command line on && || ; | & so each part is analysed on its own."""
    parts = re.split(r"\s*(?:&&|\|\||;|\||&)\s*", command.strip())
    return [p for p in parts if p]


def analyse_bash(command: str, workspace: Path) -> Decision:
    """Static checks on a shell command. Returns allow (clearly harmless), deny (never),
    or ask (needs a person). The command is tokenised with shlex, then each segment is checked."""
    cmd = command.strip()
    checks: list[str] = []
    if not cmd:
        return Decision("deny", "empty command", ["empty"])
    low = cmd.lower()
    for rx, why in HARD_DENY:
        if re.search(rx, low):
            return Decision("deny", f"blocked: {why}", ["hard-deny"])
    if "$(" in cmd or "`" in cmd:
        checks.append("command substitution")
    if re.search(r"\|\s*(sh|bash|zsh|cmd|powershell|pwsh|python\d?)\b", low):
        return Decision("deny", "blocked: piping text into a shell or interpreter", ["pipe-to-interpreter"])
    for group, label in ((DESTRUCTIVE, "destructive"), (NETWORK, "network"), (PRIVILEGE, "privilege"),
                         (SECRETS, "secrets")):
        for rx, why in group:
            if re.search(rx, low, re.IGNORECASE):
                checks.append(f"{label}: {why}")
    # redirection or arguments that point outside the workspace
    try:
        tokens = shlex.split(cmd, posix=False)
    except ValueError:
        tokens = cmd.split()
        checks.append("unbalanced quotes")
    root = workspace.resolve()
    for t in tokens:
        t = t.strip("\"'")
        target = t.lstrip(">").strip()
        if not target:
            continue
        if re.search(r"(^|[\\/])\.\.([\\/]|$)", target):     # .. with / or \ (Windows) separators
            checks.append(f"path leaves the workspace: {t}")
        elif re.match(r"^([A-Za-z]:[\\/]|/|\\\\)", target):
            try:
                p = Path(target).resolve()
                if p != root and root not in p.parents:
                    checks.append(f"absolute path outside the workspace: {t}")
            except (OSError, ValueError):
                checks.append(f"unresolvable path: {t}")
    if checks:
        return Decision("ask", "; ".join(checks), checks)
    segments = split_segments(cmd)
    if all(_is_safe_segment(s) for s in segments):
        return Decision("allow", "read-only or test command", ["safe-list"])
    return Decision("ask", "command is not on the safe list", ["not-safe-listed"])


def _is_safe_segment(segment: str) -> bool:
    s = segment.strip().lower()
    if ">" in s:              # redirection writes a file
        return False
    if any(s.startswith(p) for p in SAFE_PREFIXES):
        return True
    first = s.split()[0] if s.split() else ""
    return first in SAFE_COMMANDS


@dataclass
class PermissionPolicy:
    workspace: Path
    mode: str = DEFAULT_MODE
    allow_rules: list = field(default_factory=list)   # e.g. "bash:python -m unittest*"
    deny_rules: list = field(default_factory=list)    # e.g. "bash:*git push*"
    ask_rules: list = field(default_factory=list)
    confirm: Callable[[str, str, dict], bool] | None = None   # (tool, reason, args) -> approved?
    log_path: Path | None = None
    decisions: list = field(default_factory=list)

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"unknown permission mode {self.mode!r}; use one of {MODES}")
        self.workspace = Path(self.workspace).resolve()

    # ------------------------------------------------------------------------------
    def check(self, tool: str, args: dict) -> Decision:
        """Decide, ask the person if needed, log, and return the final decision."""
        d = self._decide(tool, args)
        if d.verdict == "ask":
            if self.confirm is None:
                d = Decision("deny", f"needs approval, but nobody can approve in this run ({d.reason})",
                             d.checks + ["no-confirmer"])
            else:
                approved = bool(self.confirm(tool, d.reason, args))
                d = Decision("allow" if approved else "deny",
                             f"{'approved' if approved else 'denied'} by user ({d.reason})",
                             d.checks + ["user-confirmation"])
        self._log(tool, args, d)
        return d

    def _decide(self, tool: str, args: dict) -> Decision:
        subject = f"{tool}:{args.get('command') or args.get('path') or ''}"
        # layer 2: explicit rules, deny first
        for rule in self.deny_rules:
            if fnmatch.fnmatch(subject, rule):
                return Decision("deny", f"denied by rule {rule!r}", ["rule"])
        if tool == "bash":
            hard = analyse_bash(str(args.get("command", "")), self.workspace)
            if hard.verdict == "deny":
                return hard
        # layer 1: mode
        if self.mode == "read-only" and tool not in READ_ONLY_TOOLS:
            return Decision("deny", f"read-only mode: {tool} is not allowed", ["mode"])
        for rule in self.allow_rules:
            if fnmatch.fnmatch(subject, rule):
                return Decision("allow", f"allowed by rule {rule!r}", ["rule"])
        for rule in self.ask_rules:
            if fnmatch.fnmatch(subject, rule):
                return Decision("ask", f"rule {rule!r} asks for confirmation", ["rule"])
        if tool != "bash":
            return Decision("allow", f"{self.mode}: {tool} allowed (paths are checked by the tool)", ["mode"])
        # layer 3: Bash analysis
        analysis = analyse_bash(str(args.get("command", "")), self.workspace)
        if self.mode == "danger-full-access" and analysis.verdict == "ask":
            return Decision("allow", f"danger-full-access ({analysis.reason})", analysis.checks)
        return analysis

    def _log(self, tool: str, args: dict, d: Decision):
        entry = {"t": round(time.time(), 2), "tool": tool, "mode": self.mode,
                 "subject": str(args.get("command") or args.get("path") or "")[:300],
                 "verdict": d.verdict, "reason": d.reason, "checks": d.checks}
        self.decisions.append(entry)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
