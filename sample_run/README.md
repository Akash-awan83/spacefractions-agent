# Space Fractions

Space Fractions is a desktop educational game that helps sixth-grade students practise
fractions. It covers addition, subtraction, multiplication, comparison and simplification,
using exact fraction arithmetic (`fractions.Fraction`) and multiple-choice questions.

The project is built with Python 3.11 and Tkinter, and uses **only the standard library**.

## Features

- Skippable animated intro (stands in for the introductory movie)
- Main menu: Play, Help, High scores, Admin, Exit
- Play: a series of multiple-choice fraction questions with immediate feedback and score
- Pause/resume during play (state machine: Playing, Paused, GameOver)
- Ending scene with final score, feedback and replay
- Help screen
- Admin screen to add, edit and delete questions
- SQLite persistence for questions and high scores

## How to run

Requires Python 3.11 (Tkinter ships with the standard library on most platforms).

```bash
python main.py
```

On first run the SQLite database file `spacefractions.db` is created next to `main.py`
and seeded with built-in questions.

## How to test

```bash
python -m unittest discover -s tests
```

Tests are headless: they never create a `tk.Tk` window and never call `mainloop()`.
They exercise the domain logic and the SQLite repositories using a temporary database.

## Project structure

```
main.py                         desktop entry point
spacefractions/
  game/                         game state machine (Game, GameState, AnswerResult)
  question/                     Question model, QuestionType and question factory
  user/                         local player identity (User, UserService)
  admin/                        question management (Admin, AdminService)
  storage/                      SQLite repositories (Database, Question/Score repositories)
  ui/                           Tkinter screens (App plus screen classes)
tests/                          unittest suite
traceability_matrix.csv         requirement -> code/tests mapping
DECISIONS.md                    resolution of every MAAD design conflict
AGENTS.md                       rules for AI coding tools
```

## How it traces to MAAD

The MAAD architecture described a web/cloud microservice system. The evaluation contract
requires a desktop app, so each documented component (Game, Question, User, Admin) was
implemented as an in-process Python package behind a public interface, with embedded SQLite
replacing PostgreSQL. The Dockerfile is retained only to run the headless unit tests.

`traceability_matrix.csv` maps every requirement id (including derived ones such as FR-2
through FR-13 and the inferred ASR-2) to the code and test files that implement it.
`DECISIONS.md` records how each deterministic design-check conflict (C01-C20) was resolved.
