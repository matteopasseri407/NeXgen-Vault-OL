"""Behavioral tests for vault-groom.sh (the gardener's hand).

The 2026-07-13 follow-up review found the old plan/run split gave no real
confirmation gate: `run` re-derived its own tranche independently of
whatever `plan` had shown, and the only "checkpoint" was which word the
user typed on the command line. This file tests the redesign: a single
guarded flow by default (propose -> show -> require a typed "yes" ->
execute the EXACT approved text), plus `preview` as the pure read-only
variant that never prompts or executes.

.ps1 is not covered here (no pwsh on this runner); mirror any finding here
into vault-groom.ps1 by hand, same caveat as the rest of the Windows twin.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="vault-groom.sh is the POSIX gardener launcher; Windows CI's `bash` "
           "resolves to WSL, which has no distribution installed there and fails "
           "immediately -- vault-groom.ps1 is the untested-here Windows twin.",
)

TESTS_DIR = Path(__file__).resolve().parent
REAL_UL = TESTS_DIR.parent
REAL_VAULT = REAL_UL.parent.parent
GROOM_SH = REAL_VAULT / "03-INFRA" / "scripts" / "vault-groom.sh"

FIXED_TRANCHE = "- archive: old-note.md\n  reason: superseded by new-note.md\n"


def _git(vault, *args):
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True, text=True, check=True,
    )


def _write_stub(bin_dir: Path, name: str, record_path: Path) -> None:
    # A plain Python script with its own shebang, run directly (not piped
    # through `python3 - <<HEREDOC`, which would consume the stub's OWN
    # stdin instead of the caller's real piped prompt).
    #
    # The stub plays BOTH roles the wrapper now invokes in one guarded run:
    # the read-only propose pass (prints a fixed, known tranche so the test
    # can assert its sha256) and the write pass (detected by the presence of
    # "APPROVED TRANCHE" in the prompt it received, either via -p/--prompt
    # argv or piped stdin) -- which performs a REAL git commit in the vault,
    # so vault_groom_audit.py has real history to build a record from.
    stub = bin_dir / name
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        "argv = sys.argv[1:]\n"
        "stdin_data = ''\n"
        "try:\n"
        "    if not sys.stdin.isatty():\n"
        "        stdin_data = sys.stdin.read()\n"
        "except Exception:\n"
        "    pass\n"
        "prompt = stdin_data\n"
        "for flag in ('-p', '--prompt'):\n"
        "    if flag in argv:\n"
        "        prompt = argv[argv.index(flag) + 1]\n"
        "record_path = os.environ['GROOM_TEST_RECORD']\n"
        "try:\n"
        "    with open(record_path) as f:\n"
        "        records = json.load(f)\n"
        "except (FileNotFoundError, json.JSONDecodeError):\n"
        "    records = []\n"
        "records.append({'argv': argv, 'stdin': stdin_data})\n"
        "with open(record_path, 'w') as f:\n"
        "    json.dump(records, f)\n"
        "if 'APPROVED TRANCHE' in prompt:\n"
        "    vault = os.environ['GROOM_TEST_VAULT']\n"
        "    target = os.path.join(vault, 'stub-groomed.md')\n"
        "    with open(target, 'w') as f:\n"
        "        f.write('groomed by stub\\n')\n"
        "    subprocess.run(['git', '-C', vault, 'add', 'stub-groomed.md'], check=True)\n"
        "    subprocess.run(['git', '-C', vault, 'commit', '-m', 'stub: simulated grooming'], check=True)\n"
        "    print('stub-write-output')\n"
        "else:\n"
        "    print(" + repr(FIXED_TRANCHE) + ", end='')\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)


def _write_empty_stub(bin_dir: Path, name: str, record_path: Path) -> None:
    # Propose pass that produces nothing -- used to test the empty-proposal
    # abort path.
    stub = bin_dir / name
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "record_path = os.environ['GROOM_TEST_RECORD']\n"
        "with open(record_path, 'w') as f:\n"
        "    json.dump([{'argv': argv}], f)\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def groom_env(tmp_path, monkeypatch):
    # The real architecture: $VAULT IS the engine checkout (README's own
    # install step clones NeXgen-Engine straight to ~/KnowledgeVault), so
    # vault-groom.sh's relative "03-INFRA/..." paths resolve inside the
    # vault itself. This fixture mirrors that: it seeds the playbook AND a
    # real copy of vault_groom_audit.py (already covered standalone in
    # test_vault_groom_audit.py -- using the real file here gives genuine
    # end-to-end coverage of the wrapper calling it, not just argv shape).
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "03-INFRA" / "scripts").mkdir(parents=True)
    (vault / "03-INFRA" / "vault-grooming-playbook.md").write_text("playbook\n", encoding="utf-8")
    (vault / "03-INFRA" / "scripts" / "vault_groom_audit.py").write_text(
        (REAL_VAULT / "03-INFRA" / "scripts" / "vault_groom_audit.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _git(vault, "init", "-q", "-b", "main")
    _git(vault, "config", "user.email", "nexgen-tests.invalid")
    _git(vault, "config", "user.name", "Test")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-q", "-m", "seed")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "record.json"
    for name in ("claude", "codex", "agy"):
        _write_stub(bin_dir, name, record)

    state_dir = tmp_path / "state"

    env = dict(os.environ)
    env["VAULT"] = str(vault)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["GROOM_TEST_RECORD"] = str(record)
    env["GROOM_TEST_VAULT"] = str(vault)
    env["GROOM_STATE_DIR"] = str(state_dir)
    env["GROOM_NOPUSH"] = "1"  # no remote configured in these fixtures
    return {"vault": vault, "env": env, "record": record, "state_dir": state_dir}


def _run(groom_env, *args, extra_env=None, stdin_input=None):
    env = dict(groom_env["env"])
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(GROOM_SH), *args],
        env=env,
        input=stdin_input,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _records(groom_env):
    return json.loads(groom_env["record"].read_text(encoding="utf-8"))


def test_preview_mode_is_read_only_and_never_prompts(groom_env):
    proc = _run(groom_env, "preview", stdin_input="")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recs = _records(groom_env)
    assert len(recs) == 1, "preview must invoke the runner exactly once (read-only pass)"
    tools = recs[0]["argv"][recs[0]["argv"].index("--allowedTools") + 1:]
    assert "Edit" not in tools and "Write" not in tools
    assert FIXED_TRANCHE.strip() in proc.stdout


def test_guarded_run_declined_does_not_execute_or_commit(groom_env):
    head_before = _git(groom_env["vault"], "rev-parse", "HEAD").stdout.strip()
    proc = _run(groom_env, stdin_input="no\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recs = _records(groom_env)
    assert len(recs) == 1, "declining must never reach the write pass"
    assert "annullato" in (proc.stdout + proc.stderr)
    head_after = _git(groom_env["vault"], "rev-parse", "HEAD").stdout.strip()
    assert head_after == head_before
    assert not groom_env["state_dir"].exists() or not list(groom_env["state_dir"].glob("*.json"))


def test_guarded_run_eof_on_stdin_is_treated_as_declined(groom_env):
    # No real caller ever gets asked for confirmation and simply has no
    # stdin at all (e.g. invoked with </dev/null) -- must fail SAFE, never
    # silently proceed to write.
    head_before = _git(groom_env["vault"], "rev-parse", "HEAD").stdout.strip()
    proc = _run(groom_env, stdin_input="")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recs = _records(groom_env)
    assert len(recs) == 1
    head_after = _git(groom_env["vault"], "rev-parse", "HEAD").stdout.strip()
    assert head_after == head_before


def test_guarded_run_confirmed_executes_exact_approved_tranche(groom_env):
    proc = _run(groom_env, stdin_input="yes\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recs = _records(groom_env)
    assert len(recs) == 2, "confirming must invoke propose, then write"

    propose_tools = recs[0]["argv"][recs[0]["argv"].index("--allowedTools") + 1:]
    assert "Edit" not in propose_tools

    write_argv = recs[1]["argv"]
    write_tools = write_argv[write_argv.index("--allowedTools") + 1:]
    assert "Edit" in write_tools

    write_prompt = write_argv[write_argv.index("-p") + 1]
    assert "APPROVED TRANCHE" in write_prompt
    assert FIXED_TRANCHE.strip() in write_prompt
    # bash's $(cat ...) command substitution strips trailing newlines --
    # match that here rather than hash the fixture text verbatim.
    expected_hash = hashlib.sha256(FIXED_TRANCHE.rstrip("\n").encode()).hexdigest()
    assert expected_hash in write_prompt


def test_guarded_run_confirmed_produces_real_commit_and_audit_record(groom_env):
    proc = _run(groom_env, stdin_input="yes\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    log = _git(groom_env["vault"], "log", "--oneline").stdout
    assert "stub: simulated grooming" in log
    assert "chore(groom): record run" in log

    records = list(groom_env["state_dir"].glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["runner"] == "claude"
    assert record["pushed"] is False
    assert len(record["commits"]) == 1
    assert record["files_touched"] == ["stub-groomed.md"]
    # bash's $(cat ...) command substitution strips trailing newlines --
    # match that here rather than hash the fixture text verbatim.
    expected_hash = hashlib.sha256(FIXED_TRANCHE.rstrip("\n").encode()).hexdigest()
    assert record["tranche_sha256"] == expected_hash

    plan_records = list(groom_env["state_dir"].glob("*-plan.txt"))
    assert len(plan_records) == 1
    assert plan_records[0].read_text(encoding="utf-8").strip() == FIXED_TRANCHE.strip()

    backlog = (groom_env["vault"] / "99-INDEX" / "vault-cleanup-backlog.md").read_text(encoding="utf-8")
    assert "runner=claude" in backlog
    assert "commits=1" in backlog


def test_empty_proposal_aborts_before_any_confirmation_prompt(groom_env, tmp_path):
    empty_record = tmp_path / "empty_record.json"
    for name in ("claude", "codex", "agy"):
        _write_empty_stub(Path(groom_env["env"]["PATH"].split(":")[0]), name, empty_record)
    env = dict(groom_env["env"])
    env["GROOM_TEST_RECORD"] = str(empty_record)

    proc = subprocess.run(
        ["bash", str(GROOM_SH)],
        env=env, input="yes\n", capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 1
    assert "empty proposal" in proc.stderr
    recs = json.loads(empty_record.read_text(encoding="utf-8"))
    assert len(recs) == 1, "an empty proposal must never reach the write pass either"


def test_invalid_mode_rejected_before_any_runner_call(groom_env):
    proc = _run(groom_env, "bogus", stdin_input="")
    assert proc.returncode == 2
    assert "usage:" in proc.stderr
    assert not groom_env["record"].exists()


def test_codex_runner_propose_uses_read_only_sandbox(groom_env):
    proc = _run(groom_env, "preview", extra_env={"GROOM_RUNNER": "codex"}, stdin_input="")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recs = _records(groom_env)
    argv = recs[0]["argv"]
    assert "exec" in argv
    assert "-s" in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert "read-only planning pass" in recs[0]["stdin"]


def test_codex_runner_write_uses_workspace_write_sandbox(groom_env):
    proc = _run(groom_env, extra_env={"GROOM_RUNNER": "codex"}, stdin_input="yes\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recs = _records(groom_env)
    assert len(recs) == 2
    write_argv = recs[1]["argv"]
    assert write_argv[write_argv.index("-s") + 1] == "workspace-write"
    assert "APPROVED TRANCHE" in recs[1]["stdin"]


def test_agy_runner_propose_uses_plan_mode_and_sandbox(groom_env):
    proc = _run(groom_env, "preview", extra_env={"GROOM_RUNNER": "agy"}, stdin_input="")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recs = _records(groom_env)
    argv = recs[0]["argv"]
    assert argv[argv.index("--mode") + 1] == "plan"
    assert "--sandbox" in argv


def test_agy_runner_write_uses_accept_edits_without_sandbox(groom_env):
    proc = _run(groom_env, extra_env={"GROOM_RUNNER": "agy"}, stdin_input="yes\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recs = _records(groom_env)
    write_argv = recs[1]["argv"]
    assert write_argv[write_argv.index("--mode") + 1] == "accept-edits"
    assert "--sandbox" not in write_argv


def test_claude_nopush_blocks_git_push_hard_on_write_pass(groom_env):
    proc = _run(groom_env, extra_env={"GROOM_NOPUSH": "1"}, stdin_input="yes\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recs = _records(groom_env)
    write_argv = recs[1]["argv"]
    assert "--disallowedTools" in write_argv
    disallowed = write_argv[write_argv.index("--disallowedTools") + 1:]
    assert "Bash(git push:*)" in disallowed


def test_opencode_runner_fails_loud_before_any_invocation(groom_env):
    proc = _run(groom_env, "preview", extra_env={"GROOM_RUNNER": "opencode"}, stdin_input="")
    assert proc.returncode == 2
    assert "no per-invocation permission-scoping flag" in proc.stderr
    assert not groom_env["record"].exists()


def test_unknown_runner_rejected(groom_env):
    proc = _run(groom_env, "preview", extra_env={"GROOM_RUNNER": "some-other-cli"}, stdin_input="")
    assert proc.returncode == 2
    assert "unknown GROOM_RUNNER" in proc.stderr
