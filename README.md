# SpaceFractions Code Agent

A simplified, Claude-Code-style coding agent that turns **architecture documentation** into a
**working, tested desktop application**. The underlying model is **DeepSeek `deepseek-v4-pro`**
through the official API. The agent ships as a **double-click Windows `.exe`** with a small GUI.

The inputs are the architecture artifacts that **MAAD** (Li, Zhang, Zhou, Liang et al.,
*MAAD: Automate Software Architecture Design through Knowledge-Driven Multi-Agent Collaboration*,
arXiv:2507.21382) produces for its running example, the **Space Fraction System (SFS)**:
an architecture document and eleven PlantUML diagrams in the 4+1 view model.
This agent is the stage after MAAD: **architecture → verified code**.

```
Architecture_Documentation.md ─┐                      ┌─ spacefractions/ (game, question, user, admin, storage, ui)
                               ├─ 1 Preprocess ─ JSON ─┤  main.py  tests/  README.md  DECISIONS.md
Architecture_View.md (UML) ────┘   (no LLM)    + 20    │  traceability_matrix.csv  requirements.txt
                                               conflicts  .agent/ transcript, cost, bug_report.json
                                     │
                     2 Agent loop (DeepSeek, sandboxed tools) ── 3 Verifier (final gate) ──┘
```

## How it works

| Stage | Module | What it does |
|---|---|---|
| 1. Preprocess | `agent/preprocess.py` | Parses the Markdown (tables, OpenAPI, proto, SQL, Kubernetes) and all PlantUML diagrams into one JSON. A deterministic **cross-view consistency checker** reports 20 design conflicts with evidence, root cause, the responsible MAAD role (Analyst / Modeler / Designer) and a suggested refinement, mirroring the outputs of MAAD's Evaluator agent. No tokens are spent. |
| 2. Agent loop | `agent/loop.py`, `agent/llm.py` | The classic coding-agent loop: model → tool calls → results → repeat. Handles DeepSeek's thinking-mode contract (`reasoning_content` must be sent back during tool calls), retries, a hard **budget guard**, a step limit, cancel, context compaction and a full transcript. |
| Tools | `agent/tools.py` | `list_dir`, `read_file`, `write_file`, `edit_file`, `search`, `check_syntax`, `run_tests`, `todo_write`, `verify`. All paths are **sandboxed** to the output folder; there is no general shell. Tests run in an **isolated process** with a timeout. |
| 3. Verifier | `agent/verify.py` | Quality gates the model cannot skip: when it tries to finish, failures are sent back (max 3 rounds). Every issue is labelled with the bug taxonomy of *A Survey of Bugs in AI-Generated Code* (Gao, Tahir, Liang, Susnjak, Khomh, arXiv:2512.05239) and logged in `.agent/bug_report.json`, including whether the agent fixed it. |
| GUI | `agent/gui.py` | API-key configuration, input/output selection, run/stop, live plan, activity log, **live cost meter**, and a button that plays the generated game. |

### What was taken from the reference implementations

The task names three references: Anthropic's **claude-code**, **how-claude-code-works**
(Windy3f3f3f3f; a chapter-by-chapter analysis of Claude Code's architecture, with the companion
*claude-code-from-scratch* re-implementation) and **claw-code** (ultraworkers; a Rust re-implementation
of the claw agent harness that also supports OpenAI-compatible providers). The table maps each mechanism
described there to this agent, and states what was simplified.

| Mechanism (as described in the references) | This agent | Simplification and why |
|---|---|---|
| Core query loop: call the model → execute tool calls → feed results back → repeat until no tool call | `loop.py` (`Agent.run`) | Same loop, one model call at a time |
| Tool system (read, edit, write, search, shell, todo, …) | `tools.py`: 8 tools + `verify` | No general shell: the only executable action is running the generated unit tests in an isolated process |
| Permission system (rule layer, shell-command analysis, user confirmation) | Path sandbox (no `..`, absolute paths or reserved folders), no shell, security gate in the verifier | No interactive confirmation: the run is unattended, so the policy is "deny by construction" |
| Context engineering (system prompt, project instructions such as CLAUDE.md) | One stable system prompt: instructions + architecture JSON + the verifier contract | Kept byte-identical for DeepSeek's prefix cache; the generated project gets its own `AGENTS.md` |
| Context compaction pipeline | `Agent._compact`: elides old tool outputs above a token threshold | One layer instead of several; runs are short |
| Hooks that run when the agent tries to stop | Final verification gate: blocking issues are sent back (max 3 rounds) | Fixed, built-in gate instead of user-configurable hooks |
| Todo list / plan mode | `todo_write` tool and a PLAN phase; the GUI shows the live plan | Planning is a phase inside the same loop, not a separate read-only mode |
| Sub-agents / multi-agent | Not used; the phases PLAN → INTERFACES → IMPLEMENT → TEST → DOCUMENT → VERIFY run in one loop | One context is cheaper than several (the $1 budget) |
| Streaming output and early tool execution | Not used; the GUI reports each step, its tokens and its cost | Simpler, and every step is logged |
| Session persistence | `.agent/transcript.jsonl`, `run_summary.json`, `bug_report.json` | Logs for review, no resume |
| Provider abstraction and a health check (claw-code: OpenAI-compatible providers, `claw doctor`) | `make_client` with DeepSeek (default) plus free testing providers; `python -m agent ping` / **Test key** | Only OpenAI-compatible Chat Completions |
| MCP servers, memory, skills | Not implemented | Outside the scope of a single generation task |

### Quality gates (the verifier)

| Gate | Taxonomy category |
|---|---|
| Every file parses | Syntax |
| Every import is stdlib or exists in the project; every `from x import y` name really exists in `x` | Hallucination |
| Unit tests pass (isolated process); failures classified from their traceback | Functional / Hallucination / System / Reliability |
| Component packages from the component diagram exist; domain packages never import the UI layer | Design conformance |
| Desktop only: the UI package uses Tkinter, no web framework or server is imported, `main.py` starts the package | Design conformance |
| No `eval`/`exec`/`subprocess`/`os.system`; SQL never built from strings | Security |
| AI-generated-code header in every file | Code style & standards |
| `DECISIONS.md` addresses every conflict id; `traceability_matrix.csv` covers every requirement and points only to existing files; required deliverables exist (README, requirements.txt, Dockerfile, AGENTS.md, tests) | Traceability & documentation |

### How the design draws on the group's research

| Paper | Used for |
|---|---|
| MAAD (arXiv:2503.20536, 2507.21382) and *Bridging Requirements and Architecture* | Input format (SFS, 4+1 views); conflict reports in the style of the MAAD Evaluator |
| *A Survey of Bugs in AI-Generated Code* | Verifier taxonomy; hybrid mitigation (LLM + program analysis + feedback loop) |
| *CodeTeam* (repository-level generation) | Interface-first workflow; cross-file name checks |
| *Beyond Functional Correctness: Design Issues in AI IDE-Generated Projects* | Design-conformance gate (component packages, layering) |
| *Using LLMs in Generating Design Rationale* | `DECISIONS.md` with decision, rationale and alternatives per conflict |
| Traceability-link papers (HGNNLink, NL-PL TLR) | `traceability_matrix.csv` requirement → code → test, verified |
| *On Developers' Self-Declaration of AI-Generated Code* | AI-generated header in every generated file |
| *Rule Taxonomy and Evolution in AI IDEs* | Generated `AGENTS.md` with the project's rules for future AI tools |

## Cost

DeepSeek bills cached prompt prefixes at a small fraction of the normal input price. The agent keeps
its system prompt (instructions + architecture JSON, about 8k tokens) **byte-identical on every call**
so it is served from the cache. The budget guard stops the run at **$0.90** by default (configurable,
capped at $1.00). Each run writes the exact token counts and cost to `.agent/run_summary.json`.
Prices are editable in the config file (see below).

## Free testing providers (development only)

The task requires DeepSeek, and the demo, the cost evidence and the submitted output come from DeepSeek.
To test the agent without spending money during development, the provider can be switched to
**Google Gemini (free tier)** or **OpenRouter (free models)** in the GUI or with `--provider`:

```bat
python -m agent set-key --provider gemini
python -m agent ping --provider gemini
python -m agent run --provider gemini --out C:\path\to\test_folder
```

DeepSeek-only parameters are not sent to these providers. Such runs are labelled in the GUI, the CLI and
`.agent/run_summary.json` (`meta.provider`, `meta.note`) so they cannot be mistaken for the DeepSeek run.

## Use the .exe (Windows, no Python needed)

1. Download `SpaceFractionsAgent.exe` (Releases, or the latest *Actions* run artifact).
2. Double-click it. If Windows SmartScreen appears: *More info → Run anyway* (the file is unsigned).
3. Paste your DeepSeek API key → **Save** → **Test key**.
4. The sample inputs are pre-selected; choose an empty **Output folder**.
5. **Check inputs** (shows the 20 conflicts) → **Run agent**.
6. When the status shows *Completed*, click **Play generated game**.

The same `.exe` also runs the generated tests and the generated game, so no Python installation is required.

## Run from source

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
python -m pytest -q                       & rem 101 tests, no API key needed (a fake model is used)
python -m agent gui                       & rem the desktop window
python -m agent set-key                   & rem or use the CLI
python -m agent ping
python -m agent run --out C:\path\to\empty\folder
python -m agent run --out C:\same\folder --resume   & rem continue an interrupted run
```

If a run stops early (quota, network, the Stop button), running again on the same folder **resumes** it:
the first message lists the files already written and the model continues from there.

Build the `.exe`: double-click `build_exe.bat` (result: `dist\SpaceFractionsAgent.exe`).
GitHub Actions (`.github/workflows/build-windows-exe.yml`) builds and smoke-tests it on every push.

## Configuration and safety

* Settings, including the API keys, are stored in `%APPDATA%\SpaceFractionsAgent\config.json`, outside the
  repository. `DEEPSEEK_API_KEY` (or `GEMINI_API_KEY`, `OPENROUTER_API_KEY`) overrides the stored key.
* The model can only touch files inside the chosen output folder. It cannot run shell commands; the only
  code it can execute is the generated unit tests, in a separate process with a timeout.

## Repository layout

```
agent/            the code agent (preprocess, llm, loop, tools, verify, runner, gui, prompts, config)
inputs/           the two MAAD artifacts for SFS
tests/            101 tests (no network): parsers, client, providers, tools, loop, verifier, GUI
sf_agent_app.py   .exe entry point (GUI, --run-tests, --run-script)
build_exe.bat     local Windows build
```

## Limitations

* The verifier checks structure, imports, security patterns, documentation and tests; it cannot prove the
  game is pleasant to play. A human still reviews the UI.
* Conflict detection targets the artifact format MAAD produced for SFS; other formats may need new rules.
* Model output varies between runs; the transcript and bug report make every run reviewable.
