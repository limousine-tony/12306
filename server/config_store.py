"""
配置存储 — web_config.json

首次启动时从旧 config.py 迁移默认值；之后 JSON 文件是唯一真源，
Web 前端通过 /api/config 读写。CLI 脚本继续使用 config.py，互不干扰。
"""

import json
import logging
import os
import sys
import threading
from typing import Any, Dict

from .paths import CLI_DIR, CONFIG_FILE as _CONFIG_PATH

logger = logging.getLogger("web12306")

CONFIG_FILE = str(_CONFIG_PATH)

DEFAULTS: Dict[str, Any] = {
    "trip": {
        "train_date": "",
        "from_station": "",
        "to_station": "",
    },
    "preferred_trains": [],
    "seat_types": ["二等座"],
    "passengers": [],           # [{name, id_number}]  id 可为脱敏号
    "monitor": {
        "auto_submit": True,
        "query_interval": 3.0,   # 保守默认（2026 风控环境）
        "fast_interval": 1.0,
        "max_interval": 10.0,
        "backoff_streak": 15,
        "fast_mode_timeout": 5,
        "pause_in_maintenance": True,   # 23:00-06:00 暂停（页面上显示状态）
        "max_retries": 0,               # 0 = 无限
    },
    "sniper": {
        "release_time": "13:15",        # HH:MM
        "pre_warm_seconds": 60,
        "sniper_interval": 0.3,
        "max_attempts": 30,
        "fallback_to_other_trains": False,
    },
    "booking": {
        "dry_run": False,               # True = 验证到 getQueueCount 即停，不真实下单
        "choose_seats": "",             # 座位格子偏好，如 "1A2F"，默认不选
        "seat_detail_type": "000",      # 铺位偏好三位数字，默认不选
        "queue_wait_seconds": 120,
    },
    "notify": {
        "channel": "",                  # serverchan | pushdeer | bark | ""
        "key": "",
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
    },
}


def _migrate_from_legacy() -> Dict[str, Any]:
    """一次性从旧 cli/config.py 迁移（存在且可导入时）"""
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    try:
        cli_dir = str(CLI_DIR)
        if cli_dir not in sys.path:
            sys.path.insert(0, cli_dir)
        import config as legacy  # cli/config.py
        cfg["trip"]["train_date"] = getattr(legacy, "TRAIN_DATE", "")
        cfg["trip"]["from_station"] = getattr(legacy, "FROM_STATION", "")
        cfg["trip"]["to_station"] = getattr(legacy, "TO_STATION", "")
        cfg["preferred_trains"] = list(getattr(legacy, "PREFERRED_TRAINS", []))
        cfg["seat_types"] = list(getattr(legacy, "SEAT_TYPES", ["二等座"]))
        cfg["passengers"] = [
            {"name": p.get("name", ""), "id_number": p.get("id_number", "")}
            for p in getattr(legacy, "PASSENGERS", [])
        ]
        mon = cfg["monitor"]
        mon["auto_submit"] = bool(getattr(legacy, "AUTO_SUBMIT", True))
        mon["query_interval"] = max(3.0, float(getattr(legacy, "QUERY_INTERVAL", 3.0)))
        mon["fast_interval"] = max(0.8, float(getattr(legacy, "FAST_INTERVAL", 1.0)))
        mon["max_interval"] = float(getattr(legacy, "MAX_INTERVAL", 10.0))
        mon["backoff_streak"] = int(getattr(legacy, "BACKOFF_STREAK", 15))
        mon["fast_mode_timeout"] = int(getattr(legacy, "FAST_MODE_TIMEOUT", 5))
        mon["max_retries"] = int(getattr(legacy, "MAX_RETRIES", 0))
        sn = getattr(legacy, "SNIPER_CONFIG", {}) or {}
        snp = cfg["sniper"]
        snp["release_time"] = sn.get("RELEASE_TIME", snp["release_time"])
        snp["pre_warm_seconds"] = sn.get("PRE_WARM_SECONDS", snp["pre_warm_seconds"])
        snp["sniper_interval"] = sn.get("SNIPER_INTERVAL", snp["sniper_interval"])
        snp["max_attempts"] = sn.get("MAX_TICKET_ATTEMPTS", snp["max_attempts"])
        snp["fallback_to_other_trains"] = sn.get(
            "FALLBACK_TO_OTHER_TRAINS", snp["fallback_to_other_trains"])
        cfg["notify"]["channel"] = getattr(legacy, "PUSH_NOTIFY", "") or ""
        cfg["notify"]["key"] = getattr(legacy, "PUSH_KEY", "") or ""
        logger.info("已从 config.py 迁移默认配置")
    except Exception as e:
        logger.info(f"未迁移旧配置（{e}），使用内置默认值")
    return cfg


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class ConfigStore:
    """线程安全的 JSON 配置读写"""

    def __init__(self, path: str = CONFIG_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return _deep_merge(json.loads(json.dumps(DEFAULTS)), json.load(f))
            except Exception as e:
                logger.error(f"读取 {self.path} 失败（{e}），重建默认配置")
        data = _migrate_from_legacy()
        self._write(data)
        return data

    def _write(self, data: Dict[str, Any]):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._data = _deep_merge(self._data, patch)
            # 安全下限：防止前端传入过激轮询间隔触发风控
            mon = self._data.get("monitor", {})
            mon["query_interval"] = max(1.0, float(mon.get("query_interval", 3.0)))
            mon["fast_interval"] = max(0.5, float(mon.get("fast_interval", 1.0)))
            snp = self._data.get("sniper", {})
            snp["sniper_interval"] = max(0.2, float(snp.get("sniper_interval", 0.3)))
            self._write(self._data)
            return json.loads(json.dumps(self._data))


store = ConfigStore()
