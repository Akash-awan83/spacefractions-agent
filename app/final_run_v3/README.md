# Space Fractions (desktop)

A standard-library-only, SQLite-backed, Tkinter desktop learning tool that helps
sixth-grade students practise fraction problems. It is generated from the Space
Fractions architecture documentation and resolves every listed design conflict
(see `DECISIONS.md`).

## Requirements

- Python 3.11+
- Tkinter (bundled with the standard Windows/macOS Python install)
- No third-party packages (`requirements.txt` is intentionally empty)

## Layout

```
spacefractions/
  game/       GameComponent: game logic, state machine, service (proto mirror)
  question/   QuestionComponent: question model, SQLite repository, service
  user/       UserComponent: authentication/authorization (ASR-2)
  admin/      AdminComponent: question-bank administration (FR-3)
  ui/         Tkinter screens (intro, menu, game, ending, help, admin)
main.py       application entry point
tests/        unittest suite (no windows, no mainloop)
```

## Run

```
python main.py
```

This opens the desktop window: intro, main menu, the question loop, and an
ending screen with feedback. Data is stored in `spacefractions.db` (SQLite).

## Test

```
python -m unittest discover -s tests -v
```

or via Docker:

```
docker build -t spacefractions .
docker run --rm spacefractions
```

## Requirement traceability

Every requirement (FR-1..FR-7 derived, NFR-1, ASR-1, ASR-2 inferred) is mapped
to code and tests in `traceability_matrix.csv`.
