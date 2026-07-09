"""B3 tests: engine VERSION/CHANGELOG hygiene, the data-schema migration
framework in agent_sync.py, and agent-doctor's new-version-available check.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess

import pytest

from conftest import REAL_VAULT, load_agent_sync_module, run_agent_doctor

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="agent-doctor.sh is POSIX-only; this file also shells out to git directly.",
)


# ── VERSION / CHANGELOG hygiene ──────────────────────────────────────────

def test_version_file_matches_changelog_top_entry():
    version = (REAL_VAULT / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"VERSION deve essere semver puro, trovato: {version!r}"
    changelog = (REAL_VAULT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.MULTILINE)
    assert m, "CHANGELOG.md deve avere almeno una voce ## [X.Y.Z]"
    assert m.group(1) == version, f"VERSION ({version}) non combacia con la prima voce del CHANGELOG ({m.group(1)})"


# ── data_migrations(): registro vuoto, fresh install ─────────────────────

def test_data_migrations_fresh_install_stamps_baseline_without_touching_data(sandbox):
    mod = load_agent_sync_module(sandbox)
    env = mod.Env()
    schema_file = env.vault_data / "99-INDEX" / "DATA-SCHEMA-VERSION.txt"
    assert not schema_file.exists(), "precondizione: nessun marker su un vault mai sincronizzato prima"

    user_file = env.vault_data / "99-INDEX" / "USER-PROFILE.md"
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text("dati personali utente\n", encoding="utf-8")
    before_hash = hashlib.sha256(user_file.read_bytes()).hexdigest()

    mod.data_migrations(env)

    assert schema_file.is_file()
    assert schema_file.read_text(encoding="utf-8").strip() == str(mod.TARGET_SCHEMA_VERSION)
    assert hashlib.sha256(user_file.read_bytes()).hexdigest() == before_hash, (
        "un fresh install (nessuna migrazione registrata) non deve MAI toccare un file utente esistente"
    )

    before = sandbox.tree_snapshot()
    mod.data_migrations(env)
    after = sandbox.tree_snapshot()
    assert before == after, "un secondo giro a schema gia' aggiornato deve essere un no-op assoluto"


# ── data_migrations(): un passo registrato, con backup pre-scrittura ─────

def test_data_migrations_runs_registered_step_with_backup_and_bumps_schema(sandbox):
    mod = load_agent_sync_module(sandbox)
    env = mod.Env()

    target = env.vault_data / "99-INDEX" / "USER-PROFILE.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old content v1\n", encoding="utf-8")

    def fake_migration(e):
        mod._backup_before_migration(e, [target])
        target.write_text("new content v2\n", encoding="utf-8")
        return [target]

    mod.MIGRATIONS = {1: fake_migration}
    mod.TARGET_SCHEMA_VERSION = 2

    schema_file = env.vault_data / "99-INDEX" / "DATA-SCHEMA-VERSION.txt"
    schema_file.write_text("1\n", encoding="utf-8")

    mod.data_migrations(env)

    assert target.read_text(encoding="utf-8") == "new content v2\n"
    backups = list(target.parent.glob("USER-PROFILE.md.bak-*"))
    assert len(backups) == 1, backups
    assert backups[0].read_text(encoding="utf-8") == "old content v1\n", (
        "il backup deve catturare il contenuto PRIMA della migrazione, non dopo"
    )
    assert schema_file.read_text(encoding="utf-8").strip() == "2"

    before = sandbox.tree_snapshot()
    mod.data_migrations(env)
    after = sandbox.tree_snapshot()
    assert before == after, "a schema gia' al target, un secondo giro non deve ricreare backup ne' rimigrare"


def test_data_migrations_missing_step_stops_without_guessing(sandbox):
    mod = load_agent_sync_module(sandbox)
    env = mod.Env()
    mod.MIGRATIONS = {}
    mod.TARGET_SCHEMA_VERSION = 5
    schema_file = env.vault_data / "99-INDEX" / "DATA-SCHEMA-VERSION.txt"
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text("3\n", encoding="utf-8")

    mod.data_migrations(env)

    assert schema_file.read_text(encoding="utf-8").strip() == "3", (
        "senza un passo registrato per v3->v4 lo schema deve restare fermo a v3, mai avanzare a caso"
    )


# ── agent-doctor: check "nuova versione motore disponibile" (B3) ────────

def _git(*args: str, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        # No @-shaped string here on purpose: it would match the leak-scan's
        # generic email pattern as a false positive (git does not require a
        # real email address for a local test commit).
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "b3 test suite", "GIT_AUTHOR_EMAIL": "b3-test-sandbox-identity",
            "GIT_COMMITTER_NAME": "b3 test suite", "GIT_COMMITTER_EMAIL": "b3-test-sandbox-identity",
        },
    )


def _make_consumer_engine_clone(sandbox, pinned_tag: str):
    """Bare-bones origin with two tagged VERSION bumps + a consumer clone
    checked out (detached) at `pinned_tag`, wired up exactly like a real
    post-cutover machine: ENGINE-PIN.txt matches the checked-out commit."""
    origin = sandbox.home.parent / "engine-origin-usa-e-getta"
    origin.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    _git("add", "VERSION", cwd=origin)
    _git("commit", "-q", "-m", "v0.1.0", cwd=origin)
    _git("tag", "v0.1.0", cwd=origin)
    (origin / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    _git("add", "VERSION", cwd=origin)
    _git("commit", "-q", "-m", "v0.2.0", cwd=origin)
    _git("tag", "v0.2.0", cwd=origin)

    consumer = sandbox.home / ".nexgen-engine"
    subprocess.run(["git", "clone", "-q", str(origin), str(consumer)], check=True, capture_output=True, text=True)
    _git("checkout", "-q", pinned_tag, cwd=consumer)
    (consumer / "03-INFRA").mkdir(parents=True, exist_ok=True)

    pinned_sha = subprocess.run(
        ["git", "-C", str(consumer), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    pin_file = sandbox.vault / "99-INDEX" / "ENGINE-PIN.txt"
    pin_file.parent.mkdir(parents=True, exist_ok=True)
    pin_file.write_text(pinned_sha + "\n", encoding="utf-8")
    return consumer


def test_doctor_warns_new_engine_version_available(sandbox):
    _make_consumer_engine_clone(sandbox, "v0.1.0")
    result = run_agent_doctor(sandbox)
    assert "new engine version available: v0.2.0 (pinned: v0.1.0)" in result.stdout, result.stdout + result.stderr


def test_doctor_ok_when_pinned_at_latest_version(sandbox):
    _make_consumer_engine_clone(sandbox, "v0.2.0")
    result = run_agent_doctor(sandbox)
    assert "consumer engine at the latest released version (v0.2.0)" in result.stdout, result.stdout + result.stderr
    assert "direct push NOT disabled on the consumer engine clone" not in result.stdout, result.stdout + result.stderr


# ── agent-doctor: Codex CLI known-bad-version check ──────────────────────

def _stub_codex_version(sandbox, version_output: str) -> None:
    stub = sandbox.bin_stubs / "codex"
    stub.write_text(f"#!/bin/sh\necho '{version_output}'\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | 0o111)


def test_doctor_fails_on_known_bad_codex_version(sandbox):
    _stub_codex_version(sandbox, "codex-cli 0.143.0")
    result = run_agent_doctor(sandbox)
    assert "Codex CLI 0.143.0 has a known tool-dispatcher regression" in result.stdout, result.stdout + result.stderr


def test_doctor_ok_on_other_codex_version(sandbox):
    _stub_codex_version(sandbox, "codex-cli 0.142.0")
    result = run_agent_doctor(sandbox)
    assert "Codex CLI 0.142.0 (not in the known-bad list)" in result.stdout, result.stdout + result.stderr
