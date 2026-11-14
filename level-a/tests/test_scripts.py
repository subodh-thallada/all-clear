"""The operator's tools: the Makefile, the shell and PowerShell scripts, the
rehearsal driver, CI and the deploy files.

No database and no network: every Makefile recipe is parsed by the real
argument parser of the command it runs, the shell scripts by `bash -n` (and
kill_demo.sh end to end with a stub interpreter), demo.py against a fake API
on httpx.MockTransport, and the YAML/TOML files by their parsers.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from itertools import count
from unittest import mock

import httpx
import pytest

from levela.config import ROOT

REPO = ROOT.parent
MAKEFILE = ROOT / "Makefile"
SCRIPTS = ROOT / "scripts"
DESIGN_V2 = (ROOT / "docs" / "DESIGN_V2.md").read_text(encoding="utf-8")
REQUIRED_TARGETS = {"setup-db", "seed", "api", "worker", "order_systems", "supervisor", "test", "test-slow", "reset",
                    "demo", "demo-4", "dryrun", "preflight", "provision", "kill-demo", "long-run"}
# Variables set on the command line in the second pass over the recipes.
SET_VARIABLES = {"RECALL": "RCL-TEST-1", "SPEED": "15"}


def _load_script(name):
    """Import scripts/<name>.py as a module. It is registered first, as an
    import would be: dataclasses look their module up while being built."""
    module_name = f"levela_scripts_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


demo = _load_script("demo")


# ---- the Makefile ---------------------------------------------------------------

def _makefile() -> tuple[str, dict[str, str], dict[str, list[str]]]:
    """(text, `X ?= v` defaults, {target: recipe lines})."""
    text = MAKEFILE.read_text(encoding="utf-8")
    defaults = dict(re.findall(r"^([A-Z_]+) \?= ?(.*)$", text, re.M))
    rules: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        target = re.match(r"^([a-z0-9-]+):", line)
        if target:
            current = target.group(1)
            rules[current] = []
        elif line.startswith("\t") and current:
            rules[current].append(line[1:])
        elif line.strip():
            current = None
    return text, defaults, rules


def _variables(text: str, overrides: dict[str, str]) -> dict[str, str]:
    """What make would substitute: the ?= defaults (python as the interpreter),
    then the `X := $(if $(Y),...)` options derived from them."""
    values = {**dict(re.findall(r"^([A-Z_]+) \?= ?(.*)$", text, re.M)), "PYTHON": "python",
              "MAKEFILE_LIST": "Makefile", **overrides}
    for name, cond, rest in re.findall(r"^([A-Z_]+) := \$\(if \$\(([A-Z_]+)\),(.*)\)$", text, re.M):
        values[name] = _expand(rest, values) if values.get(cond) else ""
    return values


def _expand(line: str, values: dict[str, str]) -> str:
    return re.sub(r"\$\(([A-Z_]+)\)", lambda m: values[m.group(1)], line)


class _Captured(Exception):
    def __init__(self, parser):
        super().__init__("parser captured")
        self.parser = parser


def _parser_of(main) -> argparse.ArgumentParser:
    """The parser a CLI's main() builds, taken at its parse_args call (so
    nothing after parsing runs: no database, no server)."""
    def capture(self, args=None, namespace=None):
        raise _Captured(self)
    with mock.patch.object(argparse.ArgumentParser, "parse_args", capture):
        with pytest.raises(_Captured) as info:
            main([])
    return info.value.parser


def _levela_parser():
    from levela.__main__ import main
    return _parser_of(main)


def _promised_options(usage: str) -> set[str]:
    """Options DESIGN_V2 documents for a command (`python -m levela worker
    [--workers N] [--recall RCL]`), which another engineer may still be adding."""
    found = set()
    for line in re.findall(re.escape(usage) + r"\b[^`\n]*", DESIGN_V2):
        found |= set(re.findall(r"--[a-z][a-z-]*", line))
    return found


def _check_parsed(parser, argv: list[str], usage: str) -> None:
    """The parser accepts argv; an option it does not know must be one the
    contract promises for this command."""
    try:
        _, unknown = parser.parse_known_args(argv)
    except SystemExit:
        pytest.fail(f"`{usage} {' '.join(argv)}` is refused by its parser")
    extra = {a.split("=")[0] for a in unknown if a.startswith("--")}
    missing = extra - _promised_options(usage)
    assert not missing, f"`{usage}` accepts no {sorted(missing)} and DESIGN_V2 promises none"


def _check_command(argv: list[str]) -> None:
    """One recipe command maps to a real CLI entry point and its options."""
    prog, rest = argv[0], argv[1:]
    if prog == "python" and rest[:2] == ["-m", "levela"]:
        _check_parsed(_levela_parser(), rest[2:], f"python -m levela {rest[2]}")
    elif prog == "python" and rest[:2] == ["-m", "seedkit"]:
        from seedkit.__main__ import main as seedkit_main
        _check_parsed(_parser_of(seedkit_main), rest[2:], "python -m seedkit")
    elif prog == "python" and rest[:2] == ["-m", "pytest"]:
        marker = rest[rest.index("-m", 2) + 1]
        assert marker in ("slow", "not slow")
    elif prog == "python" and rest[0].startswith("scripts/"):
        _check_script(rest[0], rest[1:])
    elif prog == "bash":
        script = ROOT / rest[0]
        assert script.is_file(), rest[0]
        text = script.read_text(encoding="utf-8")
        for option in (a for a in rest[1:] if a.startswith("--")):
            assert f"{option})" in text, f"{rest[0]} has no {option} option"
    else:
        assert prog == "awk", f"unexpected command {argv}"


def _check_script(path: str, argv: list[str]) -> None:
    name = path.removeprefix("scripts/").removesuffix(".py")
    if name == "demo":
        demo.parse_args(argv)
    elif (SCRIPTS / f"{name}.py").is_file():
        _check_parsed(_parser_of(_load_script(name).main), argv, f"python {path}")
    else:
        # Another engineer's script, not merged into this branch: the contract
        # must name it. Its options are checked by its parser once it is here.
        assert path in DESIGN_V2, f"{path} is neither on disk nor in DESIGN_V2"


def test_makefile_has_every_target_with_help_and_tab_recipes():
    text, defaults, rules = _makefile()
    assert defaults["PYTHON"] == "python"
    assert REQUIRED_TARGETS <= set(rules)
    phony = set(re.search(r"^\.PHONY:((?:.*\\\n)*.*)$", text, re.M).group(1).replace("\\", " ").split())
    assert set(rules) == phony
    for target in rules:
        assert re.search(rf"^{re.escape(target)}: ## \S", text, re.M), f"{target} has no ## help"
        assert rules[target], f"{target} has no recipe"
    # make needs a tab before each recipe line; a space-indented line is a syntax error.
    assert not re.search(r"^ +\$\(PYTHON\)", text, re.M)


@pytest.mark.parametrize("overrides", [{}, SET_VARIABLES], ids=["defaults", "recall-and-speed-set"])
def test_every_recipe_is_a_real_cli_call(overrides):
    text, _, rules = _makefile()
    values = _variables(text, overrides)
    for target, lines in rules.items():
        for line in lines:
            argv = [a for a in shlex.split(_expand(line.lstrip("@"), values))
                    if not re.match(r"^[A-Z_]+=", a)]                      # VAR=value prefixes
            _check_command(argv)


def test_targets_run_what_the_runbook_says():
    _, _, rules = _makefile()
    assert rules["seed"] == ["$(PYTHON) -m levela load-history"]
    assert "--scenario recall_long --mode demo" in rules["demo"][0]
    assert "--scenario recall_1 --mode demo" in rules["demo-4"][0]
    assert rules["test"] == ['$(PYTHON) -m pytest -q -m "not slow"']
    assert rules["test-slow"] == ["$(PYTHON) -m pytest -q -m slow"]
    assert [ln.split()[1] for ln in rules["long-run"]] == ["scripts/demo.py", "scripts/supervisor.py"]


# ---- shell and PowerShell scripts -------------------------------------------------

def _bash() -> str:
    """A bash that runs (on Windows, Git's; WSL's launcher without a distro does not)."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash on PATH")
    try:
        ok = subprocess.run([bash, "-c", "exit 0"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        pytest.skip(f"{bash} does not run scripts")
    return bash


SHELL_SCRIPTS = sorted(p.name for p in SCRIPTS.glob("*.sh"))


def test_the_scripts_are_there():
    assert {"kill_demo.sh", "start_order_systems.sh"} <= set(SHELL_SCRIPTS)
    for name in ("kill_demo.ps1", "start_order_systems.ps1", "demo.py", "video_shots.md"):
        assert (SCRIPTS / name).is_file(), name


@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_shell_scripts_parse(name):
    res = subprocess.run([_bash(), "-n", str(SCRIPTS / name)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    raw = (SCRIPTS / name).read_bytes()
    assert raw.startswith(b"#!/usr/bin/env bash\n") and b"\r\n" not in raw
    assert b"set -euo pipefail" in raw


def test_powershell_scripts_parse():
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("no PowerShell on PATH")
    paths = ", ".join(f"'{SCRIPTS / n}'" for n in ("kill_demo.ps1", "start_order_systems.ps1"))
    command = (f"$n = 0; foreach ($p in @({paths})) {{ $e = $null; "
               "[void][System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$null, [ref]$e); "
               "$n += $e.Count; $e | ForEach-Object { $_.Message } }; exit $n")
    res = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr


STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG"
case "$*" in
  *kill-arm*) exit "${ARM_EXIT:-0}" ;;
  *"levela count-kill"*) exit "${COUNT_EXIT:-0}" ;;
  *"levela worker"*)
    if [ "$(grep -c 'levela worker' "$STUB_LOG")" -eq 1 ]; then exit "${FIRST_EXIT:-137}"; fi
    exit 0 ;;
esac
exit 3
"""
WORKER_CALL = "-m levela worker --workers 2 --recall RCL-TEST-1"
COUNT_CALL = "-m levela count-kill --exit-code 137 --recall RCL-TEST-1"


def _kill_demo(tmp_path, **env):
    """Run kill_demo.sh with PYTHON pointing at a stub that logs its argv:
    demo.py kill-arm succeeds, the first worker exits FIRST_EXIT (137, the
    failpoint), count-kill exits COUNT_EXIT (0), the restarted worker exits 0."""
    stub, log = tmp_path / "python-stub", tmp_path / "calls.log"
    stub.write_bytes(STUB.encode())
    stub.chmod(0o755)
    run_env = {**os.environ, "PYTHON": stub.as_posix(), "STUB_LOG": log.as_posix(), **env}
    res = subprocess.run([_bash(), "scripts/kill_demo.sh", "--recall-id", "RCL-TEST-1", "--no-release",
                          "--workers", "2"], cwd=ROOT, input="\n", capture_output=True, text=True,
                         env=run_env, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return res, calls


def test_kill_demo_arms_runs_the_worker_counts_the_kill_and_restarts_it_after_exit_137(tmp_path):
    """The kill beat runs without the supervisor, so kill_demo counts the
    failpoint's kill itself (python -m levela count-kill) before the banner,
    and the restarted worker's "restart n" line includes it."""
    res, calls = _kill_demo(tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert calls == ["scripts/demo.py --recall-id RCL-TEST-1 kill-arm --no-release", WORKER_CALL, COUNT_CALL,
                     WORKER_CALL]
    assert "WORKER KILLED (exit 137)" in res.stdout and "Press Enter" in res.stdout


def test_kill_demo_carries_on_when_the_kill_cannot_be_counted(tmp_path):
    res, calls = _kill_demo(tmp_path, COUNT_EXIT="1")
    assert res.returncode == 0, res.stdout + res.stderr
    assert calls[1:] == [WORKER_CALL, COUNT_CALL, WORKER_CALL]
    assert "Could not count the kill" in res.stderr and "WORKER KILLED (exit 137)" in res.stdout


@pytest.mark.parametrize("env, code, ran_worker", [({"FIRST_EXIT": "0"}, 1, True),
                                                   ({"FIRST_EXIT": "3"}, 3, True),
                                                   ({"ARM_EXIT": "2"}, 2, False)],
                         ids=["worker-exited-cleanly", "worker-crashed", "arm-refused"])
def test_kill_demo_stops_when_the_failpoint_did_not_fire(tmp_path, env, code, ran_worker):
    res, calls = _kill_demo(tmp_path, **env)
    assert res.returncode == code
    assert sum("levela worker" in c for c in calls) == (1 if ran_worker else 0)
    assert not any("count-kill" in c for c in calls), "only a failpoint kill (exit 137) is counted"
    assert "WORKER KILLED" not in res.stdout
    if ran_worker:
        assert "not 137" in res.stdout


PS_STUB = """$line = $args -join ' '
Add-Content -Path $env:STUB_LOG -Value $line
function Code($name, $default) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ($v) { return [int]$v }
    return $default
}
if ($line -like '*kill-arm*') { exit (Code 'ARM_EXIT' 0) }
if ($line -like '*levela count-kill*') { exit (Code 'COUNT_EXIT' 0) }
if ($line -like '*levela worker*') {
    $n = @(Get-Content $env:STUB_LOG | Where-Object { $_ -like '*levela worker*' }).Count
    if ($n -eq 1) { exit (Code 'FIRST_EXIT' 137) }
    exit 0
}
exit 3
"""


def _kill_demo_ps1(tmp_path, **env):
    """kill_demo.ps1 with $env:PYTHON pointing at a PowerShell stub script
    that behaves like the bash stub above (`& $Python` runs a .ps1 and
    `exit n` in it sets $LASTEXITCODE)."""
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("no PowerShell on PATH")
    stub, log = tmp_path / "python-stub.ps1", tmp_path / "calls.log"
    stub.write_text(PS_STUB, encoding="utf-8")
    run_env = {**os.environ, "PYTHON": str(stub), "STUB_LOG": str(log), **env}
    res = subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "kill_demo.ps1"),
                          "-RecallId", "RCL-TEST-1", "-NoRelease", "-Workers", "2"], cwd=ROOT, input="\n",
                         capture_output=True, text=True, env=run_env, timeout=120)
    calls = log.read_text(encoding="utf-8-sig").splitlines() if log.exists() else []
    return res, calls


@pytest.mark.parametrize("env, code, calls_after_arm", [
    ({}, 0, [WORKER_CALL, COUNT_CALL, WORKER_CALL]),
    ({"COUNT_EXIT": "1"}, 0, [WORKER_CALL, COUNT_CALL, WORKER_CALL]),
    ({"FIRST_EXIT": "3"}, 3, [WORKER_CALL])], ids=["killed", "count-failed", "worker-crashed"])
def test_kill_demo_ps1_counts_only_the_failpoint_kill_like_the_bash_script(tmp_path, env, code, calls_after_arm):
    res, calls = _kill_demo_ps1(tmp_path, **env)
    assert res.returncode == code, res.stdout + res.stderr
    assert calls == ["scripts/demo.py --recall-id RCL-TEST-1 kill-arm --no-release", *calls_after_arm]
    assert ("WORKER KILLED (exit 137)" in res.stdout) is (code == 0)
    assert ("Could not count the kill" in res.stdout) is ("COUNT_EXIT" in env)


def test_count_kill_counts_a_kill_survived_like_the_supervisor(client, db, settings, monkeypatch, capsys):
    """`python -m levela count-kill` adds one to runs.kills and writes the
    supervisor's own feed line for a worker that died with exit 137; the
    restarted worker's "restart n" reads the new count. Without --recall it
    counts against the newest open run-time recall, as the supervisor does."""
    from levela.__main__ import main
    from levela.db.seed import load_scenario, start_recall

    rid = start_recall(client, db, load_scenario("recall_1"), all_sim=True, env={}, emit=False)["recall_id"]
    db.runs.update_one({"_id": f"run:{rid}"}, {"$set": {"kills": 8}})
    monkeypatch.setenv("ATLAS_URI", settings.atlas_uri)
    monkeypatch.setenv("ATLAS_DB", settings.db_name)

    main(["count-kill", "--recall", rid])
    assert capsys.readouterr().out.strip() == f"{rid}: worker died with exit 137 (9)"
    main(["count-kill"])
    assert db.runs.find_one({"_id": f"run:{rid}"})["kills"] == 10
    lines = list(db.feed.find({"kind": "supervisor"}).sort("at", 1))
    assert [(f["text"], f["system"], f["source"], f["level"], f["recall_id"]) for f in lines] == [
        ("worker died with exit 137 (9)", "supervisor", "supervisor", "warn", rid),
        ("worker died with exit 137 (10)", "supervisor", "supervisor", "warn", rid)]

    with pytest.raises(SystemExit, match="RCL-NOPE has no run"):
        main(["count-kill", "--recall", "RCL-NOPE"])
    db.recalls.update_one({"_id": rid}, {"$set": {"status": "closed", "open_units": 0}})
    with pytest.raises(SystemExit, match="no open run-time recall"):
        main(["count-kill"])


# ---- demo.py against a fake API ------------------------------------------------------

ENV = {"ADMIN_TOKEN": "test-admin-token", "API_BASE_URL": "http://api.test"}
NOW = "2026-11-14T22:00:00+00:00"
LATER = "2026-11-14T22:00:03+00:00"
EARLIER = "2026-11-14T21:00:00+00:00"


def _wall(**over) -> dict:
    state = {
        "now": NOW,
        "recall": {"_id": "RCL-TEST-1", "status": "open", "batch_code": "B-4471", "units_total": 2400,
                   "product": "Granola Bars"},
        "run": {"sim_label": "d6 14:00", "day": 6, "days_total": 10, "seconds_per_sim_hour": 30, "paused": False,
                "parties": {"total": 40, "at_start": 5, "found": 35}, "checked": [31, 40], "steps": 412,
                "kills": 9, "duplicates": 0, "tokens_per_decision": 1850, "model_calls": 3,
                "invariants": {"passed": [True] * 4, "ok": True, "at_label": "d6 14:00"},
                "supervisor": {"paused": False}, "saboteur": {"on": False}},
        "metrics": {"checks_completed": [31, 40], "units_documented": [2115, 2400],
                    "units_recovered": [1340, 2400], "open_units": 285, "disputed_units": 15,
                    "mass_balance": 2400, "units_total": 2400},
        "parties": [{"party_id": "harbour-fire", "hold_for_demo": True, "live_attempt": None}],
        "edges": [], "feed": [], "failpoints": {}, "calls": {"total": 0, "by_party": {}},
        "systems": [{"system": "phone", "attempts": 58, "accepted_claims": 29},
                    {"system": "North orders", "adapter_version": 1, "adapter_source": "builtin",
                     "ops_verified": 24, "ops_flagged": 0, "paused": False}],
        "workers": [], "calls_in_flight": {"real": 0, "simulated": 2},
    }
    state.update(over)
    return state


class FakeApi:
    """The API as demo.py sees it: /wall/state, /admin/status and admin POSTs.
    `on_post[path](body, api)` may change the state and returns the reply."""

    def __init__(self, state, *, workers=(), on_post=None):
        self.state, self.workers = state, list(workers)
        self.on_post = on_post or {}
        self.posts: list[tuple[str, dict]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Admin-Token"] == ENV["ADMIN_TOKEN"]
        path = request.url.path
        if request.method == "GET" and path == "/wall/state":
            return httpx.Response(200, json=self.state)
        if request.method == "GET" and path == "/admin/status":
            return httpx.Response(200, json={"workers": self.workers})
        if request.method == "POST":
            body = json.loads(request.content)
            self.posts.append((path, body))
            return httpx.Response(200, json=self.on_post.get(path, lambda b, api: {})(body, self))
        return httpx.Response(404, json={"detail": f"no route {path}"})


def _demo(api: FakeApi, argv, *, ticks=None):
    lines: list[str] = []
    clock = ticks or count(0, 0.5)
    with httpx.Client(transport=httpx.MockTransport(api.handle), base_url=ENV["API_BASE_URL"]) as http:
        code = demo.main(argv, http=http, env=ENV, out=lines.append, sleep=lambda s: None,
                         clock=lambda: next(clock))
    return code, "\n".join(lines)


def _line(kind, at=LATER, **kw) -> dict:
    return {"at": at, "kind": kind, "text": kind, "level": "info", **kw}


def test_close_beat_presses_close_and_sees_the_refusal():
    def close(body, api):
        api.state["feed"] = [_line("close_refused")]
        return {"closed": False, "open_units": 285}
    api = FakeApi(_wall(), on_post={"/admin/close": close})
    code, out = _demo(api, ["close"])
    assert code == 0, out
    assert api.posts == [("/admin/close", {"recall_id": "RCL-TEST-1"})]
    assert "ok       Atlas refused the close" in out and "close refused: 285 open units" in out
    assert 'Say: "Fifteen panels are unexplained' in out


def test_a_line_from_before_the_beat_does_not_count():
    api = FakeApi(_wall(feed=[_line("close_refused", at=EARLIER)]),
                  on_post={"/admin/close": lambda b, a: {"closed": False, "open_units": 285}})
    code, out = _demo(api, ["close"], ticks=count(0, 100))       # one poll, then out of time
    assert code == 1
    assert "MISSING  Atlas refused the close" in out


def test_check_only_does_not_act_and_counts_what_is_on_the_wall():
    api = FakeApi(_wall(feed=[_line("close_refused", at=EARLIER)]))
    code, out = _demo(api, ["--check-only", "close"])
    assert code == 0, out
    assert api.posts == []


def test_judge_pauses_kills_and_releases_harbour_fire_and_lakeside_only():
    def release(body, api):
        if body["party_id"] == "harbour-fire":
            api.state["parties"] = [{"party_id": "harbour-fire", "hold_for_demo": False,
                                    "live_attempt": {"_id": "att_1", "state": "dialed"}}]
            api.state["feed"] = [_line("claim_accepted", party_id="lakeside-systems")]
        return {"unheld": [body["party_id"]]}

    def pause(body, api):
        api.state["run"]["supervisor"]["paused"] = body["paused"]
        return {}
    api = FakeApi(_wall(), on_post={"/admin/release": release, "/admin/supervisor": pause})
    code, out = _demo(api, ["judge"])
    assert code == 0, out
    assert [(p, b.get("party_id"), b.get("paused")) for p, b in api.posts] == [
        ("/admin/supervisor", None, True), ("/admin/release", "harbour-fire", None),
        ("/admin/release", "lakeside-systems", None)]


def test_kill_arm_refuses_while_a_worker_is_alive():
    api = FakeApi(_wall(), workers=[{"name": "laptop", "alive": True, "heartbeat_age_s": 0.4}])
    code, out = _demo(api, ["kill-arm"])
    assert code == 2 and api.posts == []
    assert "Stop the supervisor first" in out


@pytest.mark.parametrize("argv, released", [(["kill-arm"], ["corner-properties", "queen-st-alarm"]),
                                            (["kill-arm", "--no-release"], ["corner-properties"])])
def test_kill_arm_arms_before_it_releases(argv, released):
    def arm(body, api):
        api.state["failpoints"] = {body["name"]: {"party_id": body["party_id"]}}
        return {}
    api = FakeApi(_wall(), workers=[{"name": "old", "alive": False}], on_post={"/admin/failpoint": arm})
    code, out = _demo(api, argv)
    assert code == 0, out
    assert api.posts[0] == ("/admin/failpoint", {"name": "after_dial_request", "party_id": "corner-properties"})
    assert [b["party_id"] for p, b in api.posts[1:]] == released
    assert all(p == "/admin/release" for p, _ in api.posts[1:])


def test_kill_evidence_must_follow_the_failpoint():
    """A resume line from an earlier restart proves nothing about this kill."""
    resumed, adopted = demo.after_fire("worker_resumed"), demo.after_fire("attempt_adopted", party_id="corner-properties")
    fired = _line("failpoint_fired", at=NOW, party_id="corner-properties")
    before = demo.View(_wall(), [_line("worker_resumed", at=EARLIER), fired])
    assert not resumed(before, before)
    after = demo.View(_wall(), [fired, _line("worker_resumed", at=LATER),
                                _line("attempt_adopted", at=LATER, party_id="corner-properties")])
    assert resumed(after, after) and adopted(after, after)
    assert not resumed(demo.View(_wall(), [_line("worker_resumed")]), before)     # never fired


def test_memory_beat_needs_an_adapter_from_an_episode_and_no_model_call():
    def switch(body, api):
        api.state["feed"] = [_line("order_system_switched", system="East orders"),
                             _line("order_system_contract_failed", system="East orders")]
        api.state["systems"] = [{"system": "East orders", "adapter_version": 2, "adapter_source": "episode ep-1",
                                 "ops_flagged": 0, "paused": False}]
        return {}
    api = FakeApi(_wall(), on_post={"/admin/order-system": switch})
    code, out = _demo(api, ["memory"])
    assert code == 0, out
    assert api.posts == [("/admin/order-system", {"order_system": "east", "action": "switch_v2"})]

    def switch_with_model(body, api):
        switch(body, api)
        api.state["run"]["model_calls"] += 1
        return {}
    api = FakeApi(_wall(), on_post={"/admin/order-system": switch_with_model})
    code, out = _demo(api, ["memory"], ticks=count(0, 100))
    assert code == 1 and "MISSING  no model call for the second repair" in out


def test_start_posts_the_recall_and_prints_the_forms():
    def started(body, api):
        return {"recall_id": "RCL-2026-020", "mode": "demo", "options": {},
                "run": {"sim_start_label": "d0 09:00", "seconds_per_sim_hour": 15, "order_system_change_at": {}},
                "parties": [{"party_id": "harbour-fire", "channel": "voice", "form_url": "https://x/form/t"}]}
    api = FakeApi(_wall(), on_post={"/admin/recalls": started})
    code, out = _demo(api, ["start", "--speed", "15", "--sim-party", "corner-properties"])
    assert code == 0, out
    assert api.posts == [("/admin/recalls", {"scenario": "recall_long", "mode": "demo", "all_sim": False,
                                             "seconds_per_sim_hour": 15.0, "sim_parties": ["corner-properties"]})]
    assert "started RCL-2026-020" in out and "by hand" in out and "https://x/form/t" in out


def test_status_shows_the_numbers_the_wall_shows():
    lines = "\n".join(demo.status_lines(_wall()))
    for text in ("Parties 40 (5 at the start, 35 found)", "Kills survived 9", "Duplicate effects 0",
                 "Invariants: 4 of 4 hold", "Documented 2,115 of 2,400", "North orders", "Steps 412"):
        assert text in lines, text
    assert "No run-time recall" in demo.status_lines({"recall": None})[0]


def test_unreachable_api_is_exit_2_with_a_hint():
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    lines: list[str] = []
    with httpx.Client(transport=httpx.MockTransport(down), base_url=ENV["API_BASE_URL"]) as http:
        code = demo.main(["status"], http=http, env=ENV, out=lines.append)
    assert code == 2 and "did not answer" in lines[-1] and "make api" in lines[-1]


def test_a_missed_poll_does_not_end_the_beat():
    """A hosted API waking up answers 503 once: the watch says so and keeps going."""
    api = FakeApi(_wall(feed=[_line("close_refused")]))
    hiccups = iter([httpx.Response(503, json={"detail": "waking up"})])

    def flaky(request):
        reply = next(hiccups, None)
        return reply if reply is not None else api.handle(request)
    lines: list[str] = []
    ticks = count(0, 1)
    with httpx.Client(transport=httpx.MockTransport(flaky), base_url=ENV["API_BASE_URL"]) as http:
        ok = demo.watch(demo.Api(http, ENV["ADMIN_TOKEN"]), "RCL-TEST-1", demo.BEAT_BY_COMMAND["close"].expect[:1],
                        demo.View(_wall()), since=None, timeout_s=5, out=lines.append, sleep=lambda s: None,
                        clock=lambda: next(ticks))
    assert ok
    assert "did not answer" in lines[0] and "HTTP 503" in lines[0]
    assert lines[1] == "  ok       Atlas refused the close"


def test_every_beat_and_extra_has_evidence_to_check():
    commands = [b.command for b in demo.BEATS]
    assert commands == ["run", "judge", "caught", "kill-arm", "kill", "order_system", "memory", "close", "wrap"]
    assert [b.at for b in demo.BEATS] == ["0:00", "0:15", "0:35", "1:00", "1:05", "1:45", "2:20", "2:35", "2:50"]
    for beat in demo.BEATS + demo.EXTRAS:
        assert beat.expect and beat.room
        demo.parse_args([beat.command] + (["text"] if beat.command == "question" else []))


# ---- CI, deploy and the repository's attributes ------------------------------------------

def _yaml(path) -> dict:
    """PyYAML comes with uvicorn[standard]; skip rather than fail without it."""
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _workflow() -> dict:
    return _yaml(REPO / ".github" / "workflows" / "ci.yml")


def test_ci_runs_both_pythons_on_a_mongodb_replica_set():
    wf = _workflow()
    triggers = wf.get("on", wf.get(True))          # YAML 1.1 reads a bare `on` key as true
    assert {"push", "pull_request"} <= set(triggers)
    job = wf["jobs"]["test"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
    assert job["defaults"]["run"]["working-directory"] == "level-a"
    assert job["env"]["LEVELA_TEST_URI"] == "mongodb://localhost:27017/?replicaSet=rs0"
    steps = job["steps"]
    mongo = next(s for s in steps if str(s.get("uses", "")).startswith("supercharge/mongodb-github-action"))
    assert mongo["uses"] == "supercharge/mongodb-github-action@1.12.0"
    assert mongo["with"] == {"mongodb-version": "8.0", "mongodb-replica-set": "rs0"}
    runs = {s.get("name"): s for s in steps if "run" in s}
    commands = "\n".join(s["run"] for s in runs.values())
    assert "pip install -r requirements-dev.txt" in commands
    assert 'python -m seedkit --uri "$LEVELA_TEST_URI" dryrun' in commands
    assert 'python -m pytest -q -m "not slow"' in commands
    slow = next(s for s in runs.values() if "-m slow" in s["run"])
    assert slow["timeout-minutes"] == 20
    assert any(str(s.get("uses", "")).startswith("actions/setup-node") for s in steps)
    assert "node --check levela/wall/wall.js" in commands
    assert (ROOT / "requirements-dev.txt").is_file() and (ROOT / "levela" / "wall" / "wall.js").is_file()


def _env_example_keys() -> tuple[set[str], set[str]]:
    """(keys set in .env.example, every key named in it, commented or not)."""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M)), set(re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", text))


SECRETS = {"ATLAS_URI", "TOOL_SECRET", "ELEVEN_WEBHOOK_SECRET", "ADMIN_TOKEN", "ELEVEN_API_KEY", "TWILIO_SID",
           "TWILIO_TOKEN", "EMBED_API_KEY", "ANTHROPIC_API_KEY", "PHONE_HARBOUR", "PHONE_CORNER"}
LAPTOP_ONLY = {"ELEVEN_WEBHOOK_ID", "LEVELA_TEST_URI"}


def test_env_example_names_every_setting_and_holds_no_secret():
    set_keys, named = _env_example_keys()
    v2 = {"ORDER_NORTH_URL", "ORDER_EAST_URL", "PLANNER", "PLANNER_MODEL", "ANTHROPIC_API_KEY", "REPAIR_MODEL",
          "SCHEDULER_ENABLED", "LEVELA_TEST_URI"}
    assert v2 <= set_keys
    read_by_config = set(re.findall(r'\bg\("([A-Z][A-Z0-9_]*)"\)', (ROOT / "levela" / "config.py").read_text()))
    assert read_by_config <= named, f"config.py reads {sorted(read_by_config - named)}, .env.example omits them"
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", (ROOT / ".env.example").read_text(), re.M))
    for key in SECRETS - {"ATLAS_URI"}:
        assert values.get(key, "") == "", f"{key} must be empty in .env.example"
    assert "USER:PASSWORD@CLUSTER" in values["ATLAS_URI"]
    # The public development secrets are an explicit, laptop-only opt-in:
    # documented, never switched on by copying the example.
    assert "LEVELA_DEV" in named and "LEVELA_DEV" not in set_keys


def test_no_host_config_opts_into_the_public_dev_secrets():
    blueprint = _yaml(ROOT / "deploy" / "render.yaml")
    assert "LEVELA_DEV" not in {e["key"] for svc in blueprint["services"] for e in svc["envVars"]}
    assert "LEVELA_DEV" not in tomllib.loads((ROOT / "deploy" / "fly.toml").read_text(encoding="utf-8"))["env"]
    assert "LEVELA_DEV" not in (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")


def test_provisioning_refuses_the_public_dev_tool_secret():
    """LEVELA_DEV=1 fills an empty TOOL_SECRET with a public value; that value
    must never be handed to ElevenLabs as the tools' secret."""
    from levela.config import Settings

    provision = _load_script("provision_agent")
    dev = Settings.from_env(env={"LEVELA_DEV": "1", "ELEVEN_API_KEY": "k"})
    errors, _ = provision.check_inputs(dev, "https://api.example.test", dry_run=False)
    assert any(e.startswith("TOOL_SECRET is not set") for e in errors)
    real = Settings.from_env(env={"LEVELA_DEV": "1", "ELEVEN_API_KEY": "k", "TOOL_SECRET": "a-real-secret-0123"})
    assert provision.check_inputs(real, "https://api.example.test", dry_run=False) == ([], [])


def test_render_blueprint_runs_the_api_with_every_setting():
    blueprint = _yaml(ROOT / "deploy" / "render.yaml")
    (svc,) = blueprint["services"]
    assert svc["type"] == "web" and svc["rootDir"] == "level-a"
    assert svc["buildCommand"] == "pip install -r requirements.txt"
    assert svc["startCommand"] == "python -m levela api --host 0.0.0.0 --port $PORT"
    assert svc["healthCheckPath"] == "/healthz"
    env = {e["key"]: e for e in svc["envVars"]}
    set_keys, _ = _env_example_keys()
    assert set_keys - LAPTOP_ONLY <= set(env), f"missing {sorted(set_keys - LAPTOP_ONLY - set(env))}"
    assert not LAPTOP_ONLY & set(env)
    for key in SECRETS | {"PUBLIC_BASE_URL", "ORDER_NORTH_URL", "ORDER_EAST_URL"}:
        assert env[key].get("sync") is False and "value" not in env[key], key
    # The API's own simulator posts back to it, on the port the API listens on.
    assert env["PORT"]["value"] == "8000" and env["API_BASE_URL"]["value"] == "http://127.0.0.1:8000"
    text = (ROOT / "deploy" / "render.yaml").read_text(encoding="utf-8")
    assert "sleeps" in text and "laptop" in text


def test_fly_config_keeps_one_api_machine_on_the_health_check():
    fly = tomllib.loads((ROOT / "deploy" / "fly.toml").read_text(encoding="utf-8"))
    assert fly["app"] and fly["primary_region"]
    assert (ROOT / "deploy" / fly["build"]["dockerfile"]).is_file()
    svc = fly["http_service"]
    assert svc["internal_port"] == int(fly["env"]["PORT"]) == 8000
    assert svc["min_machines_running"] == 1 and svc["auto_stop_machines"] == "off"
    assert svc["checks"][0]["path"] == "/healthz"
    assert not SECRETS & set(fly["env"]), "secrets go in `fly secrets set`, not fly.toml"


def test_dockerfile_runs_the_api_as_a_non_root_user_without_secrets():
    text = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM python:3\.12-slim$", text, re.M)
    user = re.findall(r"^USER (\S+)$", text, re.M)
    assert user and user[-1] not in ("root", "0")
    code = re.sub(r"#.*", "", text)
    assert not re.search(r"^COPY \. ", code, re.M) and not re.search(r"\.env(?![a-z])", code)
    for source in re.findall(r"^COPY (\S+) ", text, re.M):
        assert (ROOT / source).exists(), source
    assert "python -m levela api --host 0.0.0.0" in text and "/healthz" in text
    ignore = (ROOT / "deploy" / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore


def test_gitattributes_keeps_scripts_lf_and_the_history_still_verifies():
    rules = (REPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
    for line in ("* text=auto", "*.sh text eol=lf", "Makefile text eol=lf", "*.py text eol=lf",
                 "*.json text eol=lf", "*.pdf binary"):
        assert line in rules, line
    from seedkit.load import DATA_DIR, digest, read_history
    manifest, docs = read_history()                   # refuses files that do not match the manifest
    assert sum(len(v) for v in docs.values()) == sum(manifest["counts"].values())
    lf = (DATA_DIR / "parties.json").read_bytes().replace(b"\r\n", b"\n")
    assert digest(lf) == digest(lf.replace(b"\n", b"\r\n")) == manifest["sha256"]["parties"]
