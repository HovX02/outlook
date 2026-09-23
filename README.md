<div align="center">

# Outlook Auto Register

Microsoft Outlook 纯协议批量注册 · PerimeterX 打码 · Web 控制台 · 代理池 / 账号池 / 测活一体化

<p>
  <a href="https://github.com/lxf746/outlook-auto-register/stargazers"><img src="https://img.shields.io/github/stars/lxf746/outlook-auto-register?style=flat-square&logo=github&color=FFB003" alt="Stars" /></a>
  <a href="https://github.com/lxf746/outlook-auto-register/releases/latest"><img src="https://img.shields.io/github/v/release/lxf746/outlook-auto-register?style=flat-square&logo=github&color=22c55e" alt="Release" /></a>
  <a href="https://github.com/lxf746/outlook-auto-register/network/members"><img src="https://img.shields.io/github/forks/lxf746/outlook-auto-register?style=flat-square&logo=github&color=3b82f6" alt="Forks" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/lxf746/outlook-auto-register?style=flat-square&color=f97316" alt="License" /></a>
</p>

<p>
  <a href="#它解决什么">它解决什么</a>
  &nbsp;·&nbsp;
  <a href="#一眼看完">界面预览</a>
  &nbsp;·&nbsp;
  <a href="#快速开始">快速开始</a>
  &nbsp;·&nbsp;
  <a href="#代理池">代理推荐</a>
  &nbsp;·&nbsp;
  <a href="scripts/ANTIBAN.md">防封策略</a>
  &nbsp;·&nbsp;
  <a href="README_en.md">English</a>
  &nbsp;·&nbsp;
  <a href="README_vi.md">Tiếng Việt</a>
  &nbsp;·&nbsp;
  <a href="#交流群">Telegram</a>
</p>

<img src="assets/screenshots/批次注册.png" alt="Outlook 批次注册实时日志" width="92%" />

</div>

---

> **本仓库地址**：[`lxf746/outlook-auto-register`](https://github.com/lxf746/outlook-auto-register)

> 本项目仅供学习与研究，不得用于商业违规用途。使用者需自行评估并遵守 Microsoft 服务条款，所产生的一切后果由使用者自行承担。

**一句话**：纯协议 Outlook 批量注册 + Web 控制台，从 PX 打码到 proofs 绑定、Graph 读信验收，全流程可视化。

## 它解决什么

多数 Outlook 注册脚本只解决「怎么发 HTTP 请求」，工程化空白很大：PerimeterX 怎么过、代理怎么轮换、proofs 恢复邮箱怎么绑、注册完 token 怎么读信、批量怎么管、号死了怎么测活。本项目把这些串成一条完整链路。

| | 同类脚本 | Outlook Auto Register |
|---|---|---|
| 实现方式 | 浏览器自动化 / 半成品协议 | **纯 HTTP 协议**（Fluent Web API + PX solver），无浏览器依赖 |
| 打码 | 手动 / 单一平台 | captcha.run press/silent，兼容 CapSolver / EzCaptcha |
| 代理 | 单条 `HTTP_PROXY` | **SQLite 代理池**：预检、成功率统计、sticky 绑定、轮换策略 |
| proofs | 常直接 skip（高封号率） | 外部 IMAP 恢复邮箱池 / Cloudflare catch-all 收码 |
| 产出 | 账密 txt | 四段 / 六段 combo + SQLite 统一存储 |
| 运营 | 无 | Web 控制台：批次注册、账号池、测活、IMAP 保活 |
| 读信 | 强依赖 IMAP 开关 | Graph / Outlook REST / Thunderbird scope 多模式，注册完即可读信 |

## 一眼看完

### 批次注册 — 实时日志 + 进度追踪

左侧配参数，右侧 SSE 推送完整链路：代理预检 → PX → CreateAccount → proofs → Graph 可读信。底部汇总单号均耗与阶段耗时。

![批次注册](assets/screenshots/批次注册.png)

### IMAP / 保活 — 定时续期与读信验证

按 scope 自动路由 Graph / Outlook REST / IMAP，支持批量保活任务与读信自检。

![IMAP 保活](assets/screenshots/IMAP保活.png)

## 核心能力

**注册链路**

- **纯协议**：OAuth PKCE → signup.live.com → CheckAvailableSigninName → 两步 risk/verify → CreateAccount → slt 登录 → proofs → 邮件 OAuth
- **PX 打码**：纯协议 PerimeterX solver + captcha.run press/silent 兜底
- **卖家风格产出**：纯小写字母用户名 10–12 位、小写+数字密码 11–14 位、英美姓名随机
- **proofs 恢复邮箱**：IMAP 第三方池 / Cloudflare catch-all（`cf_domain` 后端），禁止默认 skip

**运营管理**

- **Web 控制台**：批次注册、实时 SSE 日志、干跑开关
- **代理池**：SQLite 存储、预检、成功率曲线、sticky 绑定
- **账号池**：状态管理、combo 导出、批量测活
- **保活**：定时 refresh_token 续期 + 读信验证

**打码与收码**

- captcha.run（推荐）/ CapSolver / EzCaptcha
- 恢复邮箱：IMAP 池 / CF Worker 临时邮服
- captcha key 优先读 SQLite（Web 设置页写入），`.env` 作兜底

## 注册流程

```
OAuth 登录页 (PKCE)
  → signup.live.com/signup (解析 ServerData)
  → CheckAvailableSigninName
  → risk/initialize (空 continuationToken)
  → humanSensorUrl + PX collector 预加载
  → risk/verify #1 (px metadata + msaRiskVerifySignature)
  → [riskChallengeRequired] captcha.run press/silent
  → risk/verify #2 (challengeSolution + px metadata)
  → CreateAccount
  → oauth20_authorize.srf (slt 登录)
  → [proofs] 外部恢复邮箱 / CF catch-all / (OUTLOOK_SKIP_PROOFS=1 时 cancel 跳过)
  → [可选] 跳过 Passkey / 获取邮件 OAuth refresh_token
```

## 快速开始

### 环境要求

- Python 3.11+
- **住宅代理**（必须，且 country 与代理出口一致）
- captcha.run API Key（推荐）

### 安装

```bash
git clone https://github.com/lxf746/outlook-auto-register.git
cd outlook-auto-register

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env：恢复邮箱池、CF 域名等（captcha key 建议走 Web 控制台写入数据库）
```

### Web 控制台（推荐）

```bash
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8890
```

浏览器打开 `http://127.0.0.1:8890`：

1. **代理池**页添加住宅代理（支持 `host:port:user:pass` 或旋转网关 `{sid}` 模板）
2. **批次注册**页填写 captcha.run Key → 选产出格式 → 取消「干跑」→ 开始注册
3. **账号池**页查看 / 导出 combo

> 首次使用默认 **干跑开启**，不会真实注册；确认配置无误后取消勾选再执行。

### CLI 注册

```bash
# 单账号（推荐 graph_recovery 六段：Graph token + 恢复邮箱）
OUTLOOK_MAIL_TOKEN_MODE=login_exe python main.py \
  --proxy 'gate.example.com:1000:user:pass-US-{sid}' \
  --country US -v

# 批量（保守并发 + 抖动）
REG_PROXY_RETRIES=6 python main.py --count 10 --concurrency 2 \
  --country US --proxy 'gate.example.com:1000:user:pass-US-{sid}' -v

# 仅注册，不完成 OAuth（无 refresh_token）
python main.py --skip-login

# 注册但不换 refresh_token
python main.py --no-mail-token
```

### 打码平台自检

```bash
python scripts/ez_selftest.py
python scripts/captcha_run_selftest.py
```

## 产出格式

| 模式 | 格式 | 说明 |
|---|---|---|
| `graph` | 四段 | `email----password----client_id----refresh_token` |
| `graph_recovery` / `login_exe` | 六段 | 四段 + `recovery_email----recovery_password`（推荐） |
| `dual` | 六段 | 四段 + `login_client_id----login_refresh_token`（双令牌） |

Web 控制台「产出格式」下拉：

| 选项 | 对应模式 |
|---|---|
| Graph 四段式 | `graph` |
| Graph 六段式（推荐） | `graph_recovery` |
| IMAP 六段式 | `login_exe` |

## 配置详解

### 邮件令牌模式（`OUTLOOK_MAIL_TOKEN_MODE`）

| 模式 | 说明 |
|---|---|
| `graph` | Graph API 读信，注册完即用 |
| `outlook_rest` | Outlook REST API 读信 |
| `login_exe` / `recovery` | Thunderbird scope（IMAP/POP/SMTP + EWS + Mail.Read/Send），卖家同款 |
| `dual` | 双令牌：Graph 收码 + 登录授权 SSO |

### 恢复邮箱 / proofs

```bash
# IMAP 后端（每行 email----password）
OUTLOOK_RECOVERY_BACKEND=imap
OUTLOOK_EXTERNAL_RECOVERY_POOL_FILE=/path/recovery_pool.txt
OUTLOOK_RECOVERY_IMAP_HOST=imap.your-recovery-host.com

# Cloudflare catch-all 后端
OUTLOOK_RECOVERY_BACKEND=cf_domain
OUTLOOK_CF_DOMAIN=your-domain.com
OUTLOOK_CF_WORKER_API_URL=https://apimail.your-domain.com
```

成功绑定后写入 `combo_recovery` 六段，并追加到 `accounts_recovery.txt`。

**禁止**批量 skip proofs（`OUTLOOK_SKIP_PROOFS=1` 仅调试用）。

### 代理池

- Web 控制台「代理池」页管理，数据存 SQLite（`accounts/outlook.db`）
- 支持轮换网关模板（`{sid}` 占位符）、国家标签、预检、成功率统计
- CLI 仍可读 `HTTP_PROXY` 环境变量作兜底

#### 住宅代理推荐 · <a href="https://www.ipwo.net/?ref=githubregister"><u>IPWO</u></a>

<a href="https://www.ipwo.net/?ref=githubregister"><img src="assets/screenshots/ipwo-banner.png" alt="IPWO 住宅代理" width="100%" /></a>

<a href="https://www.ipwo.net/?ref=githubregister"><u>https://www.ipwo.net/?ref=githubregister</u></a>

IPWO 住宅代理覆盖全球 **195+** 地区动静态 IP 资源，支持 **http / https / socks5** 协议，为自动化注册和多账号任务提供更多网络环境选择。适用于账号注册、浏览器自动化、Web 数据处理等场景，灵活配置不同地区代理。

**重点：** 进作者群均有无门槛 **2GB 动态代理** 相送！优惠折扣码：**`0204`**

## 环境变量

| 变量 | 说明 |
|---|---|
| `CAPTCHA_RUN_API_KEY` | captcha.run Bearer key（Web 写入 DB 优先） |
| `CAPTCHA_RUN_API_BASE` | 默认 `https://apicn.captcha.run` |
| `HTTP_PROXY` | CLI 兜底代理 |
| `OUTLOOK_MAIL_TOKEN_MODE` | `graph` / `login_exe` / `outlook_rest` / `dual` |
| `OUTLOOK_RECOVERY_BACKEND` | `imap` / `cf_domain` |
| `OUTLOOK_EXTERNAL_RECOVERY_POOL_FILE` | IMAP 恢复邮箱池文件 |
| `OUTLOOK_RECOVERY_IMAP_HOST` | 恢复邮箱 IMAP 主机 |
| `OUTLOOK_SKIP_PROOFS` | `1` 才允许 cancel 跳过 proofs（默认 `0`） |
| `OUTLOOK_DB_PATH` | 自定义 SQLite 路径 |

完整变量见 [`.env.example`](.env.example)。

## 项目结构

```
outlook-auto-register/
├── main.py                     # 唯一入口：FastAPI（uvicorn）+ CLI（带参数时）
├── controller/                 # HTTP 路由（/api/*）
├── service/
│   ├── registration/           # 注册编排 + cli.py
│   ├── risk/ captcha/ account/ resource/
│   ├── browser/                # Roxy + tools/*.swift
│   ├── web/                    # Job 运行时 / SSE
│   └── px_solver/              # PerimeterX 本地求解
├── dao/ model/ config/ common/
├── frontend/static/index.html
├── docker/
├── scripts/                    # 运维 CLI（check_imap、exchange_code、keepalive…）
└── tests/
```

常用脚本：

```bash
python scripts/check_imap.py "email----pwd----client_id----refresh_token"
python scripts/exchange_code.py --authorize-url
```

## 注意事项

1. **住宅代理必须**，且 **country 需与代理地区一致**（US 代理用 `--country US`）
2. **每个 session 不要反复压测**，易触发 `AADSTS7005106 riskBlock`
3. **PX 模式**仅 `solver`（纯协议 captcha.run）
4. 真实注册前配置 **恢复邮箱池**，否则 proofs 会失败
5. 并发建议 **1–2**，相邻账号加 **3–8 秒抖动**，详见 [防封策略](scripts/ANTIBAN.md)

## 防封策略

详见 [`scripts/ANTIBAN.md`](scripts/ANTIBAN.md)——代理选型、proofs 策略、并发与抖动、产出自检等。

## 交流群

Telegram 群组：[https://t.me/+iPUzZb_YAH45ZmVh](https://t.me/+iPUzZb_YAH45ZmVh)

<div align="center">

<img src="assets/screenshots/telegram-group-qr.jpg" alt="Telegram 交流群二维码" width="260" />

<p>扫码或点击上方链接加入 Telegram 交流群</p>

</div>

## Links

| 链接 | 说明 |
|---|---|
| [Telegram 交流群](https://t.me/+iPUzZb_YAH45ZmVh) | 扫码见上文「交流群」 |
| QQ 群 **1040827527** | 交流讨论 |
| [LINUX DO](https://linux.do/) | 社区讨论 |
| [English](README_en.md) · [Tiếng Việt](README_vi.md) | 多语言文档 |

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=lxf746/outlook-auto-register&type=Date)](https://star-history.com/#lxf746/outlook-auto-register&Date)

> 如果这个项目对你有帮助，欢迎点个 ⭐。若图表暂时显示 GitHub API 限制提示，属 star-history 第三方服务问题，不影响仓库使用。

## License

本项目采用 [GPL-3.0](LICENSE) 许可证。
