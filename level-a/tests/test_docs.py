"""The docs the judges and the operator read, checked against the spec.

The spec is `docs/All_Clear_build_spec_and_test_plan.md` at the repo root.
The README must lead with the spec's one sentence and a "Built today" list
(spec "Rules that can disqualify"), say what is real and what is simulated,
and map every spec test (1 to 49) to the file or procedure that proves it,
with the spec's own names; every endpoint the spec names must exist in the
code; every command the docs give must exist; no doc may carry a secret or a
phone number that could ring a real line; and the fire alarm words the spec
replaced ("Changed from the fire alarm version") must be gone. No database.
"""
from __future__ import annotations

import re

import pytest

from levela.config import ROOT

REPO = ROOT.parent
SPEC_PATH = REPO / "docs" / "All_Clear_build_spec_and_test_plan.md"
SPEC = SPEC_PATH.read_text(encoding="utf-8")
BUILD_PLAN = (ROOT / "docs" / "BUILD_PLAN.md").read_text(encoding="utf-8")
CONTRACTS = SPEC + BUILD_PLAN + (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8") + \
    (ROOT / "docs" / "DESIGN_V2.md").read_text(encoding="utf-8")
README = ROOT / "README.md"
RUNBOOK = ROOT / "docs" / "RUNBOOK.md"
VIDEO = ROOT / "scripts" / "video_shots.md"
DOCS = [REPO / "README.md", README, RUNBOOK, ROOT / "deploy" / "README.md", VIDEO, ROOT / "agent" / "README.md"]
# Everything the ops work ships, scanned for secrets as well as the docs.
SHIPPED = DOCS + [ROOT / ".env.example", ROOT / "Makefile", ROOT / "deploy" / "render.yaml",
                  ROOT / "deploy" / "fly.toml", ROOT / "deploy" / "Dockerfile",
                  REPO / ".github" / "workflows" / "ci.yml", *sorted((ROOT / "scripts").glob("*.sh")),
                  *sorted((ROOT / "scripts").glob("*.ps1")), *sorted((ROOT / "agent").glob("*.json"))]
SPEC_TESTS = range(1, 50)
# Spec test names that still carry fire alarm words, translated by the
# spec's "Changed from the fire alarm version" table (part maker -> plant,
# fixed version -> held, the fix canary -> the barcode check before blocks).
TRANSLATED_NAMES = {39: "Plant call and barcode check"}


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
    for heading in ("## Test plan", "### Tests 16 to 22", "### Tests 23 to 33", "### Tests", "### Tests 38 to 42",
                    "### Tests 43 to 49"):
        names.update({n: cells[0] for n, cells in _table_rows(_section(SPEC, heading)).items()})
    return names


def _readme_tests() -> dict[int, list[str]]:
    return _table_rows(_section(_text(README), "## Spec tests"))


# ---- the spec itself ---------------------------------------------------------------

def test_the_spec_numbers_its_tests_1_to_49():
    """The README's table is checked against these names; if the spec gains
    or renumbers a test, this fails first."""
    assert sorted(_spec_test_names()) == list(SPEC_TESTS)


def _api_endpoints() -> set[str]:
    """Every endpoint path the spec names (`GET /x`, `POST /y/{id}`), with the
    spec's fire alarm paths translated to the store ones."""
    found = set(re.findall(r"`(?:GET|POST|PUT|PATCH|DELETE) (/[^`?\s]*)", SPEC))
    renamed = {"/panel/{serial}": "/store/{id}", "/panels/{serial}/events": "/stores/{id}/events"}
    return {renamed.get(p, p) for p in found}


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


def test_every_endpoint_the_spec_names_is_in_the_code():
    """Each one is a route, or is named in the docstring of the router that
    owns it (a router still being filled lists its endpoints first)."""
    code = _norm("\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "levela").rglob("*.py")))
    missing = sorted(p for p in _api_endpoints() if _norm(p) not in code)
    assert not missing, f"named by the spec, absent from levela/: {missing}"
    for gone in ("/panel/", "/panels/"):
        assert gone not in code, f"{gone} is a fire alarm path; the spec renamed it to /store(s)/"


# ---- the README ------------------------------------------------------------------

def test_readme_leads_with_the_one_sentence_and_the_built_today_list():
    text = _text(README)
    sentence = re.search(r'\*\*One sentence:\*\* "(.+)"', SPEC).group(1)
    head = text.split("\n## ", 1)[0]
    assert sentence in head, "the README opens with the spec's one sentence"
    first_section = re.search(r"^## (.+)$", text, re.M).group(1)
    assert first_section == "Built today", "spec: a Built today list at the top of the README"
    built = _section(text, "## Built today")
    assert len(re.findall(r"^- ", built, re.M)) >= 6
    # Anything not built yet is marked as such, never claimed.
    for line in re.findall(r"^- .*$", built, re.M):
        assert "TODO" not in line


def test_readme_says_what_is_real_and_what_is_simulated():
    section = _section(_text(README), "## Real and simulated")
    real = " ".join(re.findall(r"^\| Real \|(.*)$", section, re.M))
    simulated = " ".join(re.findall(r"^\| Simulated \|(.*)$", section, re.M))
    for word in ("Atlas", "refusal", "ElevenLabs", "Twilio", "webhook", "public URL", "kill"):
        assert word in real, f"Real: {word}"
    for word in ("clock", "36 parties", "order systems", "shipment records", "tills", "stock records",
                 "SIMULATED", "Summit Foods"):
        assert word in simulated, f"Simulated: {word}"
    assert "invented" in section


def test_readme_claims_no_harm_and_no_first():
    """Spec: the demo product is invented, so never say anyone was hurt by it;
    on novelty, "we found none combining these", never "first"."""
    text = _text(README)
    assert "we found none combining these" in text.lower()
    assert not re.search(r"world'?s first|first ever|\bthe first (?:agent|system|tool|product|project|recall agent)",
                         text, re.I)
    assert not re.search(r"sickened|injur|hospitali[sz]|\bdied\b|deaths|illnesses", text, re.I)


def test_readme_has_the_sections_the_operator_needs():
    text = _text(README)
    for heading in ("## Built today", "## Real and simulated", "## Run it", "## Spec tests", "## Architecture",
                    "## The rules Atlas enforces", "## Pre-existing tools used"):
        assert heading + "\n" in text, heading
    run = _section(text, "## Run it")
    for step in ("make api", "make worker", "make order-systems", "make summit", "/store/", "make store-script",
                 "make test"):
        assert step in run, step
    assert "```text" in _section(text, "## Architecture")
    tools = _section(text, "## Pre-existing tools used")
    for tool in ("Atlas", "ElevenLabs", "Twilio", "FastAPI"):
        assert tool in tools, tool
    assert "seed kit" in tools and "not used" in tools.lower()


def test_readme_maps_every_spec_test_with_the_specs_names():
    rows = _readme_tests()
    assert sorted(rows) == list(SPEC_TESTS)
    spec = _spec_test_names()
    for n, cells in rows.items():
        want = TRANSLATED_NAMES.get(n, spec[n])
        assert cells[0] == want, f"test {n}: README says {cells[0]!r}, the spec {want!r}"


def _status(proven_by: str) -> str:
    for status in ("Not built yet", "By hand", "Needs a phone"):
        if proven_by.startswith(status):
            return status
    return "automated"


def test_every_spec_test_names_the_file_or_procedure_that_proves_it():
    for n, cells in _readme_tests().items():
        proven_by = cells[-1]
        status = _status(proven_by)
        files = re.findall(r"`((?:tests|scripts|docs|agent)/[\w./-]+)`", proven_by)
        if status == "Not built yet":
            # Say who is building it, so it is not a silent gap.
            assert re.search(r"\bWS[1-7]\b", proven_by), f"test {n}: not built, by which workstream?"
            continue
        assert files, f"test {n} names no file"
        for path in files:
            # On disk, or (another builder's file not merged yet) named by the contracts.
            assert (ROOT / path).exists() or path.split("/")[-1] in CONTRACTS, f"test {n}: {path}"
        if status == "automated":
            assert any(f.startswith("tests/") for f in files), f"test {n} has no test file and is not marked"


def _names_number(text: str, n: int) -> bool:
    return bool(re.search(rf"(?:test_spec_{n}_|\btest_{n}_|[Tt]ests? {n}\b|tests? \d+ (?:and|to) {n}\b|"
                          rf"tests? {n} (?:and|to) )", text))


def test_every_automated_spec_test_number_appears_in_the_tests():
    """Builders keep the spec's numbers in test names or docstrings
    (`test_spec_36_...`, "spec test 36"): a README row that says a test is
    automated must be findable in the files it names by its number, or by
    the older plan's number the row gives ("numbered 23 there")."""
    for n, cells in _readme_tests().items():
        proven_by = cells[-1]
        if _status(proven_by) != "automated":
            continue
        files = [ROOT / f for f in re.findall(r"`(tests/[\w./-]+\.py)`", proven_by) if (ROOT / f).is_file()]
        if not files:
            continue                                          # not merged yet: checked above against the contracts
        corpus = "\n".join(_text(f) for f in files)
        older = re.search(r"numbered (\d+) there", proven_by)
        number = int(older.group(1)) if older else n
        assert _names_number(corpus, number), \
            f"spec test {n}: {[f.name for f in files]} never name test {number}"


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
    makefile = _text(ROOT / "Makefile")
    for script in re.findall(r"scripts[/\\]([\w-]+\.(?:py|sh|ps1|md))", text):
        # On disk, named by the contracts, or behind a Makefile guard that says it is missing.
        assert (ROOT / "scripts" / script).is_file() or f"scripts/{script}" in CONTRACTS \
            or f"test -f scripts/{script} ||" in makefile, script


def test_runbook_covers_the_beats_the_buttons_the_checklist_and_the_fallbacks():
    from test_scripts import demo

    text = _text(RUNBOOK)
    show = _section(text, "## The show")
    for beat in demo.BEATS:
        assert f"| {beat.at} | {beat.title.split(' (')[0]} |" in show, beat.title
        assert f"python scripts/demo.py {beat.command}" in text or beat.command == "kill-arm", beat.command
    for line in ("The model did not catch that. The database did.",
                 "A naive agent redials. Ours finds the evidence first.",
                 "This is the store's own stock record, and it outranks the phone.",
                 "Thirty-nine tins are still open, so Atlas will not let us say all clear.",
                 "NOT ALL CLEAR: 39 tins open (24 still on a shelf, 15 disputed)"):
        assert line in show, line
    # Spec "Demo controls": the six buttons for the live part.
    buttons = _section(text, "## Operator buttons")
    for button in ("Release Harbour Market", "Arm failpoint", "Switch North to v2", "Saboteur",
                   "Open the store page", "Close recall"):
        assert f"**{button}**" in buttons, button
    checklist = _section(text, "## Pre-show checklist")
    for step in ("make reset", "make preflight", "make provision", "make order-systems", "make summit",
                 "make store-script", "make demo", "make supervisor", "4:55", "/healthz", "/store/"):
        assert step in checklist, step
    assert "make kill-demo" in show
    fallbacks = _section(text, "## Fallbacks")
    assert len(re.findall(r"^\| ", fallbacks, re.M)) >= 8
    for case in ("Voice fails", "second call", "Wi-Fi", "judge will not play", "repair stalls", "run goes long",
                 "store page"):
        assert case in fallbacks, case
    assert len(_section(text, "## Likely questions").splitlines()) > 10


def test_video_shot_list_has_the_segments_and_the_disclosure():
    text = _text(VIDEO)
    for segment in ("**Call segment.**", "**Kill segment.**", "**Order system change segment.**",
                    "**Store record segment.**", "**Close segment.**"):
        assert segment in text, segment
    disclosure = _section(text, "## Disclosure line (on screen at the end, and in the submission text)")
    assert "Real:" in disclosure and "Simulated:" in disclosure and "invented" in disclosure


# ---- the fire alarm words are gone ------------------------------------------------------

# Spec "Changed from the fire alarm version" and the brief's list. `lot`,
# `on_hand` and `portal` are the older food version's words.
FIRE_WORDS = re.compile(
    r"\bpanels?\b|\binstallers?\b|\bsounder|fixed version|fixed_version|firmware|Harbour Fire|Queen St Alarm|"
    r"Corner Properties|Lakeside Systems|Kestrel|DEMO-7|fire alarm|part maker|part_maker|fixed_by_panel|"
    r"connected_unfixed|model_seen|version_seen|sounder_test|\bon_hand\b|\blots?\b|\bportals?\b|"
    r"harbour-fire|queen-st-alarm|corner-properties|lakeside-systems",
    re.I)
OWNER_TYPE = re.compile(r"""["']type["']\s*:\s*["']owner["']|\btype\b\s*==\s*["']owner["']""")
# The ops workstream's files: no exception.
OPS_FILES = [README, RUNBOOK, VIDEO, ROOT / "docs" / "DESIGN.md", ROOT / "docs" / "DESIGN_V2.md",
             ROOT / "docs" / "vendor_reference.md", ROOT / "Makefile", ROOT / ".env.example",
             ROOT / "levela" / "__main__.py", ROOT / "levela" / "__init__.py", ROOT / "scripts" / "demo.py",
             *sorted((ROOT / "scripts").glob("*.sh")), *sorted((ROOT / "scripts").glob("*.ps1")),
             *sorted((ROOT / "deploy").glob("*")), REPO / ".github" / "workflows" / "ci.yml", REPO / "README.md"]


def _fire_hits(path) -> list[str]:
    hits = []
    for i, line in enumerate(_text(path).splitlines(), 1):
        if FIRE_WORDS.search(line) or OWNER_TYPE.search(line):
            hits.append(f"{path.relative_to(REPO).as_posix()}:{i}: {line.strip()[:100]}")
    return hits


@pytest.mark.parametrize("path", OPS_FILES, ids=lambda p: p.relative_to(REPO).as_posix())
def test_no_fire_alarm_word_in_the_ops_files(path):
    hits = _fire_hits(path)
    assert not hits, "\n".join(hits)


@pytest.mark.parametrize("folder", ["levela", "agent", "scenarios"])
def test_no_fire_alarm_word_in_the_product(folder):
    """The target of the conversion: the engine, the voice agent and the
    scenarios speak the food recall only. (`web/` is out of scope.)"""
    paths = [p for p in sorted((ROOT / folder).rglob("*"))
             if p.is_file() and p.suffix in (".py", ".md", ".json", ".txt", ".html", ".js", ".css")
             and "__pycache__" not in p.parts]
    hits = [h for p in paths for h in _fire_hits(p)]
    assert not hits, f"{len(hits)} lines still carry fire alarm words:\n" + "\n".join(hits[:40])


def test_the_word_check_catches_the_old_words_and_spares_the_new():
    for old in ("40 panels", "the installer", "Harbour Fire", "on_hand", "batch lot", "the portal", "DEMO-7",
                "part_maker", '"type": "owner"'):
        assert FIRE_WORDS.search(old) or OWNER_TYPE.search(old), old
    for new in ("Harbour Market", "on_shelf", "held_by_system", "slot", "pilot", "plant", "the owner of the lease",
                "Corner Mart", "operator page", "failpoint_fired"):
        assert not (FIRE_WORDS.search(new) or OWNER_TYPE.search(new)), new


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
# The invented product's barcode as printed (UPC-A, `0 62345 18803 5`) is not a phone number.
BARCODE = re.compile(r"^\d \d{5} \d{5} \d$")


def _ringable(candidate: str) -> bool:
    """A North American number that could ring a line: not in the fictional
    555-0100..0199 block and not in the unassigned 555 area code. Any other
    run of 10 to 15 digits written like a phone number counts as ringable,
    except a barcode written in its printed groups."""
    if BARCODE.match(candidate.strip()):
        return False
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
    assert not _ringable("0 62345 18803 5")
