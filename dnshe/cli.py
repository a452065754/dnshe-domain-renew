"""DNSHE 域名续期工具 · 命令行入口。

用法速查：
  python dnshe.py list                     查看全部域名与到期情况
  python dnshe.py check                    只看"该续期"的域名（不动手）
  python dnshe.py renew --all              续期所有已进入窗口的域名
  python dnshe.py renew my.de5.net 12      按 域名/ID 续期指定域名
  python dnshe.py auto                     自动续期（阈值内），给计划任务用
  python dnshe.py auto --dry-run           自动续期演练，只打印不执行
  python dnshe.py web                      打开本地管理面板
  python dnshe.py config --key xxx --secret yyy
  python dnshe.py test                     连通性 + 凭据自检
  python dnshe.py history                  最近的续期记录
"""

from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from datetime import datetime

from . import store
from .client import (RENEW_WINDOW_DAYS, DnsheClient, DnsheError, Domain,
                     plan_renewals)

# ------------------------------------------------------------------ 输出工具
# pythonw / 无控制台环境下 sys.stdout 可能是 None，这里必须容错
_COLOR = bool(getattr(sys.stdout, "isatty", lambda: False)()) and os.environ.get("NO_COLOR") is None


def c(text: str, color: str) -> str:
    if not _COLOR:
        return text
    codes = {"red": "31", "green": "32", "yellow": "33", "blue": "36",
             "gray": "90", "bold": "1", "cyan": "36"}
    return f"\033[{codes.get(color, '0')}m{text}\033[0m"


def w(s: str) -> int:
    """终端显示宽度（CJK 算 2 格）。"""
    total = 0
    for ch in str(s):
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def pad(s: str, width: int) -> str:
    s = str(s)
    return s + " " * max(0, width - w(s))


LEVEL_STYLE = {
    "expired": ("red", "已过期"),
    "urgent": ("red", "紧急"),
    "soon": ("yellow", "临近"),
    "window": ("cyan", "窗口内"),
    "safe": ("green", "安全"),
    "permanent": ("blue", "永久"),
    "unknown": ("gray", "未知"),
}


def render_table(domains: list[Domain]) -> str:
    if not domains:
        return "（没有查询到任何域名）"
    headers = ["域名", "状态", "到期时间", "剩余", "续期窗口", "内部ID"]
    rows = []
    for d in domains:
        left = d.days_left
        left_txt = "永久" if d.permanent else (f"{left} 天" if left is not None else "未知")
        color, label = LEVEL_STYLE.get(d.level, ("gray", d.level))
        rows.append([
            d.name or "(无名)",
            d.status or "-",
            d.expires_at.strftime("%Y-%m-%d") if d.expires_at else "-",
            left_txt,
            c(label, color),
            str(d.id) if d.id is not None else "-",
        ])
    widths = [max(w(headers[i]), max((w(r[i]) for r in rows), default=0)) for i in range(len(headers))]
    out = ["  ".join(c(pad(headers[i], widths[i]), "bold") for i in range(len(headers)))]
    out.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for r in rows:
        out.append("  ".join(pad(r[i], widths[i]) for i in range(len(headers))))
    return "\n".join(out)


# ------------------------------------------------------------------ 客户端装配
def build_client(cfg: dict) -> DnsheClient:
    if not cfg.get("api_key") or not cfg.get("api_secret"):
        sys.exit(c("尚未配置 API 凭据。\n"
                   "  方式一：python dnshe.py config --key 你的Key --secret 你的Secret\n"
                   "  方式二：在 config.json 里填 api_key / api_secret\n"
                   "  方式三：设置环境变量 DNSHE_API_KEY / DNSHE_API_SECRET\n"
                   "凭据在 DNSHE 后台 域名页面 → [API 管理] 创建。", "yellow"))
    return DnsheClient(cfg["api_key"], cfg["api_secret"],
                       base=cfg.get("api_base") or "https://api005.dnshe.com/index.php",
                       timeout=int(cfg.get("timeout") or 25),
                       verbose=bool(cfg.get("_verbose")))


def fetch(cfg: dict) -> list[Domain]:
    cli = build_client(cfg)
    domains = cli.list_domains()
    domains.sort(key=lambda d: (d.days_left if d.days_left is not None else 99999))
    return domains


# ------------------------------------------------------------------ 续期动作
def do_renew(cli: DnsheClient, targets: list[Domain], cfg: dict, *,
             dry_run: bool = False, quiet: bool = False) -> dict:
    """对 targets 逐个续期，返回统计。"""
    stats = {"ok": 0, "fail": 0, "skip": 0, "details": []}
    for d in targets:
        if d.id is None:
            stats["skip"] += 1
            continue
        if dry_run:
            if not quiet:
                print(f"  {c('[演练]', 'gray')} 将续期 {d.name}（剩余 {d.days_left} 天）")
            stats["details"].append({"name": d.name, "result": "dry-run"})
            continue
        try:
            res = cli.renew(d.id)
            new_exp = res.get("new_expires_at") or res.get("expires_at") or ""
            msg = res.get("message") or "续期成功"
            if not quiet:
                print(f"  {c('✔', 'green')} {d.name}  {msg}"
                      + (f" → 新到期 {new_exp}" if new_exp else ""))
            stats["ok"] += 1
            stats["details"].append({"name": d.name, "result": "ok",
                                     "previous": res.get("previous_expires_at"),
                                     "new": new_exp})
            store.append_history({"action": "renew", "name": d.name, "id": d.id,
                                  "result": "ok", "previous": res.get("previous_expires_at"),
                                  "new": new_exp})
        except DnsheError as e:
            stats["fail"] += 1
            if not quiet:
                print(f"  {c('✘', 'red')} {d.name}  失败：{e}")
            stats["details"].append({"name": d.name, "result": "fail", "error": str(e)})
            store.append_history({"action": "renew", "name": d.name, "id": d.id,
                                  "result": "fail", "error": str(e)})
    return stats


def maybe_notify(cfg: dict, stats: dict, *, title: str = "DNSHE 续期结果") -> None:
    n = cfg.get("notify") or {}
    if not n.get("enabled") or not n.get("webhook_url"):
        return
    if n.get("only_on_change", True) and stats.get("ok", 0) == 0 and stats.get("fail", 0) == 0:
        return
    from . import notify as notify_mod
    lines = [f"成功 {stats['ok']} 个，失败 {stats['fail']} 个，跳过 {stats['skip']} 个",
             f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}"]
    for item in stats.get("details", []):
        if item.get("result") == "ok":
            lines.append(f"✅ {item['name']} → {item.get('new') or '已续期'}")
        elif item.get("result") == "fail":
            lines.append(f"❌ {item['name']}：{item.get('error', '')[:120]}")
    notify_mod.send(n["webhook_url"], "\n".join(lines), kind=n.get("kind", "raw"), title=title)


# ------------------------------------------------------------------ 子命令
def cmd_list(args, cfg) -> int:
    domains = fetch(cfg)
    print(render_table(domains))
    total = len(domains)
    win = [d for d in domains if d.renewable()]
    perm = [d for d in domains if d.permanent]
    expired = [d for d in domains if d.expired]
    print()
    print(f"共 {total} 个域名 · 永久 {len(perm)} 个 · 可续期(窗口内) {len(win)} 个"
          + (f" · {c('已过期 %d 个' % len(expired), 'red')}" if expired else ""))
    return 0


def cmd_check(args, cfg) -> int:
    domains = fetch(cfg)
    threshold = args.threshold if args.threshold is not None else int(cfg.get("threshold_days", 30))
    due = plan_renewals(domains, threshold)
    win = [d for d in domains if d.renewable() and d not in due]
    print(f"续期窗口：到期前 {RENEW_WINDOW_DAYS} 天；自动续期阈值：剩余 ≤ {threshold} 天\n")
    if due:
        print(c("需要续期：", "bold"))
        print(render_table(due))
    else:
        print(c("没有到达续期阈值的域名。", "green"))
    if win:
        print(f"\n已进窗口但未到阈值（{len(win)} 个，暂不续期）：")
        print(render_table(win))
    return 0


def cmd_renew(args, cfg) -> int:
    cli = build_client(cfg)
    domains = cli.list_domains()
    by_name = {d.name.lower(): d for d in domains}
    by_id = {d.id: d for d in domains if d.id is not None}

    targets: list[Domain] = []
    if args.all:
        targets = [d for d in domains if d.renewable()]
        if not targets:
            print(c("当前没有处于续期窗口内的域名。", "yellow"))
            return 0
    elif args.due:
        targets = plan_renewals(domains, args.threshold if args.threshold is not None
                                else int(cfg.get("threshold_days", 30)))
        if not targets:
            print(c("没有到达阈值的域名，无需续期。", "green"))
            return 0
    else:
        if not args.targets:
            print(c("请指定要续期的域名或 ID，或用 --all / --due。", "yellow"))
            return 2
        for t in args.targets:
            if t.lower() in by_name:
                targets.append(by_name[t.lower()])
                continue
            try:
                tid = int(t)
            except ValueError:
                tid = None
            if tid is not None and tid in by_id:
                targets.append(by_id[tid])
            else:
                print(c(f"找不到域名：{t}", "red"))

    if not targets:
        return 2
    print(f"准备续期 {len(targets)} 个域名：")
    for d in targets:
        left = "永久" if d.permanent else f"{d.days_left} 天"
        hint = "" if d.renewable() else c("（未进 180 天窗口，后端会拒绝）", "yellow")
        print(f"  - {d.name}（剩余 {left}）{hint}")
    if args.yes is False and not args.dry_run:
        try:
            ans = input("\n确认执行？(y/N) ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            print("已取消。")
            return 0

    stats = do_renew(cli, targets, cfg, dry_run=args.dry_run)
    print(f"\n完成：成功 {stats['ok']} · 失败 {stats['fail']} · 跳过 {stats['skip']}")
    maybe_notify(cfg, stats)
    if not args.dry_run:
        print("\n续期后的状态：")
        try:
            after = cli.list_domains()
            after.sort(key=lambda d: (d.days_left if d.days_left is not None else 99999))
            print(render_table(after))
        except DnsheError as e:
            print(c(f"刷新状态失败：{e}", "yellow"))
    return 0 if stats["fail"] == 0 else 1


def cmd_auto(args, cfg) -> int:
    """无人值守入口：查询 → 挑阈值内的 → 续期 → 通知。"""
    threshold = args.threshold if args.threshold is not None else int(cfg.get("threshold_days", 30))
    store.log(f"===== 自动续期开始（阈值 {threshold} 天{', 演练' if args.dry_run else ''}）=====")
    try:
        cli = build_client(cfg)
        domains = cli.list_domains()
    except DnsheError as e:
        store.log(f"查询域名失败，中止：{e}")
        maybe_notify(cfg, {"ok": 0, "fail": 1, "skip": 0,
                           "details": [{"name": "-", "result": "fail", "error": f"查询失败：{e}"}]},
                     title="DNSHE 续期异常")
        return 1

    store.log(f"共 {len(domains)} 个域名，其中窗口内 {sum(1 for d in domains if d.renewable())} 个")
    targets = plan_renewals(domains, threshold)
    if not targets:
        store.log("没有需要续期的域名，结束。")
        return 0
    store.log("待续期：" + ", ".join(f"{d.name}({d.days_left}d)" for d in targets))

    stats = do_renew(cli, targets, cfg, dry_run=args.dry_run, quiet=False)
    store.log(f"结果：成功 {stats['ok']} · 失败 {stats['fail']} · 跳过 {stats['skip']}")
    maybe_notify(cfg, stats, title="DNSHE 自动续期")
    store.log("===== 自动续期结束 =====")
    return 0 if stats["fail"] == 0 else 1


def cmd_web(args, cfg) -> int:
    from .webui import serve
    return serve(cfg, host=args.host or cfg.get("web_host", "127.0.0.1"),
                 port=args.port or int(cfg.get("web_port", 8899)))


def cmd_config(args, cfg) -> int:
    changed = False
    if args.key is not None:
        cfg["api_key"] = args.key.strip()
        changed = True
    if args.secret is not None:
        cfg["api_secret"] = args.secret.strip()
        changed = True
    if args.threshold is not None:
        cfg["threshold_days"] = int(args.threshold)
        changed = True
    if args.port is not None:
        cfg["web_port"] = int(args.port)
        changed = True
    if args.webhook is not None:
        cfg.setdefault("notify", {})["webhook_url"] = args.webhook.strip()
        cfg["notify"]["enabled"] = bool(args.webhook.strip())
        changed = True
    if args.webhook_kind is not None:
        cfg.setdefault("notify", {})["kind"] = args.webhook_kind
        changed = True
    if changed:
        store.save_config(cfg)
        print(c(f"已保存到 {store.CONFIG_PATH}", "green"))
    print(f"api_key        : {store.mask(cfg.get('api_key', ''))}")
    print(f"api_secret     : {store.mask(cfg.get('api_secret', ''))}")
    print(f"api_base       : {cfg.get('api_base')}")
    print(f"threshold_days : {cfg.get('threshold_days')}")
    print(f"web            : {cfg.get('web_host')}:{cfg.get('web_port')}")
    n = cfg.get("notify") or {}
    print(f"notify         : {'开启' if n.get('enabled') else '关闭'}"
          + (f" ({n.get('kind')}) {store.mask(n.get('webhook_url', ''), 12)}" if n.get("webhook_url") else ""))
    return 0


def cmd_test(args, cfg) -> int:
    print("1) 检查凭据配置 …")
    print(f"   api_key : {store.mask(cfg.get('api_key', ''))}")
    print(f"   secret  : {store.mask(cfg.get('api_secret', ''))}")
    print("2) 调用 subdomains/list …")
    cli = build_client(cfg)
    try:
        domains = cli.list_domains()
    except DnsheError as e:
        print(c(f"   失败：{e}", "red"))
        return 1
    print(c(f"   成功，拿到 {len(domains)} 个域名", "green"))
    if domains:
        print(render_table(domains[:5]))
        print("\n3) 首个域名的原始字段（用于确认字段名）：")
        import json as _json
        print(_json.dumps(domains[0].raw, indent=2, ensure_ascii=False))
    return 0


def _flatten(d: dict, prefix: str = "") -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    for k, v in d.items():
        if isinstance(v, dict):
            out.extend(_flatten(v, f"{prefix}{k}."))
        elif isinstance(v, list):
            out.append((f"{prefix}{k}", f"[{len(v)} 项]"))
        else:
            out.append((f"{prefix}{k}", v))
    return out


def cmd_quota(args, cfg) -> int:
    payload = build_client(cfg).quota()
    rows = _flatten({k: v for k, v in payload.items() if k != "success"})
    if not rows:
        print("配额接口没有返回可读字段：")
        print(payload)
        return 0
    width = max(w(k) for k, _ in rows)
    for k, v in rows:
        print(f"  {pad(k, width)}  {v}")
    return 0


def cmd_history(args, cfg) -> int:
    rows = store.read_history(args.limit)
    if not rows:
        print("（还没有续期记录）")
        return 0
    for r in rows:
        mark = c("✔", "green") if r.get("result") == "ok" else c("✘", "red")
        extra = r.get("new") or r.get("error", "")
        print(f"{r.get('ts', '')}  {mark}  {r.get('name', '')}  {extra}")
    return 0


# ------------------------------------------------------------------ 入口
def main(argv: list[str] | None = None) -> int:
    # 计划任务用 pythonw 跑时没有控制台，stdout 会被置 None，先兜底
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")   # Windows 控制台防乱码
        except (AttributeError, OSError):
            pass

    p = argparse.ArgumentParser(
        prog="dnshe", description="DNSHE 域名查询 / 自动续期工具",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("-v", "--verbose", action="store_true", help="打印请求细节")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("list", help="列出全部域名")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("check", help="只看该续期的域名")
    sp.add_argument("--threshold", type=int, help="剩余天数阈值，默认取配置")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("renew", help="续期指定域名")
    sp.add_argument("targets", nargs="*", help="域名或内部 ID，可多个")
    sp.add_argument("--all", action="store_true", help="续期所有窗口内的域名")
    sp.add_argument("--due", action="store_true", help="续期达到阈值的域名")
    sp.add_argument("--threshold", type=int)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("-y", "--yes", dest="yes", action="store_true", help="跳过确认")
    sp.set_defaults(func=cmd_renew, yes=False)

    sp = sub.add_parser("auto", help="自动续期（给计划任务用）")
    sp.add_argument("--threshold", type=int)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_auto)

    sp = sub.add_parser("web", help="启动本地管理面板")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser("config", help="查看/修改配置")
    sp.add_argument("--key")
    sp.add_argument("--secret")
    sp.add_argument("--threshold", type=int)
    sp.add_argument("--port", type=int)
    sp.add_argument("--webhook")
    sp.add_argument("--webhook-kind", dest="webhook_kind",
                    choices=["wecom", "dingtalk", "feishu", "raw"])
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("test", help="连通性与凭据自检")
    sp.set_defaults(func=cmd_test)

    sp = sub.add_parser("quota", help="查看还可注册多少域名")
    sp.set_defaults(func=cmd_quota)

    sp = sub.add_parser("history", help="最近的续期记录")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_history)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    cfg = store.load_config()
    cfg["_verbose"] = args.verbose
    try:
        return args.func(args, cfg)
    except DnsheError as e:
        print(c(f"错误：{e}", "red"))
        return 1
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
