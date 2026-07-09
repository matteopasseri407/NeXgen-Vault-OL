"""Tests for the read-only lifecycle audit shipped by the public engine."""
from __future__ import annotations

import os
import subprocess
import sys

from conftest import REAL_VAULT


SCRIPT = REAL_VAULT / "03-INFRA" / "scripts" / "vault-lifecycle-audit.py"


def run_audit(vault, extra_env=None):
    env = os.environ.copy()
    env["AGENT_VAULT_DATA"] = str(vault)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--today", "2026-07-09", "--limit", "50"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def write_note(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_lifecycle_audit_has_no_public_private_crm_assumptions(tmp_path):
    vault = tmp_path / "vault"
    write_note(
        vault / "04-NOW" / "custom-records" / "item.md",
        "---\nstatus: submitted\n---\n# Item\n",
    )

    proc = run_audit(vault)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "04-NOW/custom-records/item.md" in proc.stdout
    assert "outside relaxed prefixes" in proc.stdout
    assert "outside CRM" not in proc.stdout


def test_lifecycle_audit_accepts_private_relaxed_prefix_config(tmp_path):
    vault = tmp_path / "vault"
    write_note(
        vault / "04-NOW" / "custom-records" / "item.md",
        "---\nstatus: submitted\n---\n# Item\n",
    )
    write_note(
        vault / "99-INDEX" / "vault-lifecycle-relaxed-prefixes.txt",
        "# local schemas\n04-NOW/custom-records/\n",
    )

    proc = run_audit(vault)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Relaxed prefixes: 04-NOW/custom-records/" in proc.stdout
    assert "submitted\t04-NOW/custom-records/item.md" not in proc.stdout


def test_lifecycle_audit_accepts_private_generated_dir_config(tmp_path):
    vault = tmp_path / "vault"
    write_note(vault / "01-LOCAL" / "artifacts" / "export.md", "# Generated\n")
    write_note(
        vault / "99-INDEX" / "vault-lifecycle-generated-dirs.txt",
        "# local generated payloads\n01-LOCAL/artifacts\n",
    )

    proc = run_audit(vault)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Generated dirs: 03-INFRA/n8n-backup, 01-LOCAL/artifacts" in proc.stdout
    assert "01-LOCAL/artifacts/export.md" not in proc.stdout
