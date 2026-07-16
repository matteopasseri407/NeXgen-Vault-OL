"""Windows doctor checks that must not disappear behind POSIX-only skips."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
DOCTOR = REPO / "03-INFRA" / "scripts" / "agent-doctor.ps1"


def test_windows_doctor_resolves_engine_owned_helpers_from_its_own_checkout():
    source = DOCTOR.read_text(encoding="utf-8")

    assert '$EngineInfra = Split-Path -Parent $PSScriptRoot' in source
    assert '$RenderPy = Join-Path $EngineInfra "agent-universal-layer\\mcp\\render.py"' in source
    assert '$skillsSyncScript = Join-Path $PSScriptRoot "skills-sync.py"' in source
    assert 'Join-Path $Layer "mcp\\render.py"' not in source
    assert '$Layer\\mcp\\render.py' not in source
    assert '$renderOut = python $RenderPy' in source
    assert source.count('$RenderPy = Join-Path $EngineInfra') == 1
    assert 'Join-Path $Vault "03-INFRA\\scripts\\skills-sync.py"' not in source
    assert '[IO.File]::ReadAllText($AgGlobal)' in source
    assert '(Get-Item -LiteralPath $AgGlobal).Length' not in source


def test_windows_doctor_surfaces_path_limit_and_legacy_skill_migration():
    source = DOCTOR.read_text(encoding="utf-8")

    assert "8191-character inherited-variable limit" in source
    assert "--migrate-legacy" in source
    assert "legacy eager skill view(s) await explicit quarantine" in source


@pytest.mark.skipif(os.name != "nt", reason="PowerShell parser check is Windows-only.")
def test_windows_doctor_parses_in_windows_powershell():
    command = (
        "[void][scriptblock]::Create([IO.File]::ReadAllText("
        + repr(str(DOCTOR))
        + "))"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
