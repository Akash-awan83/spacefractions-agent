"""Entry point of CodeAgent.exe (Simple Code Agent).

The same .exe plays three roles:
  CodeAgent.exe                          -> opens the GUI
  CodeAgent.exe --run-tests <args>       -> runs generated unit tests (used by the agent)
  CodeAgent.exe --run-script <main.py>   -> runs the generated program
so the target machine needs no Python installation at all.
"""
import sys


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--run-tests"]:
        from agent.runner import main as runner_main
        return runner_main(argv[1:])
    if argv[:1] == ["--run-script"] and len(argv) > 1:
        from agent.runner import run_script
        return run_script(argv[1])
    from agent.gui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
