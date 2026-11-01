"""Entry point of SpaceFractionsAgent.exe.

The same .exe plays three roles:
  SpaceFractionsAgent.exe                          -> opens the GUI
  SpaceFractionsAgent.exe --run-tests <args>       -> runs generated unit tests (used by the agent)
  SpaceFractionsAgent.exe --run-script <main.py>   -> runs the generated game
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
