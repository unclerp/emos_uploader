"""校验并归一化 INI 配置，数据库仅提供 LLM 提供商快照。"""

from copy import deepcopy
import json
from typing import Dict, Any

from .config_loader import DEFAULT_CONFIG
from .path_mappings import parse_path_mappings
from .manual_rule_engine import ManualRuleEngine


def boolean(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise ValueError("布尔值必须为 true 或 false")


def normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(config)
    for section, values in result.items():
        if not isinstance(values, dict):
            continue
        defaults = DEFAULT_CONFIG.get(section, {})
        for key, value in list(values.items()):
            default = defaults.get(key)
            label = f"{section}.{key}"
            try:
                if isinstance(default, bool):
                    values[key] = boolean(value)
                elif isinstance(default, int):
                    if isinstance(value, bool) or str(value).strip() != str(int(value)):
                        raise ValueError("必须为整数")
                    values[key] = int(value)
                    if (
                        key
                        in (
                            "max_workers",
                            "max_upload_workers",
                            "max_concurrent",
                            "timeout",
                            "poll_interval",
                            "polling_interval",
                            "directory_polling_interval",
                            "chunk_size_mb",
                            "rate_limit_per_sec",
                        )
                        and values[key] < 1
                    ):
                        raise ValueError("必须为正整数")
                    if (
                        key in ("max_workers", "max_upload_workers", "max_concurrent")
                        and values[key] > 128
                    ):
                        raise ValueError("并发数不能超过 128")
                elif key == "path_mappings":
                    values[key] = parse_path_mappings(value)
                elif isinstance(default, dict):
                    if isinstance(value, str):
                        values[key] = json.loads(value)
                    if not isinstance(values[key], dict):
                        raise ValueError("必须为对象")
                elif (
                    isinstance(default, list)
                    and isinstance(value, str)
                    and key != "rules"
                ):
                    values[key] = [
                        part.strip() for part in value.split(",") if part.strip()
                    ]
            except (ValueError, TypeError):
                raise ValueError(f"配置项 {label} 的值或范围无效") from None
    if result.get("naming"):
        naming_rules = result.setdefault("naming_rules", {})
        for key, value in result["naming"].items():
            naming_rules[key[:-7] if key.endswith("_format") else key] = value
    proc = result.setdefault("processing", {})
    raw = proc.get("upload_targets", "emos")
    aliases = {"both": "emos,p123", "all": "emos,p123,cloud189,yun139"}
    raw = aliases.get(str(raw).strip(), raw)
    targets = raw if isinstance(raw, list) else str(raw).split(",")
    targets = [str(t).strip() for t in targets if str(t).strip()]
    if not targets or set(targets) - {"emos", "p123", "cloud189", "yun139"}:
        raise ValueError("上传目标必须为 emos、p123、cloud189、yun139，且至少选择一个")
    proc["upload_targets"] = ",".join(targets)
    rules = result.get("manual_rules", {}).get("rules", [])
    if not isinstance(rules, list) or any(
        not isinstance(rule, dict)
        or not isinstance(rule.get("rule"), str)
        or not rule["rule"].strip()
        or not isinstance(rule.get("enabled", True), bool)
        for rule in rules
    ):
        raise ValueError("手动规则必须是包含规则文本与启用状态的列表")
    parser = ManualRuleEngine([], enabled=False)
    for rule in rules:
        if rule.get("enabled", True) and not parser._parse_single_rule(
            rule.get("rule", "")
        ):
            raise ValueError("手动规则语法无效")
    for section, values in list(result.items()):
        if section.startswith("downloader.") and isinstance(values, dict):
            for key in ("enabled", "auto_delete"):
                if key in values:
                    values[key] = boolean(values[key])
    if "recycle_clean" in result:
        from .recycle_cleaner.cleaner import parse_time_slots

        parse_time_slots(result["recycle_clean"].get("daily_at", "04:00"))
    return result


def with_llm_snapshot(config):
    """保留数据库优先、INI 回退，仅固定某一配置代使用的提供商列表。"""
    from ..database.config_operations import get_llm_providers

    result = deepcopy(config)
    result["_runtime_llm_providers"] = get_llm_providers()
    return result
