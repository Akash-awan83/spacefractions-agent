# AGENTS.md

Guidance for agents working in this repository.

- Standard library only. Do not add web frameworks, servers, or third-party deps.
- The desktop UI uses Tkinter and lives only in `spacefractions/ui`.
- Component packages (`game`, `question`, `user`, `admin`) must never import
  `spacefractions.ui` or `tkinter`.
- Use parameterized `?` placeholders in SQL, never f-strings/interpolation.
- Do not use `eval`, `exec`, `os.system`, `subprocess`, `pickle.loads`, or
  `__import__`.
- Every non-empty `.py` file starts with the generated-code header and a
  `# Traces:` line.
- Keep `DECISIONS.md` and `traceability_matrix.csv` in sync with code changes.
- Tests are plain `unittest` in `tests/`; never open windows or call `mainloop()`.
