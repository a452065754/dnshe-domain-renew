# dnshe域名续期

DNSHE 免费域名（`.de5.net` / `.us.ci` / `.cc.cd` / `.bot.cd` 等）的**到期查询**与**自动免费续期**工具。

纯 Python 标准库实现，零第三方依赖，Windows / Linux 直接跑。带命令行和本地 Web 面板两套界面。

> 规则提醒：DNSHE 免费域名注册期 1 年，**到期前 180 天**开放免费续期窗口。窗口外调用续期会被后端拒绝，本工具会自动跳过。

---

## 快速开始

```bash
# 1. 配置凭据（DNSHE 后台 → 域名页面 → [API 管理] → 创建，得到 X-API-Key / X-API-Secret）
python dnshe.py config --key 你的Key --secret 你的Secret

# 2. 自检 + 查看全部域名
python dnshe.py test
python dnshe.py list

# 3. 打开图形面板
python dnshe.py web
```

`config.json` 会在第一次运行时自动生成，也可以手工编辑（参考 `config.example.json`）。

---

## 命令行

| 命令 | 作用 |
|---|---|
| `python dnshe.py list` | 列出全部域名：状态、到期时间、剩余天数、是否进窗口 |
| `python dnshe.py check` | 只看「该续期」的；`--threshold 45` 临时改阈值 |
| `python dnshe.py renew --all` | 续期所有已进入 180 天窗口的域名 |
| `python dnshe.py renew --due` | 只续期达到阈值的（更保守） |
| `python dnshe.py renew my.de5.net 12` | 按域名或内部 ID 续期指定域名 |
| `python dnshe.py renew --all --dry-run` | 演练，只打印不真续 |
| `python dnshe.py auto` | 无人值守：阈值内自动续期（计划任务用这个） |
| `python dnshe.py auto --dry-run` | 自动续期的演练 |
| `python dnshe.py web` | 本地面板，默认 `http://127.0.0.1:8899/` |
| `python dnshe.py config` | 查看当前配置（密钥脱敏显示） |
| `python dnshe.py history` | 最近续期记录 |
| `python dnshe.py test` | 连通性 + 凭据自检，并打印首个域名的原始字段 |

退出码：`0` 成功 / `1` 有失败 / `2` 参数问题。适合在脚本里做判断。

---

## Web 面板

```bash
python dnshe.py web              # 默认 127.0.0.1:8899，自动开浏览器
python dnshe.py web --port 8900  # 换端口
```

面板能力：

- 四个统计卡：域名总数、窗口内可续期数、达到阈值的、最紧急剩余天数
- 域名表格：剩余天数进度条 + 紧急度标签（已过期 / 紧急 / 临近 / 窗口内 / 安全 / 永久）
- 单域「续期」按钮、窗口内全部续期、按阈值续期、演练模式
- 阈值输入框直接改并落盘
- 底部展示最近续期记录（来自 `data/history.jsonl`）

面板只绑 `127.0.0.1`，不对外暴露。接口查询有 20 秒缓存，避免刷新时把 API 打爆。

---

## 自动续期部署

### 方案 A：Windows 计划任务（本机每天跑）

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_task.ps1
# 指定时间 / 需要控制台窗口调试
powershell -ExecutionPolicy Bypass -File .\scripts\install_task.ps1 -Time 08:30 -ShowConsole
```

任务名「DNSHE域名自动续期」，默认用 `pythonw.exe` 静默运行，日志写到 `logs\auto-YYYYMMDD.log`。
删除：`.\scripts\uninstall_task.ps1`

### 方案 B：Linux 服务器 cron

```bash
crontab -e
# 每天 09:10 跑一次
10 9 * * * cd /opt/dnshe域名续期 && /usr/bin/python3 dnshe.py auto >> logs/cron.log 2>&1
```

### 方案 C：GitHub Actions（不用自己的机器）

```yaml
# .github/workflows/dnshe-renew.yml
name: dnshe-renew
on:
  schedule:
    - cron: "10 1 * * *"     # UTC 01:10 = 北京 09:10
  workflow_dispatch:
jobs:
  renew:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with: { python-version: "3.12" }
      - run: python dnshe.py auto
        env:
          DNSHE_API_KEY: ${{ secrets.DNSHE_API_KEY }}
          DNSHE_API_SECRET: ${{ secrets.DNSHE_API_SECRET }}
          DNSHE_THRESHOLD_DAYS: "180"      # config.json 不入库，阈值必须显式给
```

仓库里已经带了一份可直接用的完整 workflow（含日志 artifact 上传），上面是精简版。
凭据走环境变量 `DNSHE_API_KEY` / `DNSHE_API_SECRET`，不必落盘。私有仓库才适合这么干（Actions 日志里别打印密钥）。

---

## 通知（可选）

续期成功/失败推送到企业微信、钉钉、飞书机器人：

```bash
python dnshe.py config --webhook "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx" --webhook-kind wecom
```

`--webhook-kind` 可选 `wecom` / `dingtalk` / `feishu` / `raw`。
`only_on_change = true`（默认）时，没有续期动作就不推送，避免每天刷屏。

---

## 配置项

`config.json`：

| 字段 | 说明 | 默认 |
|---|---|---|
| `api_key` / `api_secret` | DNSHE 的 X-API-Key / X-API-Secret | 空 |
| `api_base` | 接口基址 | `https://api005.dnshe.com/index.php` |
| `threshold_days` | 剩余天数 ≤ 此值才自动续期（仍须在 180 天窗口内） | `30` |
| `timeout` | 单次请求超时秒数 | `25` |
| `web_host` / `web_port` | 面板监听地址与端口 | `127.0.0.1` / `8899` |
| `notify.enabled` | 是否推送 | `false` |
| `notify.webhook_url` / `notify.kind` | 机器人地址与类型 | 空 / `wecom` |
| `notify.only_on_change` | 无动作时不推送 | `true` |

环境变量优先级高于文件：`DNSHE_API_KEY`、`DNSHE_API_SECRET`、`DNSHE_API_BASE`。

---

## API 说明（踩过的坑）

```
基础地址  https://api005.dnshe.com/index.php?m=domain_hub&endpoint=<ep>&action=<act>
认证头    X-API-Key / X-API-Secret
```

| 用途 | 调用 |
|---|---|
| 域名列表 | `GET  endpoint=subdomains  action=list` |
| 续期 | `POST endpoint=subdomains  action=renew`  body `{"subdomain_id": 12}` |
| DNS 记录 | `GET  endpoint=dns_records  action=list&subdomain_id=12` |

坑位记录：

1. **官方知识库第三步的续期命令是错的**。文档写的是 `GET ...endpoint=dns_records&action=list&subdomain_id=1`，那条实际只是「按 id 列 DNS 记录」，不会续期。真实续期走 `subdomains/action=renew`。本工具会自动尝试 POST/GET/PUT 三种形态，成功一次后记住。
2. 认证先于路由：凭据错的时候打任何 endpoint 都只返回 `Invalid API key`，探测不到端点是否存在。
3. 列表接口在旧后端上可能是 `dns_records/action=list` 返回 `subdomains` 数组，所以列域名时对两个 endpoint 都试。
4. 到期字段命名不统一，客户端按 `expires_at / expire_at / expires / expiration / expiry / end_time ...` 依次取第一个有值的。
5. 未到 180 天窗口就调续期会被拒（返回里带 window / not yet 之类字样），工具识别后退避，不会反复重试。

---

## 目录结构

```
dnshe域名续期/
├── dnshe.py                入口
├── dnshe/
│   ├── client.py           API 客户端 + 域名归一化 + 续期计划
│   ├── cli.py              命令行
│   ├── webui.py            本地面板服务
│   ├── web/index.html      面板前端
│   ├── store.py            配置/日志/历史
│   └── notify.py           Webhook 通知
├── scripts/
│   ├── install_task.ps1    Windows 计划任务注册
│   ├── uninstall_task.ps1  删除计划任务
│   └── run_auto.bat        批处理入口
├── config.example.json
├── config.json             首次运行自动生成（含密钥，勿提交）
├── data/history.jsonl      续期历史
├── logs/auto-YYYYMMDD.log  运行日志
└── docs/                   开发文档 / 进度 / 蓝图 / 交接清单
```

## 安全

`config.json` 里有 API 密钥，已在 `.gitignore` 里排除。要提交仓库就用环境变量或 GitHub Secrets。
