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


def test_config_ownership_guards_are_present_in_both_twins():
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")
    for content in (bash, powershell):
        assert "OpenCode model/provider profile is host-local" in content
        assert "legacy shared OpenCode model/provider profile" in content
        assert "OpenCode loads the canonical AGENTS.md" in content
        assert "Claude security posture" in content
        assert "defaultMode=bypassPermissions" in content
        assert "unmanaged persistent allow rule(s)" in content
