"""
DNSHE API 客户端（纯标准库，零第三方依赖）

依据官方文档 https://github.com/dnshe/DNSHE-FreeDomains/blob/main/docs/api.md（V2.0）

  基址    https://api005.dnshe.com/index.php?m=domain_hub&endpoint=<ep>&action=<act>
  认证    X-API-Key + X-API-Secret 请求头（URL/body 传参已被禁用）
  列表    GET  subdomains/list        （字段 expires_at / never_expires / full_domain）
  续期    POST subdomains/renew       body={"subdomain_id":N}   （官方仅支持 POST/PUT）
  记录    GET  dns_records/list&subdomain_id=N
  密钥    GET  keys/list / POST keys/create / POST keys/regenerate / POST keys/delete
  配额    GET  quota（无 action）

限流：通用鉴权 60 请求/分钟；429 用 details.reset_at 或退避处理。
写操作（尤其 renew）在超时后**不自动重放**，避免重复续期把到期日叠成年限。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

DEFAULT_BASE = "https://api005.dnshe.com/index.php"
RENEW_WINDOW_DAYS = 180          # 官方规则：到期前 180 天开窗
VERSION = "1.1.0"

# 官方字段：expires_at（到期）、never_expires（永久标记）、full_domain（完整域名）
# 其余为旧版本/变体兼容，按优先级排列
_EXPIRES_KEYS = ("expires_at", "expire_at", "expires", "expiration", "expiry",
                 "expired_at", "expire_time", "expire_date", "end_time", "endtime")
_NAME_KEYS = ("full_domain", "domain", "fqdn", "subdomain", "name", "host")
_ID_KEYS = ("id", "subdomain_id", "sid")
_STATUS_KEYS = ("status", "state")
_PERMANENT_KEYS = ("never_expires", "is_permanent", "permanent", "lifetime")

# 这些 error_code 表示「续期窗口未开 / 不允许续」，重试无意义
_TERMINAL_RENEW_CODES = {
    "renewal_not_yet_available", "renewal_window_expired", "renewal_disabled",
    "redemption_period_requires_administrator", "insufficient_balance",
    "subdomain_not_found", "not_found",
}


class DnsheError(Exception):
    """API 调用失败（业务错误、鉴权错误或网络错误）。"""

    def __init__(self, message: str, *, code: str | None = None,
                 status: int | None = None, payload=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.payload = payload or {}

    def __str__(self) -> str:
        bits = [self.message]
        if self.code:
            bits.append(f"code={self.code}")
        if self.status:
            bits.append(f"HTTP {self.status}")
        return " | ".join(bits)


def parse_dt(value) -> datetime | None:
    """把后端返回的时间解析成 datetime。"""
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
            if ts > 1e11:      # 毫秒
                ts /= 1000.0
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ").replace("Z", "").split("+")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _first(raw: dict, keys):
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
    return None


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return False


@dataclass
class Domain:
    """归一化后的一个域名。"""
    id: int | None
    name: str
    status: str
    expires_at: datetime | None
    permanent: bool = False
    root: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    # ---- 派生属性 ----
    @property
    def days_left(self) -> int | None:
        if self.permanent or not self.expires_at:
            return None
        return (self.expires_at - datetime.now()).days

    @property
    def expired(self) -> bool:
        d = self.days_left
        return d is not None and d < 0

    @property
    def in_window(self) -> bool:
        """是否已进入 180 天免费续期窗口。"""
        d = self.days_left
        return d is not None and d <= RENEW_WINDOW_DAYS

    def renewable(self) -> bool:
        if self.permanent or self.id is None:
            return False
        return self.in_window

    @property
    def level(self) -> str:
        """UI 紧急度：permanent/expired/urgent/soon/window/safe/unknown"""
        if self.permanent:
            return "permanent"
        d = self.days_left
        if d is None:
            return "unknown"
        if d < 0:
            return "expired"
        if d <= 7:
            return "urgent"
        if d <= 30:
            return "soon"
        if d <= RENEW_WINDOW_DAYS:
            return "window"
        return "safe"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "root": self.root,
            "status": self.status,
            "expires_at": self.expires_at.strftime("%Y-%m-%d %H:%M:%S") if self.expires_at else None,
            "days_left": self.days_left,
            "permanent": self.permanent,
            "in_window": self.in_window,
            "renewable": self.renewable(),
            "level": self.level,
        }

    @classmethod
    def from_raw(cls, raw: dict) -> "Domain":
        if not isinstance(raw, dict):
            raw = {}
        # 完整域名优先；后端 list 里 subdomain 只是前缀，rootdomain 是后缀
        name = str(_first(raw, _NAME_KEYS) or "")
        root = str(raw.get("rootdomain") or "")
        if name and "." not in name and root:
            name = f"{name}.{root}"
        elif not name and root:
            name = root

        expires = parse_dt(_first(raw, _EXPIRES_KEYS))
        status = str(_first(raw, _STATUS_KEYS) or "").lower()

        # 永久域名只认显式标记（never_expires=1 等），不靠"没有到期时间"猜
        permanent = any(_truthy(raw.get(k)) for k in _PERMANENT_KEYS)

        idv = _first(raw, _ID_KEYS)
        try:
            did = int(idv) if idv is not None else None
        except (TypeError, ValueError):
            did = None
        return cls(id=did, name=name, status=status, expires_at=expires,
                   permanent=permanent, root=root, raw=raw)


class DnsheClient:
    def __init__(self, api_key: str, api_secret: str, *,
                 base: str = DEFAULT_BASE, timeout: int = 25, retries: int = 2,
                 verbose: bool = False):
        if not api_key or not api_secret:
            raise DnsheError("缺少 API 凭据（X-API-Key / X-API-Secret），请先在 config.json 或环境变量里配置")
        self.api_key = api_key
        self.api_secret = api_secret
        self.base = base
        self.timeout = timeout
        self.retries = retries
        self.verbose = verbose
        self._renew_shape: str | None = None   # 首次成功后记住可用形态

    # ------------------------------------------------------------------ 底层
    def _log(self, msg: str):
        if self.verbose:
            print(f"[api] {msg}", flush=True)

    def request(self, endpoint: str, action: str | None, *, method: str = "GET",
                params: dict | None = None, body: dict | None = None,
                retry_on_error: bool = True) -> dict:
        q = {"m": "domain_hub", "endpoint": endpoint}
        if action:
            q["action"] = action
        if params:
            q.update({k: v for k, v in params.items() if v is not None})
        url = f"{self.base}?{urllib.parse.urlencode(q)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "X-API-Key": self.api_key,
            "X-API-Secret": self.api_secret,
            "Accept": "application/json",
            "User-Agent": f"dnshe-renew/{VERSION} (+local)",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        attempts = (self.retries + 1) if retry_on_error else 1
        last_err: Exception | None = None
        for attempt in range(attempts):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            self._log(f"{method} {endpoint}/{action or '-'} (try {attempt + 1})")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return self._parse(resp.read().decode("utf-8", "replace"), resp.status)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                payload = self._safe_json(raw) or {}
                msg = payload.get("message") or payload.get("error") or raw[:200] or str(e)
                err = DnsheError(str(msg), code=payload.get("error_code"),
                                 status=e.code, payload=payload)
                if e.code == 429:
                    # 限流：优先按 reset_at 退避，其次有界指数退避
                    wait = self._rate_limit_wait(payload, attempt)
                    self._log(f"429 限流（{err.code}），等待 {wait:.1f}s")
                    if attempt + 1 < attempts:
                        time.sleep(wait)
                        last_err = err
                        continue
                    raise err from None
                if 400 <= e.code < 500:      # 其余 4xx 不重试
                    raise err from None
                last_err = err
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
                last_err = DnsheError(f"网络错误：{e}")
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
        raise last_err or DnsheError("请求失败")

    @staticmethod
    def _rate_limit_wait(payload: dict, attempt: int) -> float:
        details = payload.get("details") or {}
        reset_at = details.get("reset_at")
        dt = parse_dt(reset_at)
        if dt:
            gap = (dt - datetime.now()).total_seconds()
            if 0 < gap <= 120:
                return gap + 0.5
        return min(30.0, 3.0 * (2 ** attempt))

    @staticmethod
    def _safe_json(raw: str) -> dict | None:
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def _parse(raw: str, status: int) -> dict:
        payload = DnsheClient._safe_json(raw)
        if payload is None:
            raise DnsheError(f"返回非 JSON：{raw[:200]}", status=status)
        if payload.get("success") is False:
            raise DnsheError(
                str(payload.get("message") or payload.get("error") or "API 返回失败"),
                code=payload.get("error_code"), status=status, payload=payload)
        return payload

    # ------------------------------------------------------------------ 域名
    def list_domains(self, *, per_page: int = 200, max_pages: int = 20) -> list[Domain]:
        """拿全部域名，自动翻页（官方 per_page 上限 500，默认 200）。"""
        items: list[dict] = []
        page = 1
        cursor: int | None = None
        while page <= max_pages:
            params: dict = {"per_page": per_page, "fields": "all"}
            if cursor is not None:
                params["cursor_id"] = cursor
            else:
                params["page"] = page
            payload = self.request("subdomains", "list", params=params)
            batch = self._extract_list(payload, ("subdomains", "domains", "data", "items"))
            items.extend(batch)

            pg = payload.get("pagination") or {}
            nxt = pg.get("next_cursor_id")
            if pg.get("has_more") and nxt is not None:
                cursor = nxt
            elif len(batch) >= per_page and not pg:
                page += 1          # 老后端无 pagination 字段时按页数推断
            else:
                break
            if not batch:
                break
        return [Domain.from_raw(it) for it in items if isinstance(it, dict)]

    @staticmethod
    def _extract_list(payload: dict, keys) -> list:
        for key in keys:
            v = payload.get(key)
            if isinstance(v, list):
                return v
        if isinstance(payload.get("data"), dict):
            for k2 in keys:
                v = payload["data"].get(k2)
                if isinstance(v, list):
                    return v
        return []

    def get_domain(self, subdomain_id: int) -> dict:
        return self.request("subdomains", "get", params={"subdomain_id": int(subdomain_id)})

    # ------------------------------------------------------------------ 续期
    def renew(self, subdomain_id: int) -> dict:
        """续期。官方仅支持 POST / PUT。

        只在"方法不支持"时才换形态；遇到窗口未开、无权限、余额不足等直接抛错，
        网络层失败也不重放 —— 续期写操作重放会把到期日叠加成多年。
        """
        shapes = [("POST", "post"), ("PUT", "put")]
        if self._renew_shape:
            shapes.sort(key=lambda s: 0 if s[1] == self._renew_shape else 1)

        last_err: Exception | None = None
        for method, tag in shapes:
            try:
                payload = self.request("subdomains", "renew", method=method,
                                       body={"subdomain_id": int(subdomain_id)},
                                       retry_on_error=False)
                self._renew_shape = tag
                return payload
            except DnsheError as e:
                if e.status in (401, 403) or (e.code in _TERMINAL_RENEW_CODES):
                    raise
                if e.status in (404, 402, 422):
                    raise
                txt = f"{e.message} {e.code or ''}".lower()
                if any(k in txt for k in ("window", "not yet", "too early", "窗口", "未到", "already")):
                    raise
                # 仅当疑似"方法不支持"才换下一个形态
                if e.status in (405, 501) or "method" in txt:
                    last_err = e
                    continue
                raise
        raise last_err or DnsheError("续期失败")

    # ------------------------------------------------------------------ DNS 记录
    def list_dns_records(self, subdomain_id: int) -> list[dict]:
        payload = self.request("dns_records", "list", params={"subdomain_id": int(subdomain_id)})
        return self._extract_list(payload, ("records", "dns_records", "data", "list"))

    # ------------------------------------------------------------------ 密钥/配额
    def list_keys(self) -> list[dict]:
        payload = self.request("keys", "list")
        return self._extract_list(payload, ("keys", "data", "list"))

    def quota(self) -> dict:
        return self.request("quota", None)


# ---------------------------------------------------------------------- 业务规则
def plan_renewals(domains: list[Domain], threshold_days: int = 30) -> list[Domain]:
    """挑出该续期的：已进入 180 天窗口 且 剩余天数 <= 阈值（已过期也算）。"""
    out = []
    for d in domains:
        if not d.renewable():
            continue
        left = d.days_left
        if left is None or left <= threshold_days:
            out.append(d)
    out.sort(key=lambda d: (d.days_left if d.days_left is not None else 9999))
    return out
