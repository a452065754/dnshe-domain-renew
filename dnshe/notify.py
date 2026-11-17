"""Webhook 通知：企业微信 / 钉钉 / 飞书 / 通用 JSON。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .client import VERSION
from .store import log


def _payload(kind: str, text: str, title: str) -> dict:
    kind = (kind or "raw").lower()
    if kind == "wecom":
        return {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{text}"}}
    if kind == "dingtalk":
        return {"msgtype": "text", "text": {"content": f"{title}\n{text}"}}
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": f"{title}\n{text}"}}
    return {"title": title, "content": text, "source": "dnshe-renew"}


def send(webhook_url: str, text: str, *, kind: str = "raw", title: str = "DNSHE 域名续期",
         timeout: int = 15) -> bool:
    if not webhook_url:
        return False
    body = json.dumps(_payload(kind, text, title), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": f"dnshe-renew/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        log(f"通知已发送（HTTP {resp.status}）：{raw[:120]}")
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        log(f"通知发送失败：{e}")
        return False
