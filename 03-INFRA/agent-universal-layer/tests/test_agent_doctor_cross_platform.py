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
        # Gated on real use of Claude on this host (layer-managed
        # settings.json), so a non-Claude user never sees a logged-out FAIL
        # they cannot act on.
        assert "Claude is configured on this host but" in content
    assert 'fail "Claude is not authenticated' in bash
    assert 'bad "Claude is not authenticated' in powershell


def test_doctor_does_not_judge_claude_permission_posture_in_either_twin():
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")
    # 0.91.3 dropped the Claude-only permission judgement: permission posture
    # is a host-local choice, not product policy.
    for content in (bash, powershell):
        assert "Claude security posture" not in content
        assert "bypassPermissions" not in content
        assert "unmanaged persistent allow rule" not in content


def test_config_ownership_guards_are_present_in_both_twins():
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")
    for content in (bash, powershell):
        assert "OpenCode model/provider profile is host-local" in content
        assert "legacy shared OpenCode model/provider profile" in content
        assert "OpenCode loads the canonical AGENTS.md" in content
