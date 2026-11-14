"""The docs the judges and the operator read.

The README must map every test of the spec (1 to 32) to the file or the
procedure that proves it, with the spec's own test names; every command the
docs tell the operator to run must exist; and no doc may carry a secret or a
phone number that could ring a real line. No database is needed.
"""
from __future__ import annotations

import re

import pytest

from levela.config import ROOT

REPO = ROOT.parent
SPEC = (REPO / "docs" / "Level_A_build_spec_and_test_plan.md").read_text(encoding="utf-8")
CONTRACTS = SPEC + (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8") + \
    (ROOT / "docs" / "DESIGN_V2.md").read_text(encoding="utf-8")
README = ROOT / "README.md"
RUNBOOK = ROOT / "docs" / "RUNBOOK.md"
DOCS = [REPO / "README.md", README, RUNBOOK, ROOT / "deploy" / "README.md", ROOT / "scripts" / "video_shots.md",
        ROOT / "agent" / "README.md"]
# Everything the ops work ships, scanned for secrets as well as the docs.
SHIPPED = DOCS + [ROOT / ".env.example", ROOT / "Makefile", ROOT / "deploy" / "render.yaml",
                  ROOT / "deploy" / "fly.toml", ROOT / "deploy" / "Dockerfile",
                  REPO / ".github" / "workflows" / "ci.yml", *sorted((ROOT / "scripts").glob("*.sh")),
                  *sorted((ROOT / "scripts").glob("*.ps1")), *sorted((ROOT / "agent").glob("*.json"))]


def _text(path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body of a markdown section, up to the next heading of the same or higher level."""
    level = heading.split(" ")[0]
    start = text.index(heading + "\n")
    rest = text[start + len(heading) + 1:]
    end = re.search(rf"^#{{1,{len(level)}}} ", rest, re.M)
    return rest[: end.start()] if end else rest


def _table_rows(section: str) -> dict[int, list[str]]:
    """{test number: cells} for `| n | ... |` rows."""
    rows = {}
    for line in section.splitlines():
        m = re.match(r"^\| (\d+) \|(.*)\|\s*$", line)
        if m:
            rows[int(m.group(1))] = [c.strip() for c in m.group(2).split("|")]
    return rows


def _spec_test_names() -> dict[int, str]:
    names = {}
    for heading in ("## Test plan", "### Added tests", "### Saboteur tests"):
        names.update({n: cells[0] for n, cells in _table_rows(_section(SPEC, heading)).items()})
    return names


# ---- the README ------------------------------------------------------------------

def test_readme_maps_every_test_1_to_32_with_the_specs_names():
    rows = _table_rows(_section(_text(README), "## Test map"))
    assert sorted(rows) == list(range(1, 33))
    spec = _spec_test_names()
    assert sorted(spec) == list(range(1, 33))
    for n, cells in rows.items():
        assert cells[0] == spec[n], f"test {n}: README says {cells[0]!r}, the spec {spec[n]!r}"


def test_every_test_names_the_file_or_procedure_that_proves_it():
    for n, cells in _table_rows(_section(_text(README), "## Test map")).items():
        proven_by = cells[-1]
        files = re.findall(r"`((?:tests|scripts|docs|agent)/[\w./-]+)`", proven_by)
        assert files, f"test {n} names no file"
        for path in files:
            # On disk, or (for another engineer's file not merged into this
            # branch) named by the spec or the design contracts.
            assert (ROOT / path).exists() or path.split("/")[-1] in CONTRACTS, f"test {n}: {path}"
        if not any(f.startswith("tests/") for f in files):
            assert proven_by.startswith("By hand"), f"test {n} has no automated test and is not marked by hand"


def test_readme_has_the_sections_the_spec_requires():
    text = _text(README)
    for heading in ("## What was built today", "## Real and simulated", "## Pre-existing tools used", "## Run it",
                    "## Architecture", "## The rules Atlas enforces", "## Test map"):
        assert heading + "\n" in text, heading
    assert "**Seeded for the demo:**" in text and "synthetic" in _section(text, "## Real and simulated")
    for tool in ("seed kit", "Atlas", "ElevenLabs", "Twilio", "Voyage", "FastAPI"):
        assert tool in _section(text, "## Pre-existing tools used"), tool
    assert "```text" in _section(text, "## Architecture")


def test_root_readme_is_one_screen_and_its_links_resolve():
    text = _text(REPO / "README.md")
    assert len(text.splitlines()) <= 60
    assert "Finish the Job" in text and "Simulated" in text
    links = re.findall(r"\]\(([^)#]+)\)", text)
    assert "level-a/README.md" in links
    for link in links:
        assert (REPO / link).is_file(), link


# ---- the commands the docs give ----------------------------------------------------

def _makefile_targets() -> set[str]:
    return set(re.findall(r"^([a-z0-9-]+):", _text(ROOT / "Makefile"), re.M))


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.relative_to(REPO).as_posix())
def test_every_command_in_the_docs_exists(doc):
    from test_scripts import _levela_parser, demo

    text = _text(doc)
    targets = _makefile_targets()
    for target in re.findall(r"`make ([a-z0-9-]+)", text):
        assert target in targets, f"make {target}"
    demo_commands = {b.command for b in demo.BEATS + demo.EXTRAS} | {"beats", "start", "status"}
    for command in re.findall(r"python scripts/demo\.py ([a-z][a-z-]*)", text):
        assert command in demo_commands, f"demo.py {command}"
    levela = _levela_parser()._subparsers._group_actions[0].choices
    for sub in re.findall(r"python -m levela ([a-z][a-z-]*)", text):
        assert sub in levela, f"python -m levela {sub}"
    for script in re.findall(r"scripts[/\\]([\w-]+\.(?:py|sh|ps1|md))", text):
        assert (ROOT / "scripts" / script).is_file() or f"scripts/{script}" in CONTRACTS, script


def test_runbook_covers_the_nine_beats_the_checklist_and_the_fallbacks():
    from test_scripts import demo

    text = _text(RUNBOOK)
    show = _section(text, "## The show")
    for beat in demo.BEATS:
        assert f"| {beat.at} | {beat.title.split(' (')[0]} |" in show, beat.title
        assert f"python scripts/demo.py {beat.command}" in text or beat.command == "kill-arm", beat.command
    for line in ("The model did not catch that. The database did.",
                 "A naive agent redials. Ours finds the evidence first.",
                 "Fifteen panels are unexplained, so Atlas will not let the recall close.",
                 "Nothing counts in a recall until every panel is explained."):
        assert line in show, line
    checklist = _section(text, "## Pre-show checklist")
    for step in ("make reset", "make preflight", "make seed", "make provision", "make order systems", "make demo",
                 "make supervisor", "4:55", "/healthz"):
        assert step in checklist, step
    assert "make kill-demo" in show
    assert len(_section(text, "## Fallbacks").splitlines()) > 10
    assert len(_section(text, "## Likely questions").splitlines()) > 10


def test_video_shot_list_has_the_three_segments_and_the_disclosure():
    text = _text(ROOT / "scripts" / "video_shots.md")
    for segment in ("**Call segment.**", "**Kill segment.**", "**Order system-change segment.**"):
        assert segment in text
    disclosure = _section(text, "## Disclosure line (on screen at the end, and in the submission text)")
    assert "Real:" in disclosure and "Simulated:" in disclosure and "synthetic" in disclosure


# ---- secrets and phone numbers -----------------------------------------------------

SECRET_PATTERNS = {
    "a connection string with a password": re.compile(r"mongodb(?:\+srv)?://[^\s:/@]+:(?!PASSWORD@)[^\s@]+@"),
    "an Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_-]{12,}"),
    "an ElevenLabs key": re.compile(r"\bsk_[A-Za-z0-9]{24,}"),
    "a Twilio account SID": re.compile(r"\bAC[0-9a-f]{32}\b"),
    "a Voyage key": re.compile(r"\bpa-[A-Za-z0-9_-]{24,}"),
}


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.relative_to(REPO).as_posix())
def test_no_secret_in_what_ships(path):
    text = _text(path)
    for what, pattern in SECRET_PATTERNS.items():
        assert not pattern.search(text), f"{path.name} contains {what}"


def test_no_value_from_the_local_env_file_in_what_ships():
    env = ROOT / ".env"
    if not env.is_file():
        pytest.skip("no level-a/.env here (CI): nothing local to leak")
    example = _text(ROOT / ".env.example")
    values = {v.strip().strip("'\"") for v in re.findall(r"^[A-Z][A-Z0-9_]*=(.*)$", _text(env), re.M)}
    private = {v for v in values if len(v) >= 8 and v not in example}
    for path in SHIPPED:
        text = _text(path)
        leaked = [v[:4] + "..." for v in private if v in text]
        assert not leaked, f"{path.name} contains values from .env: {leaked}"


PHONE_LIKE = re.compile(r"\+?\d[\d ().-]{8,}\d")


def _ringable(candidate: str) -> bool:
    """A North American number that could ring a line: not in the fictional
    555-0100..0199 block and not in the unassigned 555 area code. Any other
    run of 10 to 15 digits written like a phone number counts as ringable."""
    digits = re.sub(r"\D", "", candidate)
    if not 10 <= len(digits) <= 15:
        return False
    if len(digits) == 10 or (len(digits) == 11 and digits[0] == "1"):
        area, exchange, line = digits[-10:-7], digits[-7:-4], digits[-4:]
        return not (area == "555" or (exchange == "555" and line.startswith("01")))
    return True


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.relative_to(REPO).as_posix())
def test_no_real_looking_phone_number_in_the_docs(path):
    found = [m.group(0) for m in PHONE_LIKE.finditer(_text(path)) if _ringable(m.group(0))]
    assert not found, f"{path.name}: {found}"


def test_the_phone_check_tells_fictional_from_real():
    assert not _ringable("+1-416-555-0127") and not _ringable("+15550100123")
    assert _ringable("+1 416 555 2368") and _ringable("(905) 555-0300") and _ringable("+44 20 7946 0958")
    assert not _ringable("2026-11-14") and not _ringable("20261114")
