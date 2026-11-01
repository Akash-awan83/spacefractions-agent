"""Tests for agent.preprocess. Run with:  python -m pytest -q"""
from pathlib import Path

import pytest

from agent import preprocess as pp

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "inputs" / "Architecture_Documentation.md"
VIEWS = ROOT / "inputs" / "Architecture_View.md"


@pytest.fixture(scope="module")
def data():
    return pp.build(DOC, VIEWS, target="desktop")


# ---- unit-level parsers -------------------------------------------------------
def test_markdown_table_keeps_empty_cells():
    secs = pp.parse_markdown("# T\n| A | B |\n| --- | --- |\n| x | |\n")
    t = secs[1]["tables"][0]
    assert t["rows"] == [{"A": "x", "B": ""}]


def test_code_fence_hides_headings():
    secs = pp.parse_markdown("# T\n```yml\n# not a heading\n```\n")
    assert [s["title"] for s in secs] == ["(preamble)", "T"]
    assert secs[1]["code_blocks"][0]["code"] == "# not a heading"


def test_member_parsing():
    m = pp._parse_member("+ playGame(game: Game): void")
    assert m == {"visibility": "public", "name": "playGame", "type": "void", "kind": "method",
                 "params": [{"name": "game", "type": "Game"}]}
    assert pp._parse_member("- options: List<string>")["type"] == "List<string>"


@pytest.mark.parametrize("line,frm,to,label", [
    ("User->>Game: play()", "User", "Game", "play()"),
    ("[*] --> Playing", "[*]", "Playing", ""),
    ("Playing --> Paused : pause()", "Playing", "Paused", "pause()"),
    ("EndUser -- (PlayGame)", "EndUser", "PlayGame", ""),
    ("Game --* Question", "Game", "Question", ""),
])
def test_edges(line, frm, to, label):
    e = pp.parse_plantuml_body([line])["edges"][0]
    assert (e["from"], e["to"], e["label"]) == (frm, to, label)


def test_composition_whole_follows_diamond():
    shaped, _ = pp._shape("class", pp.parse_plantuml_body(["class A", "class B", "A *-- B", "C --* D"]))
    rels = shaped["relations"]
    assert (rels[0]["whole"], rels[0]["part"]) == ("A", "B")
    assert (rels[1]["whole"], rels[1]["part"]) == ("D", "C")


def test_sql_parser():
    t = pp.parse_sql("CREATE TABLE t (id SERIAL PRIMARY KEY, x NUMERIC(5,2) NOT NULL);")[0]
    assert [c["name"] for c in t["columns"]] == ["id", "x"]
    assert t["columns"][1]["type"] == "NUMERIC(5,2)"


# ---- whole-document behaviour ------------------------------------------------------
def test_all_diagrams_found_and_fully_parsed(data):
    kinds = [d["kind"] for d in data["views"]["diagrams"]]
    assert len(kinds) == 13
    assert set(kinds) == set(pp.KNOWN_KINDS)
    for d in data["views"]["diagrams"]:
        assert not [i for i in d["issues"] if i["type"] == "unparsed_lines"], d["id"]


def test_document_structure(data):
    assert data["project"]["name"] == "Space Fractions"
    assert [r["id"] for r in data["requirements"]] == ["FR-1", "NFR-1", "ASR-1"]
    assert len(data["component_designs"]["GameComponent"]["technology_options"]) == 12
    assert data["interfaces"]["openapi"]["endpoints"][0]["path"] == "/play"
    assert data["data_model"]["tables"][0]["name"] == "games"
    assert data["test_matrix"]["is_empty"] is True
    assert {a["id"] for a in data["assumptions"]} == {"A1", "A2"}


def test_duplicate_code_blocks_are_merged(data):
    # Section L repeats the OpenAPI/proto/SQL/k8s blocks from D and E.
    assert len(data["deployment"]["kubernetes"]) == 1
    assert len(data["data_model"]["tables"]) == 1


@pytest.mark.parametrize("category,needle", [
    ("delivery_target", "desktop"),
    ("traceability", "ASR-2"),
    ("artifacts", "question_ddl.sql"),
    ("components", "Admin"),
    ("data_types", "Identifier"),
    ("interfaces", "behaviours"),
    ("data_model", "Question"),
    ("slo", "0.01%"),
    ("uml_wrong_keyword", "participant"),
    ("uml_no_final_state", "StateDiagram"),
    ("scope", "movie"),
    ("testing", "empty"),
])
def test_known_conflicts_detected(data, category, needle):
    hits = [c for c in data["conflicts"] if c["category"] == category and needle in c["finding"]]
    assert hits, f"expected a {category} conflict mentioning {needle!r}"


def test_web_target_has_no_delivery_conflict():
    d = pp.build(DOC, VIEWS, target="web")
    assert not [c for c in d["conflicts"] if c["category"] == "delivery_target"]


def test_compact_json_is_small(data):
    # Prompt-prefix budget: stay well under 10k tokens (~4 chars/token).
    assert len(pp.compact_json(data)) / 4 < 10_000


def test_cli_writes_file(tmp_path):
    out = tmp_path / "a.json"
    assert pp.main(["--doc", str(DOC), "--views", str(VIEWS), "--out", str(out)]) == 0
    assert out.stat().st_size > 1000
    assert pp.main(["--doc", "missing.md", "--views", str(VIEWS), "--out", str(out)]) == 2


def test_conflicts_carry_maad_evaluator_fields(data):
    for c in data["conflicts"]:
        assert c["root_cause"] and c["refinement"]
        assert c["responsible_role"] in {"Analyst", "Modeler", "Designer", "external"}
