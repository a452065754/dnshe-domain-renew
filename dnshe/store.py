"""配置、日志、续期历史的持久化（纯标准库）。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
HISTORY_PATH = DATA_DIR / "history.jsonl"

DEFAULT_CONFIG = {
    "api_key": "",
    "api_secret": "",
    "api_base": "https://api005.dnshe.com/index.php",
    "threshold_days": 30,          # 剩余天数 <= 此值就自动续期（且必须在 180 天窗口内）
    "timeout": 25,
    "web_host": "127.0.0.1",
    "web_port": 8899,
    "notify": {
        "enabled": False,
        "webhook_url": "",         # 企业微信 / 钉钉 / 飞书 机器人地址，或任意接受 JSON POST 的地址
        "kind": "wecom",           # wecom | dingtalk | feishu | raw
        "only_on_change": True,    # 只有续期成功/失败时才推送
    },
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))   # deep copy
    if not CONFIG_PATH.exists():
        try:
            CONFIG_PATH.write_text(
                json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError:
            pass
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user_cfg, dict):
                _deep_merge(cfg, user_cfg)
        except (json.JSONDecodeError, OSError) as e:
            raise SystemExit(f"config.json 解析失败：{e}")
    # 环境变量优先（方便 CI / 计划任务里不落盘密钥）
    env_key = os.environ.get("DNSHE_API_KEY")
    env_secret = os.environ.get("DNSHE_API_SECRET")
    if env_key:
        cfg["api_key"] = env_key.strip()
    if env_secret:
        cfg["api_secret"] = env_secret.strip()
    if os.environ.get("DNSHE_API_BASE"):
        cfg["api_base"] = os.environ["DNSHE_API_BASE"].strip()
    # CI 里推送通知也走环境变量，避免密钥落盘
    if os.environ.get("DNSHE_NOTIFY_WEBHOOK"):
        n = cfg.setdefault("notify", {})
        n["webhook_url"] = os.environ["DNSHE_NOTIFY_WEBHOOK"].strip()
        n["enabled"] = bool(n["webhook_url"])
    if os.environ.get("DNSHE_NOTIFY_KIND"):
        cfg.setdefault("notify", {})["kind"] = os.environ["DNSHE_NOTIFY_KIND"].strip()
    if os.environ.get("DNSHE_THRESHOLD_DAYS"):
        try:
            cfg["threshold_days"] = int(os.environ["DNSHE_THRESHOLD_DAYS"])
        except ValueError:
            pass
    return cfg


def _deep_merge(base: dict, patch: dict) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def save_config(cfg: dict) -> None:
    ensure_dirs()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def mask(s: str, keep: int = 4) -> str:
    if not s:
        return "(未配置)"
    if len(s) <= keep * 2:
        return s[:2] + "*" * max(2, len(s) - 2)
    return s[:keep] + "*" * (len(s) - keep * 2) + s[-keep:]


# ------------------------------------------------------------------ 日志
def log_file() -> Path:
    ensure_dirs()
    return LOG_DIR / f"auto-{datetime.now():%Y%m%d}.log"


def log(msg: str, *, echo: bool = True) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    if echo:
        print(line, flush=True)
    try:
        ensure_dirs()
        with log_file().open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ 历史
def append_history(record: dict) -> None:
    ensure_dirs()
    record = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **record}
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_history(limit: int = 50) -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    rows = []
    with HISTORY_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:][::-1]
