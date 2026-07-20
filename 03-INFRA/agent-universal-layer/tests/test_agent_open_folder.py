"""Regression coverage for the cross-platform file-manager launcher."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "03-INFRA" / "scripts"


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher behavior is covered on Linux and macOS.")
def test_posix_launcher_rejects_relative_folder_without_opening_a_window():
    launcher = SCRIPTS / "agent-open-folder.sh"
    result = subprocess.run(
        [str(launcher), "relative-folder"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "percorso assoluto" in result.stderr


def test_windows_launcher_uses_explorer_and_rejects_relative_paths():
    launcher = (SCRIPTS / "agent-open-folder.ps1").read_text(encoding="utf-8")

    assert "[System.IO.Path]::IsPathRooted($Path)" in launcher
    assert "Test-Path -LiteralPath $Folder -PathType Container" in launcher
    assert "Start-Process -FilePath 'explorer.exe'" in launcher
