"""统一读取网页 JSON 和旧版 INI 路径映射。"""

import ast
import json
import re
from typing import Dict


def parse_path_mappings(value) -> Dict[str, str]:
    """JSON 优先；兼容旧的冒号、逗号列表及旧写入器的字典字符串。"""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        if text.startswith(("{", "[")):
            try:
                value = json.loads(text)
            except ValueError:
                try:
                    value = ast.literal_eval(text)
                except (ValueError, SyntaxError):
                    raise ValueError("路径映射格式无效") from None
        else:
            result = {}
            for entry in text.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                # Windows 源路径的盘符冒号不是映射分隔符。
                start = 2 if re.match(r"^[A-Za-z]:[\\/]", entry) else 0
                separator = entry.find(":", start)
                if separator < 0:
                    raise ValueError("路径映射需要源路径和目标路径")
                result[entry[:separator].strip()] = entry[separator + 1 :].strip()
            value = result
    if isinstance(value, list):
        result = {}
        for entry in value:
            result.update(parse_path_mappings(entry))
        value = result
    if not isinstance(value, dict) or any(
        not isinstance(source, str)
        or not source.strip()
        or not isinstance(target, str)
        or not target.strip()
        for source, target in value.items()
    ):
        raise ValueError("路径映射必须是源路径到目标路径的非空字符串对象")
    return dict(value)
