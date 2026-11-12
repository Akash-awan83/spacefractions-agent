# Simple Code Agent

A simplified **Claude-Code-style coding agent** built by studying the three reference projects of
Task 1, running on **DeepSeek `deepseek-v4-pro`** through the official API, and shipped as a
**double-click Windows `.exe`** with a small GUI. Like the references, it is a **general** agent: the
user gives it a task in natural language and any input files; it plans, reads, writes, runs commands
under a permission system, tests and verifies its work.

For Task 1 the inputs are `Architecture_Documentation.md` and `Architecture_View.md` (the MAAD
artifacts for the Space Fraction System) and the task is "generate the project they describe as a
desktop application". Nothing about that project is built into the agent: the project name, the
components and the rules the verifier checks are all derived from the input documents.

```
 task (natural language) + input files ──▶ Session setup ──▶ Main loop ◀──▶ DeepSeek V4 Pro
                                            │ context engineering    │
                                            │ (layered prompt,       ▼
                                            │  memory, inputs)   Tool engine ──▶ permission system ──▶ tools
                                            │                        │           (modes, rules, Bash     read/write/edit,
                                            ▼                        ▼            analysis, confirm)     glob/grep, bash,
                                     compression pipeline     final verification gate                   tests, todo, verify
```

## Built from the three references

| Reference | What it contributed |
| --- | --- |
| **anthropics/claude-code** | The product idea: an agent that takes natural-language tasks, works on a codebase with tools, asks before risky actions, and reads project memory (`CLAUDE.md`). |
| **ultraworkers/claw-code** | The harness layout (runtime: conversation loop, config, session persistence, permission policy, system-prompt assembly, usage tracking; tools crate), the tool names (`read_file`, `write_file`, `edit_file`, `glob_search`, `grep_search`, `bash`, `TodoWrite`), the three permission modes (`read-only`, `workspace-write` default, `danger-full-access`), `CLAUDE.md`/`CLAW.md`/`AGENTS.md` memory, `claw prompt "..."` and `claw doctor`, and OpenAI-compatible providers. |
| **Windy3f3f3f3f/how-claude-code-works** | The internals: session manager → main loop → tool execution engine; context engineering (layered system prompt, memory, 4-level compression); uniform tool interface with parallel read-only tools and large results saved to disk; silent recovery ("continue" strategies); the layered permission and security model with Bash analysis and user confirmation; search-and-replace editing; observability (transcripts, cost, permission-decision log). |

### Mechanism by mechanism

| Mechanism (references) | This agent | Simplified because |
| --- | --- | --- |
| Natural-language task as input (`claw prompt "..."`) | GUI **Task** box; `python -m agent prompt "..." --input f --out dir` | — |
| Session manager + main loop | `pipeline.create_agent` (setup) and `loop.Agent.run` | One model call at a time |
| Layered system prompt | `context.build_system_prompt`: identity, how to work, tools & permissions, environment (OS, date, git status), project memory, attached inputs, verification contract — built once, byte-identical on every call (provider cache) | — |
| Project memory `CLAUDE.md` / `CLAW.md` / `AGENTS.md` | Loaded from the workspace **only if the folder is trusted** (asked once, remembered) | — |
| Tools with one interface | `tools.ToolBox`: `read_file`, `write_file`, `edit_file`, `list_dir`, `glob_search`, `grep_search`, `bash`, `check_syntax`, `run_tests`, `todo_write`, `verify` | No web, MCP or sub-agent tools |
| Read-only tools in parallel, writes serialized | `loop._run_calls` (thread pool, results kept in order) | — |
| Large results saved to disk | Over 12,000 chars → `.agent/tool_outputs/…`, the model gets the start and the path | Lower threshold than Claude Code |
| Search-and-replace editing | `edit_file` (`old_str` must match exactly once) | — |
| Permission modes | `read-only` / **`workspace-write`** (default) / `danger-full-access` | — |
| Rule layer | Allow / deny / ask patterns, e.g. `bash:python -m unittest*` allowed, `bash:*git push*` denied | Glob patterns |
| Bash analysis | `permissions.analyse_bash`: tokenised, chained commands split; hard-blocks (recursive root delete, disk formatting, shutdown, fork bomb, encoded PowerShell, system settings, piping into a shell); asks for network, package installs, destructive deletes, privilege, secrets in env vars, paths outside the workspace; allows read-only and test commands | About a dozen groups of checks, not Claude Code's 23 |
| User confirmation | GUI **Allow / Deny** dialog; `y/N` prompt in the CLI; unattended runs refuse | — |
| Workspace isolation | Paths resolved inside the workspace; commands run in it with a timeout; generated tests in a separate process | No OS-level sandbox |
| Compression pipeline | Snip old outputs → dedupe identical outputs → summarise older turns (last resort) → re-list recently edited files | 3 levels instead of 4 (no reversible collapse) |
| Silent recovery | API retries with back-off; cut-off reply continued; empty reply nudged; context-overflow error → compress and retry; bad tool arguments returned to the model | — |
| Hooks (stop hook) | **Final verification gate**: when the model finishes, the verifier runs and blocking issues go back to it (max 3 rounds) | Fixed hook, not user scripts |
| TodoWrite / plan | `todo_write`; the GUI shows the live plan | No separate plan mode |
| Session persistence / resume | `.agent/session.json`, `transcript.jsonl`; `--resume` lists existing files to continue | Resume restarts from the files, not the exact turn |
| Observability | `run_summary.json` (tokens, cache hit rate, cost, peak calls), `permissions.jsonl`, `bug_report.json` | — |
| Health check (`claw doctor`) | `python -m agent ping`, GUI **Test key** | — |
| Streaming, sub-agents, MCP, skills, plan mode | Not implemented | Not needed for one task; the $1 budget argues against several contexts |

## Task-1 specifics, derived from the inputs

When the inputs are an architecture document plus PlantUML views, the agent preprocesses them **without
an LLM** (`preprocess.py`): Markdown tables, OpenAPI, proto, SQL, Kubernetes and 13 diagrams become one
JSON, and **20 cross-view inconsistencies** are reported with evidence, root cause and the responsible
MAAD role (MAAD: Li, Zhang, Zhou, Liang et al., arXiv:2507.21382). The verifier's **contract is derived**
from that JSON (`verify.derive_contract`): root package from the project name, one package per component
in the component diagram, `DECISIONS.md` when there are conflicts, a traceability matrix when there are
requirements, and the desktop gate when the target is `desktop`. With other inputs, other rules follow;
with no inputs, only the general gates apply.

### Verification gates

| Gate | Category (bug taxonomy of *A Survey of Bugs in AI-Generated Code*, arXiv:2512.05239) |
| --- | --- |
| Every file parses | Syntax |
| Every import exists (stdlib or project); `from x import y` really defines `y` | Hallucination |
| Unit tests pass in an isolated process; failures classified from the traceback | Functional / Hallucination / System / Reliability |
| Component packages exist; component packages do not import the UI or tkinter | Design conformance |
| Desktop target: tkinter UI, no web frameworks, `main.py` starts the package | Design conformance |
| No `eval` / `exec` / `subprocess` / `os.system`; SQL never built from strings | Security |
| AI-generated-code header in every file | Code style & standards |
| Required files; `DECISIONS.md` covers every conflict; traceability covers every requirement | Traceability & documentation |

## Use the .exe (Windows, no Python needed)

1. Download `app/CodeAgent.exe` and double-click it (SmartScreen: *More info → Run anyway*).
2. Paste the DeepSeek API key → **Save** → **Test key**.
3. The **Task** box holds the Task-1 instruction and the two sample inputs are listed; change either freely.
4. Choose an empty **Output folder**, keep **workspace-write**, press **Check inputs**, then **Run agent**.
5. Answer **Allow / Deny** if the agent asks to run a command. **Play generated game** runs `main.py`.

The same exe runs the generated tests (`--run-tests`) and the generated program (`--run-script`).

## Command line

```bat
python -m agent set-key
python -m agent ping
python -m agent prompt "Add input validation to calc.py and write tests" --input calc.py --out C:\work\calc
python -m agent run --out C:\work\sfs        & rem default Task-1 task with the bundled inputs
python -m agent gui
```

Options: `--mode read-only|workspace-write|danger-full-access`, `--target desktop|any`, `--no-verify`,
`--yes-deny` (unattended: refuse anything that needs approval), `--budget`, `--max-steps`, `--resume`.

## Cost and budget

Prices follow DeepSeek's official page (checked 2026-11-04) for `deepseek-v4-pro`, per 1M tokens: cache-hit
input $0.022, cache-miss input $0.66, output $1.98 off-peak, **double in peak hours** (Mon–Fri 01:00–04:00
and 06:00–10:00 UTC). Each call is priced at the moment it is made. The budget guard (default $0.90, at most
$1.00) starts a call only if the spend so far plus the most expensive call so far stays under the budget;
a peak-hour start asks first.

## Results

**Version 1 run (4 November 2026, before the generalisation):** completed in 10 calls, 8 minutes,
**$0.115**, 97.7% cache hit rate, 31 files, 11 passing tests, all gates passed; the generated Tkinter game
was played. Its output and logs are in [`sample_run/`](sample_run/).

**Version 2 run:** [['sample_run/'](sample_run/)](https://github.com/Akash-awan83/spacefractions-agent/tree/main/app/final_run_v3).

## Run from source and tests

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
python -m pytest -q          & rem 172 tests, no network, a fake model drives the agent
build_exe.bat                & rem builds dist\CodeAgent.exe
```

The tests include a **generality check**: the agent's source may not mention the Space Fraction System,
and a different architecture (a "Library Manager") yields a different contract.

## Limitations

* The verifier's code checks are for **Python** projects; for other languages only the documentation
  gates apply, and the model has to run the tests through `bash`.
* Bash analysis is static and simplified (about a dozen groups of checks); `danger-full-access` trusts the model.
* Compression has 3 levels; resume restarts from the files on disk, not from the exact turn.
* No streaming, sub-agents, MCP, skills or plan mode.
* The verifier cannot judge whether a program is pleasant to use, and tests are written by the same model.
* One run per version; model output varies between runs.
* Built with the help of an AI coding assistant; the design decisions, Windows testing and runs were done
  and checked by the author.

## Repository layout

```
agent/            context.py, loop.py, tools.py, permissions.py, verify.py, preprocess.py, llm.py,
                  pipeline.py, gui.py, runner.py, config.py, events.py, __main__.py
inputs/           the two MAAD artifacts for SFS (the Task-1 inputs)
sample_run/       output and logs of the final run
tests/            172 tests
code_agent_app.py .exe entry point
build_exe.bat     local Windows build;  .github/workflows builds and smoke-tests the exe on every push
```
