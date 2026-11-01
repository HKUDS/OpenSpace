import json
from pathlib import Path
from typing import Any


def get_config_value(config: Any, key: str, default=None):
    if isinstance(config, dict):
        return config.get(key, default)
    else:
        return getattr(config, key, default)


def load_json_file(filepath: str | Path) -> dict[str, Any]:
    filepath = Path(filepath) if isinstance(filepath, str) else filepath
    
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_json_file(data: dict[str, Any], filepath: str | Path, indent: int = 2) -> None:
    filepath = Path(filepath) if isinstance(filepath, str) else filepath
        
    # Ensure directory exists
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)


__all__ = ["get_config_value", "load_json_file", "save_json_file"]
