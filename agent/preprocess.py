#!/usr/bin/env python3
"""
Step 1 of the code agent: deterministic preprocessing (no LLM, no tokens).

Turns the two architecture inputs into ONE structured JSON file:

    Architecture_Documentation.md -> prose, tables, code blocks (OpenAPI, proto, SQL, k8s)
    Architecture_View.md          -> PlantUML diagrams (4+1 views)

Why do this before the LLM sees anything?
  1. Cost:    a compact, stable JSON becomes the fixed prefix of every prompt,
              so DeepSeek's context cache bills it at the cheap cached-input rate.
  2. Quality: contradictions spread across two files are easy for code to find
              and easy for an LLM to miss. We hand the agent an explicit list.
  3. Repeatable: same input -> same JSON, every time.

Background:
  The inputs follow the MAAD framework (Zhang, Li, Liang et al., arXiv:2503.20536):
  Analyst -> requirements/ASRs, Modeler -> style and stack, Designer -> UML views.
  This agent is the stage after MAAD: architecture -> working code. The conflict
  detector plays the role of the MAAD Evaluator (mismatch, root cause, refinement).

Usage:
    python -m agent.preprocess --doc inputs/Architecture_Documentation.md \
        --views inputs/Architecture_View.md --out build/architecture.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

try:
    import yaml  # PyYAML, used for the OpenAPI and Kubernetes snippets
except ImportError:  # the parser still works, just with less detail
    yaml = None

SCHEMA_VERSION = "1.0"
REQ_ID_RE = re.compile(r"\b(?:FR|NFR|ASR)-\d+\b")

# ---------------------------------------------------------------------------
# 1. Generic Markdown parsing
# ---------------------------------------------------------------------------
FENCE_RE = re.compile(r"^\s*```\s*([\w+-]*)\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
BULLET_RE = re.compile(r"^\s*(?:[*\-+]|\d+[.)])\s+(.*)$")
KV_RE = re.compile(r"^([^:]{1,40}):\s+(.+)$")
SEPARATOR_CELL_RE = re.compile(r":?-{3,}:?")


def _new_section(title: str, level: int, path: list[str]) -> dict:
    return {"title": title, "level": level, "path": path, "paragraphs": [],
            "bullets": [], "tables": [], "code_blocks": []}


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _parse_table(lines: list[str]) -> dict | None:
    rows = [_split_row(l) for l in lines]
    if len(rows) < 2:
        return None
    header = rows[0]
    body = [r for r in rows[1:] if not all(SEPARATOR_CELL_RE.fullmatch(c) for c in r)]
    out_rows = []
    for r in body:
        r = r + [""] * (len(header) - len(r))
        out_rows.append(dict(zip(header, r)))
    return {"columns": header, "rows": out_rows}


def parse_markdown(text: str) -> list[dict]:
    """Split Markdown into a flat list of sections. Each section remembers its
    heading path, so 'D. Detailed Technical Design > GameComponent > ...'
    can be reconstructed later."""
    sections = [_new_section("(preamble)", 0, [])]
    path: list[str] = []
    in_code, lang, code_buf, table_buf = False, "", [], []

    def flush_table():
        if table_buf:
            t = _parse_table(table_buf)
            if t:
                sections[-1]["tables"].append(t)
            table_buf.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        fence = FENCE_RE.match(line)
        if fence:
            if not in_code:
                flush_table()
                in_code, lang, code_buf = True, fence.group(1).lower(), []
            else:
                in_code = False
                sections[-1]["code_blocks"].append(
                    {"lang": lang, "code": "\n".join(code_buf).strip("\n")})
            continue
        if in_code:
            code_buf.append(raw)
            continue
        if line.lstrip().startswith("|"):
            table_buf.append(line)
            continue
        flush_table()
        h = HEADING_RE.match(line)
        if h:
            level, title = len(h.group(1)), h.group(2)
            path = path[: level - 1] + [title]
            sections.append(_new_section(title, level, list(path)))
            continue
        if not line.strip():
            continue
        b = BULLET_RE.match(line)
        if b:
            sections[-1]["bullets"].append(b.group(1).strip())
        else:
            sections[-1]["paragraphs"].append(line.strip())
    flush_table()
    if in_code:  # unterminated fence: keep what we have
        sections[-1]["code_blocks"].append({"lang": lang, "code": "\n".join(code_buf)})
    return sections


def _clean_title(t: str) -> str:
    return re.sub(r"^[A-Z]\.\s+", "", t).strip()


def _sections(sections, pattern):
    return [s for s in sections if re.search(pattern, s["title"], re.I)]


def _find_table(sections, *required, predicate=None):
    for s in sections:
        for t in s["tables"]:
            cols = [c.lower() for c in t["columns"]]
            if all(any(r.lower() in c for c in cols) for r in required):
                if predicate is None or predicate(t):
                    return t
    return None


def _col(row: dict, *keys: str) -> str:
    for k, v in row.items():
        if any(key.lower() in k.lower() for key in keys):
            return v
    return ""


def _split_list(v: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,;]", v) if x.strip()]


def _kv(lines: list[str]) -> dict:
    out = {}
    for l in lines:
        m = KV_RE.match(l)
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
    return out


# ---------------------------------------------------------------------------
# 2. Code-block parsers (OpenAPI, proto, SQL, Kubernetes)
# ---------------------------------------------------------------------------
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def classify_block(block: dict) -> str:
    code, lang = block["code"], block["lang"]
    if re.search(r"^\s*openapi\s*:", code, re.M):
        return "openapi"
    if re.search(r'syntax\s*=\s*"proto', code):
        return "proto"
    if "apiVersion" in code and re.search(r"^\s*kind\s*:", code, re.M):
        return "kubernetes"
    if lang == "sql" or re.search(r"\bCREATE\s+TABLE\b", code, re.I):
        return "sql"
    if lang == "csv":
        return "csv"
    lines = [x.strip() for x in code.splitlines() if x.strip()]
    if lines and all(re.fullmatch(r"[\w./-]+\.\w+", x) for x in lines):
        return "file_list"
    return "other"


def parse_openapi(code: str) -> dict:
    if yaml is None:
        return {"raw": code}
    try:
        spec = yaml.safe_load(code) or {}
    except yaml.YAMLError as e:
        return {"error": f"invalid YAML: {e}"}
    endpoints = []
    for path, ops in (spec.get("paths") or {}).items():
        for method, op in (ops or {}).items():
            if method.lower() not in HTTP_METHODS:
                continue
            op = op or {}
            responses = op.get("responses") or {}
            fields = {}
            for r in responses.values():
                schema = ((((r or {}).get("content") or {}).get("application/json") or {})
                          .get("schema") or {})
                for name, prop in (schema.get("properties") or {}).items():
                    fields[name] = (prop or {}).get("type", "")
            endpoints.append({
                "method": method.upper(), "path": path,
                "summary": op.get("summary", ""),
                "has_request_body": "requestBody" in op,
                "response_codes": [str(k) for k in responses],
                "response_fields": fields,
            })
    info = spec.get("info") or {}
    return {"openapi": str(spec.get("openapi", "")), "title": info.get("title", ""),
            "version": str(info.get("version", "")), "endpoints": endpoints}


def parse_proto(code: str) -> dict:
    pkg = re.search(r"^\s*package\s+([\w.]+)\s*;", code, re.M)
    services = []
    for m in re.finditer(r"service\s+(\w+)\s*\{(.*?)\n\}", code, re.S):
        rpcs = [{"name": a, "request": b, "response": c} for a, b, c in re.findall(
            r"rpc\s+(\w+)\s*\(\s*(?:stream\s+)?([\w.]+)\s*\)\s*returns\s*\(\s*(?:stream\s+)?([\w.]+)\s*\)",
            m.group(2))]
        services.append({"name": m.group(1), "rpcs": rpcs})
    messages = []
    for m in re.finditer(r"message\s+(\w+)\s*\{([^}]*)\}", code):
        fields = [{"type": t, "name": n, "number": int(num), "repeated": bool(rep)}
                  for rep, t, n, num in re.findall(
                      r"(repeated\s+)?([\w.]+)\s+(\w+)\s*=\s*(\d+)", m.group(2))]
        messages.append({"name": m.group(1), "fields": fields})
    return {"package": pkg.group(1) if pkg else "", "services": services, "messages": messages}


def _split_top_level(s: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def parse_sql(code: str) -> list[dict]:
    tables = []
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)\s*;",
                         code, re.S | re.I):
        cols = []
        for part in _split_top_level(m.group(2)):
            p = part.strip()
            if not p or re.match(r"(PRIMARY|FOREIGN|UNIQUE|CONSTRAINT|CHECK)\b", p, re.I):
                continue
            toks = p.split(None, 2)
            cols.append({"name": toks[0], "type": toks[1] if len(toks) > 1 else "",
                         "constraints": toks[2] if len(toks) > 2 else ""})
        tables.append({"name": m.group(1), "columns": cols})
    return tables


def parse_kubernetes(code: str) -> list[dict]:
    if yaml is None:
        return [{"raw": code}]
    try:
        docs = [d for d in yaml.safe_load_all(code) if d]
    except yaml.YAMLError as e:
        return [{"error": f"invalid YAML: {e}"}]
    out = []
    for d in docs:
        spec = d.get("spec") or {}
        pod = ((spec.get("template") or {}).get("spec") or {})
        containers = []
        for c in pod.get("containers") or []:
            containers.append({
                "name": c.get("name"), "image": c.get("image"),
                "ports": [p.get("containerPort") for p in c.get("ports") or []],
                "has_liveness_probe": "livenessProbe" in c,
                "has_readiness_probe": "readinessProbe" in c,
                "has_resources": "resources" in c,
            })
        out.append({"kind": d.get("kind"), "name": (d.get("metadata") or {}).get("name"),
                    "replicas": spec.get("replicas"), "containers": containers})
    return out


# ---------------------------------------------------------------------------
# 3. Architecture document -> structured dict
# ---------------------------------------------------------------------------
TECH_RE = re.compile(r"^(?P<cat>[^:]+):\s*(?P<tech>.+?)\s*\(Justification:\s*(?P<just>.*)\)\s*$", re.I)
VERSION_RE = re.compile(r"^(?P<name>.+?)\s+(?P<ver>\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)$")


def _tech(name_ver: str) -> dict:
    m = VERSION_RE.match(name_ver.strip())
    if m:
        return {"name": m.group("name"), "version": m.group("ver").replace(" ", "")}
    return {"name": name_ver.strip(), "version": ""}


def parse_architecture_doc(text: str) -> dict:
    secs = parse_markdown(text)
    all_text = text

    # --- executive summary --------------------------------------------------
    exec_sec = (_sections(secs, r"executive summary") or [secs[0]])[0]
    summary = exec_sec["paragraphs"][0] if exec_sec["paragraphs"] else ""
    kv = _kv(exec_sec["paragraphs"])
    name_m = re.search(r"The\s+([A-Z][\w ]+?)\s+system", summary)
    project = {
        "name": name_m.group(1) if name_m else "Project",
        "summary": summary,
        "architectural_style": kv.get("Chosen architectural style", ""),
        "deployment_topology": kv.get("Deployment topology", ""),
    }

    # --- requirements / traceability ------------------------------------------
    trace = _find_table(secs, "requirement id")
    requirements = []
    for row in (trace or {}).get("rows", []):
        requirements.append({
            "id": _col(row, "requirement id"),
            "short_text": _col(row, "short text"),
            "diagrams": _split_list(_col(row, "diagram")),
            "components": _split_list(_col(row, "component")),
            "artifacts": _split_list(_col(row, "artifact")),
            "rationale": _col(row, "rationale"),
        })

    # --- components (overview) ------------------------------------------------
    components = []
    for s in _sections(secs, r"architecture overview"):
        for b in s["bullets"]:
            m = re.match(r"^(\w+):\s*(.+)$", b)
            if m:
                components.append({"name": m.group(1), "responsibility": m.group(2)})

    # --- per-component detailed design -------------------------------------------
    designs: dict[str, dict] = {}
    for s in secs:
        p = s["path"]
        if len(p) >= 2 and re.search(r"detailed technical design", p[0], re.I):
            d = designs.setdefault(p[1], {"responsibilities": [], "technology_options": [],
                                          "recommended_stack": [], "stack_justification": "",
                                          "caching_and_consistency": []})
            sub = p[2] if len(p) > 2 else ""
            if re.search(r"responsibilit", sub, re.I):
                d["responsibilities"] += s["paragraphs"] + s["bullets"]
            elif re.search(r"technology options", sub, re.I):
                for b in s["bullets"]:
                    m = TECH_RE.match(b)
                    if m:
                        d["technology_options"].append({
                            "category": m.group("cat").strip(), **_tech(m.group("tech")),
                            "justification": m.group("just").strip(),
                            "justifies": REQ_ID_RE.findall(m.group("just"))})
            elif re.search(r"recommended", sub, re.I):
                d["recommended_stack"] += [_tech(b) for b in s["bullets"]]
                d["stack_justification"] = _kv(s["paragraphs"]).get("Justification", "")
            elif re.search(r"caching", sub, re.I):
                d["caching_and_consistency"] += s["bullets"]
    for c in components:
        c["has_detailed_design"] = c["name"] in designs

    # --- code blocks (deduplicated: section L repeats section D) ----------------
    blocks, seen = [], set()
    for s in secs:
        for b in s["code_blocks"]:
            digest = hashlib.sha1(re.sub(r"\s+", " ", b["code"]).encode()).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            blocks.append({**b, "kind": classify_block(b), "found_in": " > ".join(s["path"]) or "(preamble)"})

    interfaces = {"openapi": None, "proto": None}
    tables, k8s, deliverables = [], [], []
    for b in blocks:
        if b["kind"] == "openapi":
            interfaces["openapi"] = parse_openapi(b["code"])
        elif b["kind"] == "proto":
            interfaces["proto"] = parse_proto(b["code"])
        elif b["kind"] == "sql":
            tables += parse_sql(b["code"])
        elif b["kind"] == "kubernetes":
            k8s += parse_kubernetes(b["code"])
        elif b["kind"] == "file_list":
            deliverables += [x.strip() for x in b["code"].splitlines() if x.strip()]

    # --- ops / SLO / test matrix / assumptions --------------------------------
    slo = {}
    for s in _sections(secs, r"\bSLO"):
        slo.update(_kv(s["bullets"]))
    db_ha = sum((s["bullets"] for s in _sections(secs, r"DB HA")), [])

    tm = _find_table(secs, "test type",
                     predicate=lambda t: any(c.lower().endswith("component") for c in t["columns"]))
    test_matrix = None
    if tm:
        filled = [v for r in tm["rows"] for k, v in r.items() if k != tm["columns"][0] and v]
        test_matrix = {"columns": tm["columns"],
                       "test_types": [r[tm["columns"][0]] for r in tm["rows"]],
                       "is_empty": not filled}

    assumptions = []
    for s in secs:
        for l in s["paragraphs"] + s["bullets"]:
            m = re.match(r"^(A\d+):\s*(.+)$", l)
            if m:
                assumptions.append({"id": m.group(1), "text": m.group(2)})
    open_questions = sum((s["bullets"] for s in _sections(secs, r"unresolved|open question")), [])
    review_checklist = sum((s["bullets"] for s in _sections(secs, r"how to review")), [])

    risks = [{"risk": _col(r, "risk"), "mitigation": _col(r, "mitigation")}
             for r in (_find_table(secs, "risk", "mitigation") or {}).get("rows", [])]
    qa = [{"target": _col(r, "id"), "tests": _split_list(_col(r, "test type"))}
          for r in (_find_table(secs, "asr/nfr", "test type") or {}).get("rows", [])]

    # --- everything not captured structurally: compact notes ---------------------
    structured_tops = re.compile(r"executive summary|traceability|architecture overview|"
                                 r"detailed technical design|deliverables|how to review", re.I)
    notes: dict[str, list[str]] = {}
    for s in secs:
        if not s["path"] or structured_tops.search(s["path"][0]):
            continue
        top = _clean_title(s["path"][0])
        sub = s["title"] if s["level"] > 1 else ""
        for l in s["paragraphs"] + s["bullets"]:
            notes.setdefault(top, []).append(f"{sub}: {l}" if sub else l)

    return {
        "project": project,
        "analysis_plan": secs[0]["paragraphs"],
        "risks": risks,
        "qa_coverage": qa,
        "requirements": requirements,
        "requirement_ids_referenced": sorted(set(REQ_ID_RE.findall(all_text))),
        "components": components,
        "component_designs": designs,
        "interfaces": interfaces,
        "data_model": {"tables": tables},
        "deployment": {"kubernetes": k8s, "db_ha": db_ha},
        "slo": slo,
        "test_matrix": test_matrix,
        "assumptions": assumptions,
        "open_questions": open_questions,
        "deliverables": deliverables,
        "review_checklist": review_checklist,
        "notes": notes,
        "_code_blocks": blocks,  # kept for conflict checks, dropped from final JSON
    }


# ---------------------------------------------------------------------------
# 4. PlantUML views -> structured diagrams
# ---------------------------------------------------------------------------
DIAGRAM_LABEL_RE = re.compile(r"^\s*(\d+)\.\s*([A-Za-z]+)\s*[\u2014\u2013-]+\s*(.+?)\s*$")
KNOWN_KINDS = ("usecase", "class", "object", "state", "activity", "sequence",
               "collaboration", "package", "component", "deployment", "container")
ELEMENT_KW = ("actor|usecase|participant|object|state|artifact|node|component|database|"
              "container|boundary|control|entity|cloud|queue|collections")
GROUP_RE = re.compile(r"^(package|rectangle|namespace|node|frame|folder|cloud)\s+(.+?)\s*\{$", re.I)
CLASS_RE = re.compile(r"^(?:abstract\s+|interface\s+|enum\s+)?class\s+(\w+)\s*(\{)?\s*$", re.I)
ELEMENT_RE = re.compile(rf"^({ELEMENT_KW})\s+(.+?)\s*$", re.I)
NOTE_RE = re.compile(r"^note\s+(left|right|top|bottom|over)\s*(?:of\s+)?(\w+)?\s*:\s*(.+)$", re.I)
_NODE = r'(\[\*\]|\(\w+\)|\w+|"[^"]+")'
EDGE_RE = re.compile(
    rf'^{_NODE}\s*(<\|?|<<?|\*|o|\+|#)?([-.]+(?:\[[^\]]*\])?[-.]*)(\|?>{{1,2}}|\*|o|\+|#)?\s*{_NODE}'
    rf'(?:\s*:\s*(.*))?$')
MEMBER_RE = re.compile(r"^([-+#~])?\s*(\w+)\s*(\(([^)]*)\))?\s*(?::\s*(.+))?$")
VISIBILITY = {"-": "private", "+": "public", "#": "protected", "~": "package"}


def _parse_decl(rest: str) -> tuple[str, str]:
    rest = rest.strip().rstrip("{").strip()
    for pat, id_g, label_g in (
            (r'^"([^"]+)"\s+as\s+\(?(\w+)\)?$', 2, 1),   # "Label" as Id
            (r'^\(?(\w+)\)?\s+as\s+"([^"]+)"$', 1, 2),   # Id as "Label"
            (r'^\(([^)]+)\)\s+as\s+(\w+)$', 2, 1),       # (Label) as Id
    ):
        m = re.match(pat, rest)
        if m:
            return m.group(id_g), m.group(label_g)
    m = re.match(r'^"([^"]+)"$', rest) or re.match(r"^\(?(\w+)\)?", rest)
    return (m.group(1), m.group(1)) if m else (rest, rest)


def _clean_node(n: str) -> str:
    return n.strip().strip('"').strip("()")


def _parse_member(s: str) -> dict | None:
    m = MEMBER_RE.match(s)
    if not m:
        return None
    vis, name, parens, params, typ = m.groups()
    member = {"visibility": VISIBILITY.get(vis or "", "unspecified"), "name": name,
              "type": (typ or "").strip()}
    if parens is not None:
        member["kind"] = "method"
        member["params"] = []
        for p in [x.strip() for x in (params or "").split(",") if x.strip()]:
            pn, _, pt = p.partition(":")
            member["params"].append({"name": pn.strip(), "type": pt.strip()})
    else:
        member["kind"] = "attribute"
    return member


def _relation_type(arrow: str) -> str:
    if "|>" in arrow or "<|" in arrow:
        return "inheritance"
    if "*" in arrow:
        return "composition"
    if "o" in arrow:
        return "aggregation"
    if "." in arrow:
        return "dependency"
    if ">" in arrow or "<" in arrow:
        return "directed_association"
    return "association"


def parse_plantuml_body(lines: list[str]) -> dict:
    """One tolerant pass that recognises the syntax of every diagram type.
    Diagram-specific shaping happens afterwards in _shape()."""
    p = {"elements": [], "members": {}, "edges": [], "flow": [], "notes": [], "unparsed": []}
    open_class, groups = None, []
    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("'") or s.lower() in ("left to right direction", "top to bottom direction"):
            continue
        if s == "}":
            if open_class:
                open_class = None
            elif groups:
                groups.pop()
            continue
        if open_class:
            m = _parse_member(s)
            (p["members"][open_class].append(m) if m else p["unparsed"].append(s))
            continue
        group = groups[-1] if groups else None
        low = s.lower()
        # activity syntax
        if low in ("start", "stop", "end", "kill", "detach"):
            p["flow"].append({"type": low}); continue
        if s.startswith(":"):
            p["flow"].append({"type": "action", "text": s[1:].rstrip(";").strip()}); continue
        m = re.match(r"^(else)?if\s*\((.+?)\)\s*then(?:\s*\((.+?)\))?$", s, re.I)
        if m:
            p["flow"].append({"type": "elseif" if m.group(1) else "if", "condition": m.group(2),
                              "branch": m.group(3) or ""}); continue
        m = re.match(r"^else(?:\s*\((.+?)\))?$", s, re.I)
        if m:
            p["flow"].append({"type": "else", "branch": m.group(1) or ""}); continue
        if re.match(r"^end\s?if$", s, re.I):
            p["flow"].append({"type": "endif"}); continue
        m = re.match(r"^(while|repeat\s+while)\s*\((.+?)\)", s, re.I)
        if m:
            p["flow"].append({"type": "loop", "condition": m.group(2)}); continue
        if re.match(r"^(repeat|endwhile|end\s?while)$", s, re.I):
            p["flow"].append({"type": low.replace(" ", "")}); continue
        # structural syntax
        m = GROUP_RE.match(s)
        if m:
            gid, label = _parse_decl(m.group(2))
            p["elements"].append({"keyword": m.group(1).lower(), "id": gid, "label": label,
                                  "group": group, "is_group": True})
            groups.append(gid); continue
        m = CLASS_RE.match(s)
        if m:
            p["elements"].append({"keyword": "class", "id": m.group(1), "label": m.group(1), "group": group})
            p["members"].setdefault(m.group(1), [])
            if m.group(2):
                open_class = m.group(1)
            continue
        m = ELEMENT_RE.match(s)
        if m:
            eid, label = _parse_decl(m.group(2))
            p["elements"].append({"keyword": m.group(1).lower(), "id": eid, "label": label, "group": group})
            continue
        m = NOTE_RE.match(s)
        if m:
            p["notes"].append({"position": m.group(1), "target": m.group(2), "text": m.group(3)}); continue
        m = EDGE_RE.match(s)
        if m:
            a, head, line, tail, b, label = m.groups()
            p["edges"].append({"from": _clean_node(a), "to": _clean_node(b),
                               "arrow": f"{head or ''}{line}{tail or ''}", "label": (label or "").strip()})
            continue
        p["unparsed"].append(s)
    return p


def _shape(kind: str, p: dict) -> tuple[dict, list[dict]]:
    els, edges = p["elements"], p["edges"]
    issues = []
    if p["unparsed"]:
        issues.append({"type": "unparsed_lines", "detail": p["unparsed"]})

    if kind == "usecase":
        actors = {e["id"]: e["label"] for e in els if e["keyword"] == "actor"}
        ucs = [{"id": e["id"], "label": e["label"], "boundary": e["group"]}
               for e in els if e["keyword"] == "usecase"]
        assoc = []
        for e in edges:
            if e["from"] in actors:
                assoc.append({"actor": e["from"], "use_case": e["to"]})
            elif e["to"] in actors:
                assoc.append({"actor": e["to"], "use_case": e["from"]})
        return {"actors": [{"id": k, "label": v} for k, v in actors.items()],
                "use_cases": ucs, "associations": assoc}, issues

    if kind == "class":
        classes = []
        for e in els:
            if e["keyword"] != "class":
                continue
            mem = p["members"].get(e["id"], [])
            classes.append({"name": e["id"],
                            "attributes": [{k: v for k, v in m.items() if k != "kind"} for m in mem if m["kind"] == "attribute"],
                            "methods": [{k: v for k, v in m.items() if k != "kind"} for m in mem if m["kind"] == "method"]})
        rels = []
        for e in edges:
            r = {"from": e["from"], "to": e["to"], "arrow": e["arrow"], "type": _relation_type(e["arrow"])}
            if r["type"] in ("composition", "aggregation"):
                # PlantUML: the diamond sits next to the WHOLE.  A *-- B -> A is whole.
                diamond_left = e["arrow"][0] in "*o"
                r["whole"], r["part"] = (e["from"], e["to"]) if diamond_left else (e["to"], e["from"])
            rels.append(r)
        return {"classes": classes, "relations": rels}, issues

    if kind == "object":
        objs = [{"id": e["id"], "declared_with": e["keyword"]} for e in els]
        wrong = [o["id"] for o in objs if o["declared_with"] != "object"]
        if wrong:
            issues.append({"type": "wrong_keyword",
                           "detail": f"object diagram declares {wrong} with 'participant' "
                                     f"(sequence-diagram syntax); PlantUML expects 'object'"})
        return {"objects": objs, "links": [{"from": e["from"], "to": e["to"], "label": e["label"]}
                                           for e in edges]}, issues

    if kind == "state":
        states = [e["id"] for e in els if e["keyword"] == "state"]
        trans = [{"from": e["from"], "to": e["to"], "event": e["label"]} for e in edges]
        initial = [t["to"] for t in trans if t["from"] == "[*]"]
        final = [t["from"] for t in trans if t["to"] == "[*]"]
        if not final:
            issues.append({"type": "no_final_state", "detail": "no transition to [*]"})
        return {"states": states, "initial": initial, "final": final, "transitions": trans}, issues

    if kind == "activity":
        if not any(f["type"] in ("loop", "repeat") for f in p["flow"]):
            issues.append({"type": "no_loop", "detail": "activity flow is linear (no while/repeat)"})
        return {"flow": p["flow"]}, issues

    if kind in ("sequence", "collaboration"):
        parts = [e["id"] for e in els]
        msgs = []
        for i, e in enumerate(edges, 1):
            msgs.append({"seq": i, "from": e["from"], "to": e["to"], "text": e["label"],
                         "is_return": e["label"].lower().startswith("return")})
            for n in (e["from"], e["to"]):
                if n not in parts:
                    issues.append({"type": "undeclared_participant", "detail": n})
        return {"participants": parts, "messages": msgs, "notes": p["notes"]}, issues

    if kind == "package":
        pkgs = [{"name": e["id"],
                 "classes": [c["id"] for c in els if c["keyword"] == "class" and c["group"] == e["id"]]}
                for e in els if e.get("is_group")]
        return {"packages": pkgs, "dependencies": [{"from": e["from"], "to": e["to"], "arrow": e["arrow"]}
                                                    for e in edges]}, issues

    # component / deployment / container (and anything unknown)
    return {"elements": [{"name": e["id"], "type": e["keyword"]} for e in els],
            "connections": [{"from": e["from"], "to": e["to"], "arrow": e["arrow"], "label": e["label"]}
                            for e in edges]}, issues


def parse_views(text: str) -> list[dict]:
    diagrams, view, label, in_uml, buf = [], "", None, False, []
    for raw in text.splitlines():
        line = raw.strip()
        if not in_uml:
            m = re.match(r"^##\s+(.+)$", line)
            if m:
                view = m.group(1).strip()
                continue
            m = DIAGRAM_LABEL_RE.match(line)
            if m:
                label = {"number": int(m.group(1)), "kind": m.group(2).lower(), "title": m.group(3)}
                continue
            f = FENCE_RE.match(line)
            if f and f.group(1).lower() in ("plantuml", "puml", "uml"):
                in_uml, buf = True, []
            continue
        if FENCE_RE.match(line):
            in_uml = False
            diagrams.append(_build_diagram(buf, view, label))
            continue
        buf.append(raw)
    return diagrams


def _build_diagram(lines: list[str], view: str, label: dict | None) -> dict:
    name, body, inside = "", [], False
    for l in lines:
        s = l.strip()
        if s.lower().startswith("@startuml"):
            name, inside = s[len("@startuml"):].strip(), True
            continue
        if s.lower().startswith("@enduml"):
            break
        if inside:
            body.append(l)
    kind = (label or {}).get("kind", "")
    if kind not in KNOWN_KINDS:  # fall back to the @startuml name
        kind = next((k for k in KNOWN_KINDS if k in name.lower()), "unknown")
    shaped, issues = _shape(kind, parse_plantuml_body(body))
    return {"id": name or f"diagram{(label or {}).get('number', '')}", "kind": kind,
            "view": view, "title": (label or {}).get("title", ""), **shaped, "issues": issues}


# ---------------------------------------------------------------------------
# 5. Cross-document conflict detection
# ---------------------------------------------------------------------------
def _base(name: str) -> str:
    return re.sub(r"(Component|Container|Server|Client|Service)$", "", name)


def _words(s: str) -> set[str]:
    stop = {"the", "a", "an", "of", "to", "for", "and", "game"}
    return {w for w in re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", s).lower())
            if len(w) > 2 and w not in stop}


# Root causes follow the MAAD Evaluator (Zhang, Li, Liang et al., arXiv:2503.20536):
# each mismatch is traced back to the architecture agent whose output caused it.
ROOT_CAUSES = {
    "delivery_target": ("external", "New constraint from the evaluation brief, not present in the SRS."),
    "traceability": ("Analyst", "Incomplete requirement analysis: IDs cited but never specified."),
    "artifacts": ("Analyst", "Artifact names differ between the traceability matrix and the deliverables."),
    "components": ("Designer", "Designer views add or omit elements from the Modeler's logical view."),
    "data_types": ("Designer", "Interface specifications are not unified across layers."),
    "interfaces": ("Designer", "Interface design does not cover all modelled behaviour."),
    "data_model": ("Designer", "Data design does not cover all persisted domain classes."),
    "slo": ("Modeler", "Quality-attribute targets are numerically inconsistent."),
    "scope": ("Modeler", "Requirements in the summary were not carried into the views."),
    "testing": ("Modeler", "Verification plan left unspecified."),
    "operations": ("Modeler", "Deployment plan not refined to production level."),
}


def detect_conflicts(doc: dict, diagrams: list[dict], target: str) -> list[dict]:
    out: list[dict] = []

    def add(sev, cat, finding, evidence, refinement):
        role, cause = ROOT_CAUSES.get(cat, ("Designer", "Modelling notation or semantics error in a UML view."))
        out.append({"id": f"C{len(out) + 1:02d}", "severity": sev, "category": cat,
                    "finding": finding, "evidence": evidence,
                    "root_cause": cause, "responsible_role": role, "refinement": refinement})

    by_kind: dict[str, list[dict]] = {}
    for d in diagrams:
        by_kind.setdefault(d["kind"], []).append(d)
    endpoints = (doc["interfaces"].get("openapi") or {}).get("endpoints", [])
    tables = {t["name"].lower() for t in doc["data_model"]["tables"]}

    # 0. delivery target vs documented deployment
    style = f'{doc["project"]["architectural_style"]} {doc["project"]["deployment_topology"]}'.lower()
    if target == "desktop" and (re.search(r"microservice|cloud|web", style + doc["project"]["summary"].lower())
                                or doc["deployment"]["kubernetes"]):
        add("high", "delivery_target",
            "Documentation describes a web-based, cloud microservice system; the evaluation requires a desktop application.",
            [f'style="{doc["project"]["architectural_style"]}"', f'topology="{doc["project"]["deployment_topology"]}"',
             "summary says 'web-based'", f'{len(doc["deployment"]["kubernetes"])} Kubernetes object(s)'],
            "Build a desktop app. Keep each documented component as an in-process module behind an interface "
            "that mirrors the OpenAPI/proto contracts; use embedded SQLite instead of PostgreSQL; keep "
            "Dockerfile/k8s only as optional artifacts.")

    # 1. requirements referenced but never defined
    defined = {r["id"] for r in doc["requirements"]}
    missing = sorted(set(doc["requirement_ids_referenced"]) - defined)
    if missing:
        add("high", "traceability",
            f"Requirement IDs used in the document but absent from the traceability matrix: {missing}.",
            [f"traceability defines {sorted(defined)}"],
            "Add the missing IDs with an explicit short text derived from where they are cited, and mark them as inferred.")
    if len(defined) < 5:
        add("medium", "traceability",
            f"Only {len(defined)} requirements are traced and the source Requirements document is not provided.",
            [f"use cases in views: {[u['label'] for d in by_kind.get('usecase', []) for u in d['use_cases']]}"],
            "Derive functional requirements from use cases, state events and sequence messages; label them as derived.")

    # 2. artifact names in traceability vs deliverables list
    trace_art = {a for r in doc["requirements"] for a in r["artifacts"]}
    deliv = set(doc["deliverables"])
    if trace_art - deliv:
        add("medium", "artifacts",
            f"Traceability names artifacts that are not in the deliverables list: {sorted(trace_art - deliv)}.",
            [f"deliverables: {sorted(deliv)}"], "Generate both names or rename consistently; record the choice.")

    # 3. component sets disagree between prose and diagrams
    prose = {_base(c["name"]) for c in doc["components"]}
    mismatch, extra = [], set()
    for d in by_kind.get("component", []) + by_kind.get("container", []) + by_kind.get("deployment", []):
        drawn = {_base(e["name"]) for e in d["elements"]}
        if drawn != prose:
            extra |= drawn - prose
            mismatch.append(f'{d["id"]}: extra {sorted(drawn - prose)}, missing {sorted(prose - drawn)}')
    if mismatch:
        add("medium", "components",
            f"Diagrams add components {sorted(extra)} that the architecture overview "
            f"(lists {sorted(prose)}) does not define.",
            mismatch, "Use the union of components; give each one an owner module and responsibilities.")
    no_design = [c["name"] for c in doc["components"] if not c["has_detailed_design"]]
    if no_design:
        add("medium", "components",
            f"Components without a detailed design section: {no_design}.",
            [f'detailed designs exist for {sorted(doc["component_designs"])}'],
            "Derive responsibilities, interfaces and schema for them from the class and sequence diagrams.")

    # 4. identifier type mismatch (class diagram vs SQL/OpenAPI/proto)
    id_types = {}
    for d in by_kind.get("class", []):
        for c in d["classes"]:
            for a in c["attributes"]:
                if a["name"].lower() == "id":
                    id_types[f'class {c["name"]}.id'] = a["type"]
    for t in doc["data_model"]["tables"]:
        for col in t["columns"]:
            if col["name"].lower() == "id":
                id_types[f'sql {t["name"]}.id'] = col["type"]
    for e in endpoints:
        for f, typ in e["response_fields"].items():
            if f.lower().endswith("id"):
                id_types[f'openapi {e["path"]}.{f}'] = typ
    for m in (doc["interfaces"].get("proto") or {}).get("messages", []):
        for f in m["fields"]:
            if f["name"].lower().endswith("id"):
                id_types[f'proto {m["name"]}.{f["name"]}'] = f["type"]
    norm = {"string": "string", "str": "string", "uuid": "string", "text": "string"}
    families = {norm.get(v.lower(), "integer") for v in id_types.values() if v}
    if len(families) > 1:
        add("medium", "data_types", "Identifier types disagree across class diagram, SQL, OpenAPI and proto.",
            [f"{k}: {v}" for k, v in id_types.items()],
            "Pick one identifier type for all layers and document it.")

    # 5. behaviour with no interface (use cases, state events, actor messages)
    api_words = set().union(*[_words(f'{e["path"]} {e["summary"]}') for e in endpoints]) if endpoints else set()
    behaviours = []
    for d in by_kind.get("usecase", []):
        behaviours += [f'use case "{u["label"]}"' for u in d["use_cases"] if not (_words(u["label"]) & api_words)]
    for d in by_kind.get("state", []):
        behaviours += [f'state event {t["event"]}' for t in d["transitions"]
                       if t["event"] and not (_words(t["event"]) & api_words)]
    seen_msg = set()
    for d in by_kind.get("sequence", []):
        for m in d["messages"]:
            if m["from"] in ("User", "Admin") and not (_words(m["text"]) & api_words) and m["text"] not in seen_msg:
                seen_msg.add(m["text"])
                behaviours.append(f'message {m["from"]}->{m["to"]} "{m["text"]}"')
    if behaviours:
        add("high", "interfaces",
            f"{len(behaviours)} modelled behaviours have no operation in the OpenAPI/proto contracts.",
            behaviours, "Add an operation for each; keep names traceable to the diagram element.")
    for e in endpoints:
        if e["method"] == "GET" and _words(e["summary"]) & {"play", "start", "create", "update", "submit"}:
            add("low", "interfaces", f'{e["method"]} {e["path"]} changes state ("{e["summary"]}") but uses GET.',
                [f'{e["method"]} {e["path"]}'], "Use POST for state-changing operations.")

    # 6. persisted entities without a schema
    no_table = []
    for d in by_kind.get("class", []):
        for c in d["classes"]:
            n = c["name"].lower()
            if n not in tables and f"{n}s" not in tables:
                no_table.append(c["name"])
    if no_table:
        add("high", "data_model",
            f"Classes with no SQL table: {no_table} (ASR-1 requires question data to be durable).",
            [f"tables defined: {sorted(tables)}"], "Create a table for every persisted class.")

    # 7. SLO arithmetic
    slo_m = re.search(r"([\d.]+)\s*%", doc["slo"].get("SLO", ""))
    eb_m = re.search(r"([\d.]+)\s*%", doc["slo"].get("Error budget", ""))
    if slo_m and eb_m:
        expected = round(100 - float(slo_m.group(1)), 6)
        if abs(expected - float(eb_m.group(1))) > 1e-9:
            add("low", "slo", f"SLO {slo_m.group(1)}% implies an error budget of {expected}%, "
                              f"not {eb_m.group(1)}%.",
                [f'SLO: {doc["slo"].get("SLO")}', f'Error budget: {doc["slo"].get("Error budget")}'],
                "Correct one of the two numbers; for a desktop app, restate as availability of local features.")

    # 8. class-diagram composition read literally
    odd = [f'{r["from"]} {r["arrow"]} {r["to"]} => whole={r["whole"]}, part={r["part"]}'
           for d in by_kind.get("class", []) for r in d["relations"] if r["type"] == "composition"]
    if odd:
        add("medium", "uml_semantics",
            "Composition arrows, read with PlantUML semantics, put the diamond on the opposite class "
            "from what the other diagrams suggest.", odd,
            "Treat as: Game aggregates Questions, User plays Games, Admin manages Questions; record the reinterpretation.")

    # 9. per-diagram issues (syntax, missing final state, linear flow)
    for d in diagrams:
        for i in d["issues"]:
            add("low" if i["type"] in ("unparsed_lines",) else "medium", "uml_" + i["type"],
                f'{d["id"]}: {i["detail"] if isinstance(i["detail"], str) else i["type"]}',
                i["detail"] if isinstance(i["detail"], list) else [i["detail"]],
                {"wrong_keyword": "Read the declarations as objects.",
                 "no_final_state": "Add GameOver -> [*] (return to menu / exit).",
                 "no_loop": "Loop over the question list until all questions are answered."}
                .get(i["type"], "Review this diagram manually."))

    # 10. collaboration diagrams that duplicate sequence diagrams
    def sig(d):
        return [(m["from"], m["to"], m["text"]) for m in d["messages"]]
    for c in by_kind.get("collaboration", []):
        for s in by_kind.get("sequence", []):
            if sig(c) and sig(c) == sig(s):
                add("low", "uml_redundancy", f'{c["id"]} repeats {s["id"]} message-for-message.',
                    [f'{len(sig(c))} identical messages'], "Treat them as one scenario.")

    # 11. features promised in the summary but absent from every diagram
    uml_text = json.dumps(diagrams).lower()
    missing_feats = [f for f in ("movie", "menu", "ending", "feedback", "help")
                     if f in doc["project"]["summary"].lower() and f not in uml_text]
    if missing_feats:
        add("medium", "scope", f"Summary features not modelled in any diagram: {missing_feats}.",
            [doc["project"]["summary"][:160] + "..."],
            "Implement them as screens of the game (intro, main menu, ending) and trace them to the summary.")

    # 12. empty test matrix
    if doc["test_matrix"] and doc["test_matrix"]["is_empty"]:
        add("medium", "testing", "Test matrix lists test types but every cell is empty.",
            doc["test_matrix"]["test_types"], "Fill it with the concrete test files the generator produces.")

    # 13. Kubernetes manifest quality
    for k in doc["deployment"]["kubernetes"]:
        probs = []
        for c in k.get("containers", []):
            if str(c.get("image", "")).endswith(":latest"):
                probs.append(f'image {c["image"]} uses :latest')
            if not (c.get("has_liveness_probe") or c.get("has_readiness_probe")):
                probs.append("no liveness/readiness probes")
            if not c.get("has_resources"):
                probs.append("no resource requests/limits")
        if len(doc["deployment"]["kubernetes"]) == 1 and len(prose) > 1:
            probs.append(f"one Deployment for a {len(prose)}-component microservice design")
        if probs:
            add("low", "operations", f'Kubernetes object {k.get("name")} is not production-ready.', probs,
                "Pin image tags, add probes and resources, one Deployment per service (optional for desktop).")
    return out


# ---------------------------------------------------------------------------
# 6. Glue
# ---------------------------------------------------------------------------
def build(doc_path: Path, views_path: Path, target: str = "desktop") -> dict:
    doc_text = doc_path.read_text(encoding="utf-8-sig")      # -sig: tolerate a Windows BOM
    views_text = views_path.read_text(encoding="utf-8-sig")
    doc = parse_architecture_doc(doc_text)
    diagrams = parse_views(views_text)
    conflicts = detect_conflicts(doc, diagrams, target)
    doc.pop("_code_blocks", None)
    return {
        "schema_version": SCHEMA_VERSION,
        "sources": {"documentation": doc_path.name, "views": views_path.name,
                    "documentation_sha1": hashlib.sha1(doc_text.encode()).hexdigest()[:12],
                    "views_sha1": hashlib.sha1(views_text.encode()).hexdigest()[:12]},
        "delivery_target": target,
        **doc,
        "views": {"count": len(diagrams), "diagrams": diagrams},
        "conflicts": conflicts,
    }


def compact_json(data: dict) -> str:
    """The form we will send to the LLM: no indentation, no spaces."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Architecture docs -> structured JSON")
    ap.add_argument("--doc", required=True, type=Path, help="Architecture_Documentation.md")
    ap.add_argument("--views", required=True, type=Path, help="Architecture_View.md")
    ap.add_argument("--out", default=Path("build/architecture.json"), type=Path)
    ap.add_argument("--target", default="desktop", choices=["desktop", "web", "service"])
    a = ap.parse_args(argv)

    for p in (a.doc, a.views):
        if not p.is_file():
            print(f"ERROR: file not found: {p}", file=sys.stderr)
            return 2
    data = build(a.doc, a.views, a.target)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    sev = {s: sum(c["severity"] == s for c in data["conflicts"]) for s in ("high", "medium", "low")}
    compact = compact_json(data)
    print(f"Project       : {data['project']['name']}")
    print(f"Requirements  : {len(data['requirements'])} traced, referenced {data['requirement_ids_referenced']}")
    print(f"Components    : {[c['name'] for c in data['components']]}")
    print(f"Diagrams      : {data['views']['count']} ({', '.join(d['kind'] for d in data['views']['diagrams'])})")
    print(f"Conflicts     : {len(data['conflicts'])} (high {sev['high']}, medium {sev['medium']}, low {sev['low']})")
    print(f"Compact JSON  : {len(compact)} chars, ~{len(compact) // 4} tokens")
    print(f"Written to    : {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
