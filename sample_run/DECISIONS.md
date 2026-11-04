# Design Conflict Decisions

This file records how every deterministic design-check conflict from the MAAD architecture
was resolved in the desktop implementation.

## C01 — delivery_target: web/cloud vs desktop

Decision: build a desktop Tkinter app. Each documented component (Game, Question, User,
Admin) becomes an in-process module behind a Python interface that mirrors the OpenAPI/proto
contracts; embedded SQLite replaces PostgreSQL; the Dockerfile only runs headless unit tests.

Rationale: the evaluation contract requires a desktop application.

Alternatives: a web/Express app (rejected, violates the delivery target); a local hybrid web
server (rejected, heavier and violates stdlib-only).

## C02 — traceability: ASR-2 missing

Decision: add ASR-2 as an inferred requirement — "Security: no eval/exec, parameterised SQL
only, validate inputs" — and trace it to storage, admin and main.

Rationale: ASR-2 is cited throughout the architecture (OAuth2, TLS, threat mitigations). For
a single-user desktop app it is realised as safe coding practices rather than network security.

Alternatives: drop ASR-2 (rejected because the architecture repeatedly cites it); implement
full OAuth2/TLS (rejected, out of scope).

## C03 — traceability: only 3 requirements traced

Decision: derive FR-2..FR-13 from the use cases, state events, sequence messages and summary,
and mark them as derived in `traceability_matrix.csv`.

Rationale: the views describe more behaviour than the three original requirement rows.

Alternatives: keep only FR-1/NFR-1/ASR-1 (rejected, leaves features untraced).

## C04 — artifacts: sql/question_ddl.sql vs sql/game_ddl.sql

Decision: do not ship separate SQL files. The single schema lives in
`spacefractions/storage/database.py` (tables `questions` and `scores`).

Rationale: a desktop app with embedded SQLite needs one executable schema, not two
deliverable SQL files.

Alternatives: generate both SQL filenames (rejected, duplicates the schema).

## C05 — components: extra Admin component

Decision: use the union of components — Game, Question, User and Admin — each with an owner
module (`spacefractions/game`, `question`, `user`, `admin`).

Rationale: the use-case diagram includes an Admin actor and the Update Questions use case.

Alternatives: drop Admin (rejected, loses the update-questions feature).

## C06 — components without detailed design

Decision: derive QuestionComponent and UserComponent designs from the class/sequence diagrams:
a question domain model plus repository for QuestionComponent; local player identity for
UserComponent.

Rationale: their behaviour is visible in the views even though the detail section only covers
GameComponent.

Alternatives: leave them unspecified (rejected, they are required by the contract).

## C07 — identifier types disagree

Decision: use integer auto-increment IDs (SQLite `INTEGER PRIMARY KEY`) across all layers; the
class-diagram string IDs are treated as display-level labels only.

Rationale: a single integer key is the natural SQLite primary key and matches the
OpenAPI/proto int32/int types.

Alternatives: string UUIDs (rejected, overkill for a local app).

## C08 — behaviours missing from API contracts

Decision: implement every modelled behaviour as an in-process method: play/start_game,
submit_answer, pause, resume, finish, view_score/list_high_scores, add/update/delete/list
questions, and view_help.

Rationale: with the desktop target the "API" is the set of public Python interfaces, each
mirroring the OpenAPI/proto operation names where practical.

Alternatives: expose a local REST API (rejected, violates stdlib-only/desktop).

## C09 — GET /play mutates state

Decision: replace state-changing REST operations with explicit mutating methods
(submit_answer, pause, resume); no HTTP layer exists.

Rationale: state-changing operations must not be modelled as GET.

Alternatives: keep GET /play (rejected, semantically wrong).

## C10 — classes without SQL tables

Decision: persist questions (`questions` table) and scores (`scores` table). User and Admin
are in-memory roles in a single-user desktop app, so they need no tables.

Rationale: ASR-1 requires question data durability; scores support the high-score screen.
There is no multi-user authentication to persist.

Alternatives: persist users/admins too (rejected, no auth requirement remains after the
desktop scoping of C02).

## C11 — SLO error budget inconsistency

Decision: drop the cloud SLO. For the desktop app the relevant targets are local
responsiveness (NFR-1) and local SQLite durability (ASR-1). The corrected arithmetic
(99.99% => 0.01% error budget) is noted here as documentation only.

Rationale: a local desktop app has no uptime SLO.

Alternatives: keep 99.99%/1% (rejected, numerically inconsistent); adopt 99.99%/0.01% as an
operational target (rejected, meaningless locally).

## C12 — composition arrow semantics

Decision: reinterpret as Game aggregates Questions, Users play Games, and Admins manage
Questions. Implemented with plain references/lists, not ownership deletion.

Rationale: the PlantUML diamond placement contradicted the rest of the views; the textual
intent is clear.

Alternatives: keep literal PlantUML semantics (rejected, wrong ownership direction).

## C13 — object diagram 'participant' keyword

Decision: read the four declarations as objects (game1, question1, user1, admin1); no code
impact beyond naming.

Rationale: the keyword is sequence-diagram syntax used in an object diagram.

Alternatives: regenerate the diagram (rejected, cosmetic).

## C14 — missing final state transition

Decision: add GameOver -> [*] by making the ending screen return to the menu or replay (exit
via the menu).

Rationale: every state machine needs an exit path.

Alternatives: leave GameOver terminal (rejected, the user could be stuck).

## C15 — activity diagram has no loop

Decision: loop over the question list in `Game` until all questions are answered; `PlayScreen`
advances one question at a time.

Rationale: the game is inherently a loop over questions.

Alternatives: linear single-question flow (rejected, contradicts the product).

## C16 — CollaborationDiagram1 duplicates SequenceDiagram1

Decision: treat both as the single "Play Game" scenario; implement once in `Game` +
`PlayScreen`.

Rationale: they are the same interaction shown in two notations.

Alternatives: implement twice (rejected).

## C17 — CollaborationDiagram2 duplicates SequenceDiagram2

Decision: treat both as the single "Update Questions" scenario; implement once in
`AdminService` + `AdminScreen`.

Rationale: same interaction, two notations.

Alternatives: implement twice (rejected).

## C18 — summary features not modelled

Decision: implement the intro animation (FR-7), main menu (FR-8), ending scene (FR-6) and
immediate feedback (FR-9) as Tkinter screens, traced back to the summary.

Rationale: the product summary is authoritative for user-visible features.

Alternatives: ignore them (rejected, they are in the product contract).

## C19 — empty test matrix

Decision: fill the matrix with concrete unittest files: `tests/test_question.py`,
`test_game.py`, `test_storage.py` and `test_admin.py`.

Rationale: the generator must ship passing tests; they are the unit-testing column of the
matrix.

Alternatives: leave the matrix empty (rejected).

## C20 — Kubernetes object not production-ready

Decision: do not ship Kubernetes manifests. The Dockerfile pins `python:3.11-slim` and runs
headless unit tests; the desktop game itself runs natively.

Rationale: the desktop target makes Kubernetes optional; a pinned, test-only container is the
only meaningful artefact.

Alternatives: ship a hardened k8s manifest (rejected, no cloud deployment exists).
