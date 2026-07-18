from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

COUNCIL_PATH = Path(__file__).resolve().parents[1] / "council" / "council.py"
COUNCIL_DIR = COUNCIL_PATH.parent


def load_council():
    if str(COUNCIL_DIR) not in sys.path:
        sys.path.insert(0, str(COUNCIL_DIR))
    module_name = f"council_allow_training_under_test_{id(object())}"
    spec = importlib.util.spec_from_file_location(module_name, COUNCIL_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses in council.py can resolve their module.
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_allow_training_toggle_roundtrip(tmp_path):
    council = load_council()
    pref = tmp_path / "council" / "allow-training.enabled"
    council.COUNCIL_STATE_DIR = pref.parent
    council.ALLOW_TRAINING_PREF_FILE = pref

    # default (no file) = protection on
    assert council._persistent_allow_training() is False
    council.cmd_allow_training(argparse.Namespace(state="on"))
    assert pref.is_file()
    assert council._persistent_allow_training() is True
    council.cmd_allow_training(argparse.Namespace(state="off"))
    assert not pref.exists()
    assert council._persistent_allow_training() is False


def test_persistent_toggle_folds_into_flag(tmp_path):
    council = load_council()
    pref = tmp_path / "on"
    council.ALLOW_TRAINING_PREF_FILE = pref

    # toggle OFF: the per-call flag is left untouched (protection stays on)
    ns = argparse.Namespace(func=council.cmd_brainstorm, allow_training_risk=False)
    council._fold_persistent_allow_training(ns)
    assert ns.allow_training_risk is False

    # toggle ON: it folds into the flag the zero-retention gate reads
    pref.write_text("x", encoding="utf-8")
    ns2 = argparse.Namespace(func=council.cmd_brainstorm, allow_training_risk=False)
    council._fold_persistent_allow_training(ns2)
    assert ns2.allow_training_risk is True

    # the allow-training command itself is never affected by the toggle
    ns3 = argparse.Namespace(func=council.cmd_allow_training, allow_training_risk=False)
    council._fold_persistent_allow_training(ns3)
    assert ns3.allow_training_risk is False
