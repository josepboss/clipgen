import json
import os
import threading

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
_lock = threading.Lock()

_DEFAULTS = {
    "postiz_api_key": "",
    "postiz_base_url": "",
}


def load_config() -> dict:
    with _lock:
        if not os.path.exists(CONFIG_PATH):
            _write(dict(_DEFAULTS))
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in _DEFAULTS.items():
                data.setdefault(k, v)
            return data
        except (json.JSONDecodeError, OSError):
            return dict(_DEFAULTS)


def save_config(data: dict) -> None:
    merged = load_config()
    merged.update(data)
    with _lock:
        _write(merged)


def update_config(key: str, value: str) -> None:
    cfg = load_config()
    cfg[key] = value
    with _lock:
        _write(cfg)


def is_configured() -> bool:
    cfg = load_config()
    return bool(cfg.get("postiz_api_key", "").strip()) and bool(cfg.get("postiz_base_url", "").strip())


def mask_api_key(key: str) -> str:
    if not key:
        return ""
    visible = key[-4:] if len(key) >= 4 else key
    return "••••••" + visible


def _write(data: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
