# DECISIONS.md

One section per architecture design conflict (C01..C20). Each records the
decision, rationale, and considered alternatives.

## C01 — delivery target (web -> desktop)
- **Decision:** Build a Python 3.11 desktop app with Tkinter; no web UI, no server.
  Each documented component becomes an in-process module behind a service
  interface that mirrors the OpenAPI/proto contracts (`GameService.play` mirrors
  proto `Play`). SQLite replaces PostgreSQL. Dockerfile/k8s are optional artifacts.
- **Rationale:** The evaluation requires a desktop app; keeping the component
  boundaries preserves the documented architecture.
- **Alternatives:** Keep the microservice/cloud deployment (rejected: not desktop);
  collapse into a single file (rejected: loses component traceability).

## C02 — missing ASR-2
- **Decision:** Add `ASR-2 = Security (authentication and authorization)` as an
  inferred requirement, implemented by `UserService` (salted SHA-256 password
  hashing, registration, authentication) and the persisted `users` table.
- **Rationale:** ASR-2 is cited throughout the docs but never specified.
- **Alternatives:** Drop ASR-2 (rejected: docs cite it); full OAuth2 (rejected:
  overkill for desktop and non-standard-library).

## C03 — only 3 requirements traced
- **Decision:** Derive functional requirements from the use cases and sequence
  messages, all labelled derived: FR-2 View score, FR-3 Update questions,
  FR-4 View help, FR-5 Pause/resume, FR-6 Game over, FR-7 Submit answer.
- **Rationale:** Covers modelled behaviours not present in the original matrix.
- **Alternatives:** None reasonable.

## C04 — artifact name mismatch
- **Decision:** Generate both `sql/game_ddl.sql` and `sql/question_ddl.sql` with
  the consistent schema; traceability uses `sql/question_ddl.sql` for question
  persistence. Recorded in `traceability_matrix.csv`.
- **Rationale:** Satisfies both referenced names and removes ambiguity.
- **Alternatives:** Rename to one file (rejected: would break the traceability reference).

## C05 — extra Admin component
- **Decision:** Use the union of components: Game, Question, User, Admin. Each has
  a package and responsibilities; Admin manages Questions.
- **Rationale:** The diagrams add Admin, which also has a use case (Update Questions).
- **Alternatives:** Drop Admin (rejected: it owns FR-3).

## C06 — missing detailed designs for Question/User
- **Decision:** Derive responsibilities/interfaces/schema from the class and
  sequence diagrams: `QuestionService` (getPrompt, getOptions, checkAnswer,
  add/update) backed by `QuestionRepository`; `UserService` (register/authenticate).
- **Rationale:** Fills the documented gap.
- **Alternatives:** Leave undocumented (rejected).

## C07 — identifier type mismatch
- **Decision:** Use integer ids everywhere: Python `int`, SQLite `INTEGER`
  (SERIAL-equivalent), proto `int32`, OpenAPI `integer`.
- **Rationale:** One canonical type across layers.
- **Alternatives:** String ids (rejected: SQL used SERIAL and OpenAPI/proto used integer).

## C08 — behaviours without operations
- **Decision:** Add an operation for each modelled behaviour: `view_score`,
  `submit_answer`, `update_questions`, `view_help`, `pause`, `resume`,
  `game_over`, `update`.
- **Rationale:** The interface now covers all modelled behaviour.
- **Alternatives:** Ignore unmodeled behaviour (rejected).

## C09 — GET /play changes state
- **Decision:** State-changing operations are method calls in-process; any HTTP
  mapping uses POST (e.g. `POST /games/{id}/answer`). `GameService.play` remains
  the entry point. Recorded here.
- **Rationale:** Correct HTTP semantics; the desktop app has no HTTP layer.
- **Alternatives:** Keep GET (rejected: side-effectful GET).

## C10 — classes without tables
- **Decision:** Create tables `games`, `questions`, `users`, `admins` (ASR-1
  requires question data durability).
- **Rationale:** Every persisted domain class has a table.
- **Alternatives:** Persist only games (rejected).

## C11 — SLO error budget math
- **Decision:** Correct the error budget to 0.01% for a 99.99% uptime SLO; for the
  desktop app restate availability as "local features remain available" (no
  cloud SLA target).
- **Rationale:** Arithmetic consistency (1 - 0.9999 = 0.0001).
- **Alternatives:** Change SLO to 99% (rejected: keep documented SLO).

## C12 — composition arrow direction
- **Decision:** Reinterpret: Game aggregates Questions (1..*), User plays Games,
  Admin manages Questions.
- **Rationale:** Matches the sequence and use-case semantics.
- **Alternatives:** Literal PlantUML reading (rejected: contradicts other diagrams).

## C13 — object diagram wrong keyword
- **Decision:** Read the declarations as objects (`game1`, `question1`, `user1`,
  `admin1`); PlantUML expects `object`, not `participant`.
- **Rationale:** Correct UML notation.
- **Alternatives:** None.

## C14 — missing final state
- **Decision:** Add `GameOver -> done` (return to menu / exit) via
  `GameStateMachine.finish()`.
- **Rationale:** Completes the state machine.
- **Alternatives:** None.

## C15 — activity flow linear
- **Decision:** Loop over the question list until all questions are answered;
  `Game.submit_answer` advances the cursor and finishes the game.
- **Rationale:** Models the real game loop.
- **Alternatives:** None.

## C16 / C17 — collaboration diagrams repeat sequence diagrams
- **Decision:** Treat `CollaborationDiagram1` = `SequenceDiagram1` and
  `CollaborationDiagram2` = `SequenceDiagram2` as one scenario each (no new info).
- **Rationale:** They are message-for-message duplicates.
- **Alternatives:** Keep both as separate scenarios (rejected: redundancy).

## C18 — summary features not modelled
- **Decision:** Implement intro (movie placeholder), main menu, ending, and
  feedback as Tkinter screens in `spacefractions/ui/app.py`, traced to the summary.
- **Rationale:** Covers the full summary scope.
- **Alternatives:** Omit them (rejected).

## C19 — empty test matrix
- **Decision:** Fill the matrix with concrete unittest files: `tests/test_game.py`,
  `test_game_service.py`, `test_question.py`, `test_user.py`, `test_admin.py`,
  `test_persistence.py`, `test_help.py`.
- **Rationale:** Concrete verification plan.
- **Alternatives:** None.

## C20 — k8s not production-ready
- **Decision:** Kubernetes is optional for the desktop target. The Dockerfile pins
  `python:3.11-slim` (no `:latest` app image) and runs the test suite; no probes
  or resource limits are needed for tests.
- **Rationale:** Desktop deployment; Kubernetes is out of scope.
- **Alternatives:** Full k8s manifests per service (rejected: not desktop).
