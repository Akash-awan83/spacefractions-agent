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

## Demo and results

* **Video:** [YouTube demo](VIDEO_LINK_HERE)
* **Windows app:** `SpaceFractionsAgent.exe` under [Releases](../../releases) (no Python needed)
* **Generated project from the final run:** [`sample_run/`](sample_run/), including the agent's logs in
  [`sample_run/.agent/`](sample_run/.agent/) (`run_summary.json`, `bug_report.json`, `transcript.jsonl`)

Final run on DeepSeek `deepseek-v4-pro` (official API), 4 November 2026, off-peak, started from the `.exe`:

| Measure | Result |
| --- | --- |
| Status | completed, all verification gates passed |
| Token cost | **$0.115** (limit: $1.00; budget guard set to $0.95) |
| Cache hit rate | 97.7% of input tokens |
| Model calls | 10 |
| Wall-clock time | 486 s (about 8 minutes) |
| Files generated | 31 |
| Generated unit tests | 11, all passing |
| Final-gate rounds needed | 0 (the model ran `verify` itself before finishing; both verification rounds found no issues) |
| Manual check | the generated Tkinter game was launched and played: intro, menu, questions with feedback, score |

In this run the verifier found no issues in either round, so it caught no bugs; stating the contract in the
prompt appears to have prevented the usual ones. The gates themselves are exercised by the test suite, which
injects each kind of defect and checks that it is caught.

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

Prices follow the official DeepSeek page (checked 2026-11-04): for `deepseek-v4-pro`, per 1M tokens,
cache-hit input $0.022, cache-miss input $0.66 and output $1.98 off-peak; **peak hours cost double**
(Mon-Fri 01:00-04:00 and 06:00-10:00 UTC; weekends are off-peak). Every call is priced at the moment it
is made, and older saved configs are upgraded to these prices automatically.

* The system prompt (instructions + architecture JSON, about 8k tokens) is **byte-identical on every call**,
  so it is billed at the cache-hit rate.
* The **budget guard** (default $0.90, capped at $1.00) starts a new call only if the spend so far plus the
  most expensive call so far stays under the budget, so a run cannot cross it.
* Starting a DeepSeek run in peak hours asks for confirmation first.
* Each run writes its tokens, cache hit rate, peak calls and cost to `.agent/run_summary.json`.

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
python -m pytest -q                       & rem 113 tests, no API key needed (a fake model is used)
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
sample_run/       the project the agent generated in the final DeepSeek run, with its .agent/ logs
practice/         a small script that shows the verifier catching planted defects
tests/            113 tests (no network): parsers, client, providers, tools, loop, verifier, GUI
sf_agent_app.py   .exe entry point (GUI, --run-tests, --run-script)
build_exe.bat     local Windows build
```

## Limitations

Stated plainly, so they can be weighed against the results above.

* **Generality.** The engine (loop, tools, sandbox, LLM client, budget guard, verifier mechanics, GUI, exe)
  is generic. The verification *contract* is configured for SFS: the package names (`spacefractions/game`,
  `question`, `user`, `admin`, `storage`, `ui`) and the screens described in the prompt are fixed in code.
  Another architecture document would be parsed, but the gates would still expect the SFS packages.
  Next step: derive the packages from the component diagram and the screens from the use cases.
* **What the verifier cannot judge.** It checks what can be checked deterministically: syntax, imports,
  layering, desktop-only UI, security patterns, headers, documentation, traceability and tests. It cannot
  judge whether the game is pleasant to play or whether the layout fits the window; a person has to play it.
* **Tests written by the same model.** A misunderstood requirement can produce a wrong test that passes.
  "At least 8 tests" is a count, not coverage.
* **Static checks have blind spots.** A call to a method that does not exist on an object is caught only if a
  test executes it; dynamic and star imports are not checked; the SQL check catches queries built directly
  in `execute(...)` with f-strings, `format` or `+`, not queries assembled earlier in a variable.
* **Bug categories are heuristic.** Test failures are classified by patterns in the traceback.
* **Conflict rules fit MAAD's SFS artifacts.** Documents in another format may need new rules.
* **One run.** The results above come from a single run; model output varies, and no statistics over
  repeated runs were collected.
* **Simplifications versus Claude Code.** No shell tool, single-layer compaction, no streaming, no
  sub-agents, no MCP (see the comparison table above for the reasons).
* **Prices.** Costs use DeepSeek's published prices as checked on 4 November 2026, including peak hours; the
  price table in the config file must be updated if DeepSeek changes them.
* **Development note.** The project was built with the help of an AI coding assistant; the design decisions,
  testing on Windows and the runs were done and checked by the author.