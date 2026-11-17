"""本地 Web 管理面板（标准库 http.server，仅监听本机）。"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import store
from .client import RENEW_WINDOW_DAYS, DnsheClient, DnsheError, plan_renewals
from .store import log

INDEX = Path(__file__).resolve().parent / "web" / "index.html"
_CACHE: dict = {"ts": 0.0, "domains": None}
_CACHE_TTL = 20.0   # 秒，避免面板反复刷新把接口打爆
_LOCK = threading.Lock()


def _client(cfg: dict) -> DnsheClient:
    return DnsheClient(cfg.get("api_key", ""), cfg.get("api_secret", ""),
                       base=cfg.get("api_base") or "https://api005.dnshe.com/index.php",
                       timeout=int(cfg.get("timeout") or 25))


def _get_domains(cfg: dict, *, force: bool = False):
    now = time.time()
    if not force and _CACHE["domains"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["domains"]
    domains = _client(cfg).list_domains()
    domains.sort(key=lambda d: (d.days_left if d.days_left is not None else 99999))
    _CACHE.update(ts=now, domains=domains)
    return domains


def _invalidate() -> None:
    _CACHE.update(ts=0.0, domains=None)


class Handler(BaseHTTPRequestHandler):
    server_version = "dnshe-renew"
    cfg: dict = {}

    # ---------------------------------------------------------- 基础
    def log_message(self, fmt, *args):   # 静音默认访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, json.JSONDecodeError, OSError):
            return {}

    # ---------------------------------------------------------- 路由
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                html = INDEX.read_bytes()
            except OSError as e:
                return self._send(500, f"找不到面板文件：{e}".encode(), "text/plain; charset=utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/api/state":
            return self.api_state(force="force=1" in self.path)
        if path == "/favicon.ico":
            return self._send(204, b"")
        self._send(404, b'{"ok":false,"error":"not found"}')

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/renew":
                return self.api_renew(self._read_json())
            if path == "/api/auto":
                return self.api_auto(self._read_json())
            if path == "/api/config":
                return self.api_config(self._read_json())
        except Exception:                      # noqa: BLE001 兜底，面板不能整个崩
            traceback.print_exc()
            return self._json({"ok": False, "error": "服务端异常，详见控制台"}, 500)
        self._send(404, b'{"ok":false,"error":"not found"}')

    # ---------------------------------------------------------- 业务
    def api_state(self, force: bool = False):
        cfg = self.cfg
        threshold = int(cfg.get("threshold_days", 30))
        has_cred = bool(cfg.get("api_key") and cfg.get("api_secret"))
        payload = {
            "ok": True,
            "has_credentials": has_cred,
            "window_days": RENEW_WINDOW_DAYS,
            "history": store.read_history(20),
            "domains": [],
            "stats": {"total": 0, "permanent": 0, "in_window": 0, "due": 0,
                      "threshold": threshold, "most_urgent": None},
            "error": None,
        }
        if not has_cred:
            payload["error"] = "尚未配置 API 凭据，请运行 python dnshe.py config --key … --secret …"
            return self._json(payload)
        try:
            domains = _get_domains(cfg, force=force)
        except DnsheError as e:
            payload["error"] = f"查询失败：{e}"
            return self._json(payload)

        due = plan_renewals(domains, threshold)
        urgent = next((d for d in domains if not d.permanent and d.days_left is not None), None)
        payload["domains"] = [d.to_dict() for d in domains]
        payload["stats"] = {
            "total": len(domains),
            "permanent": sum(1 for d in domains if d.permanent),
            "in_window": sum(1 for d in domains if d.renewable()),
            "due": len(due),
            "threshold": threshold,
            "most_urgent": urgent.to_dict() if urgent else None,
        }
        return self._json(payload)

    def api_renew(self, body: dict):
        cfg = self.cfg
        try:
            domains = _get_domains(cfg, force=True)
        except DnsheError as e:
            return self._json({"ok": False, "error": f"查询失败：{e}"}, 200)

        if body.get("mode") == "window":
            targets = [d for d in domains if d.renewable()]
        else:
            ids = {int(i) for i in (body.get("ids") or []) if str(i).lstrip("-").isdigit()}
            targets = [d for d in domains if d.id in ids]

        if not targets:
            return self._json({"ok": False, "error": "没有匹配到可续期的域名（可能未进入 180 天窗口）"})

        cli = _client(cfg)
        ok = fail = 0
        details = []
        for d in targets:
            try:
                res = cli.renew(d.id)
                ok += 1
                new_exp = res.get("new_expires_at") or ""
                details.append({"name": d.name, "result": "ok", "new": new_exp})
                store.append_history({"action": "renew", "via": "web", "name": d.name,
                                      "id": d.id, "result": "ok",
                                      "previous": res.get("previous_expires_at"),
                                      "new": new_exp})
            except DnsheError as e:
                fail += 1
                details.append({"name": d.name, "result": "fail", "error": str(e)})
                store.append_history({"action": "renew", "via": "web", "name": d.name,
                                      "id": d.id, "result": "fail", "error": str(e)})
        _invalidate()
        msg = f"成功 {ok} 个" + (f"，失败 {fail} 个" if fail else "")
        if ok:
            log(f"[web] 续期 {msg}：" + ", ".join(x["name"] for x in details if x["result"] == "ok"))
        return self._json({"ok": ok > 0 or fail == 0, "ok_count": ok, "fail_count": fail,
                           "message": msg, "details": details})

    def api_auto(self, body: dict):
        cfg = self.cfg
        threshold = int(body.get("threshold") or cfg.get("threshold_days", 30))
        dry = bool(body.get("dry_run"))
        try:
            domains = _get_domains(cfg, force=True)
        except DnsheError as e:
            return self._json({"ok": False, "error": f"查询失败：{e}"}, 200)
        targets = plan_renewals(domains, threshold)
        if not targets:
            return self._json({"ok": True, "ok_count": 0, "fail_count": 0, "details": [],
                               "message": f"没有剩余 ≤ {threshold} 天的域名，无需续期"})
        if dry:
            names = "、".join(f"{d.name}({d.days_left}天)" for d in targets)
            return self._json({"ok": True, "ok_count": 0, "fail_count": 0, "details": [],
                               "message": f"演练：将续期 {len(targets)} 个 —— {names}"})
        return self.api_renew({"ids": [d.id for d in targets]})

    def api_config(self, body: dict):
        cfg = self.cfg
        touched = False
        if "threshold_days" in body:
            try:
                v = max(0, min(RENEW_WINDOW_DAYS, int(body["threshold_days"])))
            except (TypeError, ValueError):
                return self._json({"ok": False, "error": "阈值必须是数字"})
            cfg["threshold_days"] = v
            touched = True
        for k in ("api_key", "api_secret", "api_base"):
            if isinstance(body.get(k), str) and body[k].strip():
                cfg[k] = body[k].strip()
                touched = True
                _invalidate()
        if isinstance(body.get("webhook_url"), str):
            cfg.setdefault("notify", {})["webhook_url"] = body["webhook_url"].strip()
            cfg["notify"]["enabled"] = bool(body["webhook_url"].strip())
            touched = True
        if touched:
            clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
            store.save_config(clean)
        return self._json({"ok": True, "threshold_days": cfg.get("threshold_days")})


def serve(cfg: dict, host: str = "127.0.0.1", port: int = 8899) -> int:
    Handler.cfg = cfg
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"端口 {port} 启动失败：{e}（换一个：python dnshe.py web --port 8900）")
        return 1
    url = f"http://{host}:{port}/"
    print(f"面板已启动：{url}")
    print("按 Ctrl+C 停止。")
    if not os.environ.get("DNSHE_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0
