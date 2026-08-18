from __future__ import annotations

import json
import os
import threading
from pathlib import Path

DEFAULT_PLAYIT_CONFIG = {
    "secret": "",
    "enabled": False,
    "setup_complete": False,
    "auto_start": True,
    "auto_install": True,
    "java_endpoint": "",
    "bedrock_endpoint": "",
    "voicechat_endpoint": "",
    "bedrock_port": 19132,
    "voicechat_port": 24454,
}

def playit_config_path(server_dir: str | Path) -> Path:
    root = Path(server_dir)
    return root / ".niksnaks-hosting-playit.json"

# The runtime poll asks for this config every second for every running server, which
# on Windows means a disk read per server per second for a file that changes only
# when its dialog saves it. Parsed configs are kept per path and invalidated by
# mtime, so the steady state costs one stat call.
_cache_lock = threading.Lock()
_config_cache: dict[str, tuple[int, dict]] = {}

def load_playit_config(server_dir: str | Path) -> dict:
    path = playit_config_path(server_dir)
    key = os.path.normcase(os.fspath(path))

    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        with _cache_lock:
            _config_cache.pop(key, None)
        return dict(DEFAULT_PLAYIT_CONFIG)

    with _cache_lock:
        cached = _config_cache.get(key)
        if cached and cached[0] == mtime_ns:
            # Callers may edit what they get, so the cached copy stays private.
            return dict(cached[1])

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return dict(DEFAULT_PLAYIT_CONFIG)

    if not isinstance(data, dict):
        return dict(DEFAULT_PLAYIT_CONFIG)

    cfg = dict(DEFAULT_PLAYIT_CONFIG)
    cfg.update(data)
    cfg["secret"] = str(cfg.get("secret", ""))
    cfg["enabled"] = bool(cfg.get("enabled", False))
    cfg["setup_complete"] = bool(cfg.get("setup_complete", False))
    cfg["auto_start"] = bool(cfg.get("auto_start", True))
    cfg["auto_install"] = bool(cfg.get("auto_install", True))
    cfg["java_endpoint"] = str(cfg.get("java_endpoint", "")).strip()
    cfg["bedrock_endpoint"] = str(cfg.get("bedrock_endpoint", "")).strip()
    cfg["voicechat_endpoint"] = str(cfg.get("voicechat_endpoint", "")).strip()
    try:
        cfg["bedrock_port"] = int(cfg.get("bedrock_port", 19132))
    except Exception:
        cfg["bedrock_port"] = 19132
    try:
        cfg["voicechat_port"] = int(cfg.get("voicechat_port", 24454))
    except Exception:
        cfg["voicechat_port"] = 24454

    with _cache_lock:
        _config_cache[key] = (mtime_ns, dict(cfg))
    return cfg

def save_playit_config(server_dir: str | Path, config: dict) -> bool:
    path = playit_config_path(server_dir)
    payload = dict(DEFAULT_PLAYIT_CONFIG)
    payload.update(config or {})
    payload["secret"] = str(payload.get("secret", ""))
    payload["enabled"] = bool(payload.get("enabled", False))
    payload["setup_complete"] = bool(payload.get("setup_complete", False))
    payload["auto_start"] = bool(payload.get("auto_start", True))
    payload["auto_install"] = bool(payload.get("auto_install", True))
    payload["java_endpoint"] = str(payload.get("java_endpoint", "")).strip()
    payload["bedrock_endpoint"] = str(payload.get("bedrock_endpoint", "")).strip()
    payload["voicechat_endpoint"] = str(payload.get("voicechat_endpoint", "")).strip()
    try:
        payload["bedrock_port"] = int(payload.get("bedrock_port", 19132))
    except Exception:
        payload["bedrock_port"] = 19132
    try:
        payload["voicechat_port"] = int(payload.get("voicechat_port", 24454))
    except Exception:
        payload["voicechat_port"] = 24454

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        # Dropped rather than updated: the next load re-reads and re-validates, and
        # a same-instant rewrite on a coarse-mtime filesystem cannot serve stale data.
        with _cache_lock:
            _config_cache.pop(os.path.normcase(os.fspath(path)), None)
        return True
    except Exception:
        return False
