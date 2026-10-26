"""列表顺序、启停状态以及旧 INI 格式的兼容性。"""

import configparser
from copy import deepcopy
from pathlib import Path

import pytest
from video_organizer.core.config_loader import (
    DEFAULT_CONFIG,
    load_config,
    update_config,
)
from video_organizer.core.effective_config import normalize_config
from video_organizer.core.manual_rule_engine import ManualRuleEngine

RULES = [
    {"rule": "when: Show => {[tmdbid=1399;type=tv]}", "enabled": True},
    {"rule": "when: Show => {[tmdbid=42;type=tv]}", "enabled": False},
    {"rule": "replace: 旧片名 -> 新片名", "enabled": True},
]


def test_disabled_rule_and_order_survive_roundtrip(tmp_path):
    path = tmp_path / "config.ini"
    config = deepcopy(DEFAULT_CONFIG)
    config["manual_rules"].update(enabled=True, rules=deepcopy(RULES))
    update_config(normalize_config(config), str(path))
    loaded = load_config(str(path))
    assert loaded["manual_rules"]["rules"] == RULES
    assert "_rules_json" not in loaded["manual_rules"]
    engine = ManualRuleEngine(loaded["manual_rules"]["rules"])
    result = engine.apply_rules(
        {"_processed_filename": "Show.S01E01.mkv"}, Path("Show.S01E01.mkv")
    )
    assert result["tmdb_id"] == "1399"
    old = configparser.ConfigParser()
    old.read(path, encoding="utf-8")
    assert old["manual_rules"]["rule1"] == RULES[0]["rule"]
    assert old["manual_rules"]["rule2"] == RULES[2]["rule"]
    assert "42" not in " ".join(
        v for k, v in old["manual_rules"].items() if k.startswith("rule")
    )


def test_old_ini_rules_are_imported_without_duplication(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text(
        "[manual_rules]\nenabled=True\nrules=block: 广告\nrule1=replace: 旧 -> 新\n",
        encoding="utf-8",
    )
    config = load_config(str(path))
    original = config["manual_rules"]["rules"]
    assert len(original) == 2 and all(rule["enabled"] for rule in original)
    update_config(config, str(path))
    assert load_config(str(path))["manual_rules"]["rules"] == original


def test_disabled_rules_remain_disabled_after_deleting_and_reordering(tmp_path):
    path = tmp_path / "config.ini"
    config = deepcopy(DEFAULT_CONFIG)
    config["manual_rules"]["rules"] = deepcopy(RULES)
    update_config(config, str(path))
    config = load_config(str(path))
    config["manual_rules"]["rules"] = [RULES[2], RULES[1]]
    update_config(config, str(path))
    assert load_config(str(path))["manual_rules"]["rules"] == [RULES[2], RULES[1]]
    config["manual_rules"]["rules"] = []
    update_config(config, str(path))
    assert load_config(str(path))["manual_rules"]["rules"] == []
    stored = configparser.ConfigParser()
    stored.read(path, encoding="utf-8")
    assert not any(key.startswith("rule") for key in stored["manual_rules"])


@pytest.mark.parametrize(
    "value", ["{}", "invalid", "[{}]", '[{"rule":"block: a","enabled":"false"}]']
)
def test_corrupt_structured_rules_are_rejected(tmp_path, value):
    path = tmp_path / "config.ini"
    path.write_text("[manual_rules]\n_rules_json=" + value + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="手动规则"):
        load_config(str(path))


@pytest.mark.parametrize(
    "rules",
    [
        None,
        ["block: a"],
        [{"rule": "", "enabled": True}],
        [{"rule": "block: a", "enabled": "false"}],
    ],
)
def test_api_rejects_malformed_rule_list(rules):
    config = deepcopy(DEFAULT_CONFIG)
    config["manual_rules"]["rules"] = rules
    with pytest.raises(ValueError, match="手动规则"):
        normalize_config(config)
