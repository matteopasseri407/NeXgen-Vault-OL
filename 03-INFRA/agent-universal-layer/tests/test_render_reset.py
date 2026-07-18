"""Onboarding reset (v0.92 slice 5): render.py --reset backs up + removes a
CLI's config; render.py --revert restores it. Reset and revert are a pair."""
from __future__ import annotations

from conftest import load_render_module


def test_reset_backs_up_and_removes_config(sandbox_with_live_configs):
    mod = load_render_module(sandbox_with_live_configs)
    path = mod._cli_config_path("claude")
    original = path.read_text("utf-8")
    assert path.exists()

    rc = mod.cmd_reset("claude")

    assert rc == 0
    assert not path.exists()                                  # removed
    baks = sorted(path.parent.glob(path.name + ".bak-*"))
    assert baks and baks[-1].read_text("utf-8") == original   # backed up first


def test_revert_restores_a_reset_config(sandbox_with_live_configs):
    mod = load_render_module(sandbox_with_live_configs)
    path = mod._cli_config_path("claude")
    original = path.read_text("utf-8")

    mod.cmd_reset("claude")
    assert not path.exists()

    rc = mod.cmd_revert("claude")

    assert rc == 0
    assert path.exists()
    assert path.read_text("utf-8") == original                # fully restored


def test_reset_is_noop_when_config_absent(sandbox):
    mod = load_render_module(sandbox)
    assert mod.cmd_reset("claude") == 0                       # nothing to reset
