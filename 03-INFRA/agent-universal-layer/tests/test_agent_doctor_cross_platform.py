"""Cross-platform source-parity guards for the doctor twins."""

from pathlib import Path


def test_claude_authentication_guard_present_in_both_twins():
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")
    for content in (bash, powershell):
        assert "Claude authentication" in content
        assert "claude auth status" in content
        assert "claude auth login" in content
    assert 'fail "Claude is not authenticated' in bash
    assert 'bad "Claude is not authenticated' in powershell
