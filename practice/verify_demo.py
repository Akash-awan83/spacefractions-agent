# Mashq: verifier ko ek chhote, jaan boojh kar kharab project par chalayein.
# Chalane ka tareeqa (project folder se):  python practice\verify_demo.py
from pathlib import Path
import json, shutil, sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # project folder, taake 'agent' mile
from agent.preprocess import build
from agent.verify import Verifier

root = Path("practice_project")
shutil.rmtree(root, ignore_errors=True)
(root / "spacefractions" / "question").mkdir(parents=True)
(root / "spacefractions" / "question" / "__init__.py").write_text(
    "def add(a, b):\n    return a + b\n", encoding="utf-8")
(root / "spacefractions" / "game.py").write_text(
    "from spacefractions.question import subtract\n"   # ghalti 1: subtract mojood hi nahi
    "import requests\n"                                  # ghalti 2: standard library nahi
    "x = eval('1 + 1')\n",                               # ghalti 3: khatarnak call
    encoding="utf-8")

arch = build(Path("inputs/Architecture_Documentation.md"), Path("inputs/Architecture_View.md"))
report = Verifier(root, arch)()          # tests nahi chalaye: run_tests=None
for issue in report.issues:
    if issue.file.endswith("game.py"):
        print(f"[{issue.category}] {issue.file}:{issue.line}  {issue.message}")
print("\nKul masle:", len(report.issues), "| pass?", report.passed)
print("Bug report:", json.loads((root / ".agent" / "bug_report.json").read_text())["by_category"])
