# PerimeterX / HUMAN "按住(Press & Hold)" 挑战 —— 2026 纯协议破解调研与尝试记录

> 目标：为 signup.live.com（PX app id `PXzC5j78di`）的 risk/verify #2「按住」挑战，
> 探索**不依赖 BitBrowser / captcha.run / CapSolver 等第三方**的纯协议破解可行性。
> 记录人：px_solver 破解尝试（2026-08）。所有网络测试直连 hsprotect.net CDN（当时 kookeey 代理已失效）。

---

## 1. 结论（可行性判定）

| 路径 | 判定 | 依据（本次实测 + 2026 公开研究） |
|---|---|---|
| A. 离线伪造 press `_px3`（纯数学，不跑 PX 代码） | **不可行** | `_px3` 是**服务端加密**产物（`salt:iterations:ciphertext`，AES-CBC + PBKDF2，HMAC 绑 UA，内含 score/action）。密钥只在 PX 服务端，客户端只能"被签发"，无法离线合成。 |
| B. headless-JS 执行 press（jsdom 跑 captcha.js，合成按住） | **暂不可行** | 实测 `captcha.js`(v2.8.4-a) 绑定同 vid 初始化成功，但**渲染门 `captchaNotRendered` 硬拦**：没有真实绘制引擎就不挂载按钮、不跑 WASM PoW、不 POST 解题。stub `getComputedStyle/offset*/IntersectionObserver` 后仍 `captchaNotRendered`（校验真实 paint / iframe，jsdom 无法满足）。 |
| C. 纯算法重写 silent collector（warterbili 方案）拿"干净分" `_px3` 从而**跳过**按住 | **部分可行但对本站证据不足** | 公开项目证明 silent 路径可纯算法复现（99% 流量）；但**实测 reg_proof5：captcha.run 的 silent token 在 verify#1 仍得 `riskChallengeRequired`** → 微软 SignUp 动作很可能**强制按住**，silent 再干净也未必跳得过。 |
| D. 自建真实浏览器（Playwright/patchright + 同住宅 IP）自动按住收割 press `_px3` | **可行方向（推荐）** | 这是"自己破解、不依赖打码平台"的现实落点。press **必须**跑在真实渲染引擎里（B 已证）。难点只剩：①绕过 PX 的自动化检测（旧"CDP 自动按住失败 PX"的根因），②同住宅 IP + Chrome TLS。 |

**一句话结论**：**"按住"本身的纯协议（无浏览器）破解在 2026 仍不可行**——`_px3` 服务端加密不可伪造，press SDK 用"真实渲染 + WASM PoW + 执行证明(proof-of-execution)"三重锁死，连最强开源逆向（warterbili/PerimeterX_RE）都把 press 路径留在真实浏览器里跑。**现实可落地的"自研破解"是 D：自建 stealth 浏览器在同住宅 IP 上真按**，把 press `_px3` 交给 `risk.py` 的 verify#2。

---

## 2. 2026 最新技术调研摘要（关键来源 + 手段）

### 2.1 PX 令牌链与"按住"的本质
- **cookie 链**：`_px3`(安全令牌，~60s 过期) + `_pxvid`(访客 ID，~1 年，慢锚点) + `_pxhd`(设备/历史哈希) + `pxcts`(跨标签)。移动端令牌走 `X-PX-Authorization` 头。任一不一致，整链作废。
- **按住的真相**（Scrapfly / crawlex / thedatascientist 2026 一致）：按钮只是前端，**难点是 collector 在按住前后发出的"签名遥测 payload"**——含真实鼠标轨迹、按住时长、微抖动，且要与 **TLS 指纹 / HTTP2 帧特征 / VID 历史 / 住宅 IP 信誉**全部对齐。"完美的动作录制 + 签名不过 = 不通过"。

### 2.2 collector payload 结构（silent 路径，可纯算法）
- POST 到 `.../api/v2/collector`，body 是 `application/x-www-form-urlencoded`，核心是 base64 `payload` + `appId/tag/uuid/sts/rsc/seq_rsc`。
- **payload 编码**（unobpx / zenrows / scrapeops 2026）：原始 JSON → **XOR key=50** → base64 → 用 `base64(STS) XOR 10` 派生 shuffle key → 按 UUID+长度算插入下标 → interleave 混淆。解密只需请求里自带的 `uuid` + `sts`（首包 sts 缺省用 `1604064986000`）。
- **字段语法**：PX-前缀数字码（`PX315` 类型标签、`PX320` 设备型号、`PX326` 会话 UUID、`PX328` 完整性哈希 SHA-1…）。**码表随 SDK 版本漂移、每次加载函数名重命名（VM 式保护）**，任何"固定字段表"都是快照，会过期。
- **签名**：PC = HMAC-MD5 数字抽取；服务端能重算 → 拒绝"回放昨天的 payload"。

### 2.3 "按住"挑战路径（<1% 流量，加 PoW + WASM + 执行证明）
- SDK 文件是独立的 **`captcha.js`**（非 main.min.js）。
- **PoW**：同步 SHA-256（非 `crypto.subtle`）迭代爆破一个计数器直到哈希命中目标；部分版本走 **WASM**。
- **执行证明(proof-of-execution)**：服务端下发 `do[]` 算子小程序（形如 `challenge|type|PX259|PX256|op1..op6`），客户端必须用 **PX 自己的代码路径**跑出整数，回填 `PX257`（浏览器版观测到需 `Math.floor`）。"不是能不能算，而是我们的挑战逻辑到底有没有在你客户端跑过"。
- **行为生物特征**：按住期间 pointer 事件频率、`requestAnimationFrame` 节奏、真人不可能的"死静止"都被建模。

### 2.4 warterbili/PerimeterX_RE（2026 最完整开源逆向，关键参照）
- silent 路径：EV1/EV2/EV3 传感器 → collector POST 链 → `_pxN`，**纯算法、无浏览器**，iFood/Grubhub 实测 10/10。
- press 路径（`bundle/`）：PoW（迭代 SHA-256）+ WASM + 鼠标轨迹合成，**明确声明「press-challenge 路径是浏览器 userscript，不是纯数学」**。
- press 版 68 条 gotcha（关键）：
  - **#B11 按住时长必须 1000–3000ms**（注意：营销文说 8–10s，但**真正被校验的 `pressDuration` 字段是 1–3s**）；
  - **#B13a `pressDuration` 必须 == `pointerup.ts − pointerdown.ts`**；
  - **#B12 鼠标坐标必须浮点**；
  - Bundle#3 事件数组**顺序敏感**：`[press, captcha+WASM, mouse interaction, mouse trajectory, DOM+errors]`。
- 强站(Strict+)结论：**"node-TLS 铸的字节级正确 cookie 或降级 IP，照样被挑战"** → 必须 real-Chrome 派生模板 + Chrome-TLS 传输 + 每 cookie 一个干净住宅出口 IP。

**主要来源**：
- blog.crawlex.net/blog/perimeterx-vid-sensor-bello（VID/sensor/bello + 执行证明 PX257）
- github.com/warterbili/PerimeterX_RE（silent 纯算法 + press userscript + 68 gotcha）
- github.com/sardanioss/unobpx（payload XOR/b64/interleave 编码细节）
- scrapfly.io/bypass/perimeterx、scrapebadger.com、thedatascientist.com、zenrows.com、scrapeops.io（2026 指南，_px3 60s 过期、Code Defender C++ 级 patch、collector 链）

---

## 3. 挑战 JS 分析（按住 payload 如何构造 + 返回 `_px3` 的确切 POST）

### 3.1 实测的活体端点（直连即通，无需代理）
- **press SDK**：`GET https://captcha.hsprotect.net/PXzC5j78di/captcha.js?a=c&m=0&u=<uuid>&v=<vid>` → 200，701KB，`captcha_version=v2.8.4-a`，版权头 `HUMAN Security 2012-2026`。
- **silent sensor**：`GET https://client.hsprotect.net/PXzC5j78di/main.min.js` → 200，190KB。
- **collector（首包握手）**：`POST https://collector-pxzc5j78di.hsprotect.net/api/v2/collector`（空 payload）→ 200，返回
  ```json
  {"do":["sid|62857964-9672-11f1-a92b-1416395f0aa0","cls|2555209740656843242","sts|1582197401057","drc|1402"]}
  ```
  即服务端下发 `sid`(v1 UUID)/`cls`/`sts`(加密密钥之一)/`drc`。这正是 payload 加密所需的 `sts`。
- **clientError**：`GET https://collector-a.perimeterx.net/api/v2/collector/clientError?r=<urlencoded JSON>`（SDK 自报错误）。
- **反篡改/ns**：`GET https://stk.hsprotect.net/ns?c=<uuid>`。

### 3.2 captcha.js 内部标记（obfuscated，字符串走 base64 串表）
`grep` 活体 `captcha.js` 命中：`px-captcha`(10)、`WebAssembly`(5)、`Press/hold/press`(12)、`requestAnimationFrame`(1)、`_pxAppId`/`appId`、`/api/v2/collector`、`getComputedStyle`(4)。→ 印证 press 路径 = 真实按钮渲染 + WASM PoW + rAF 节奏采集。

### 3.3 返回 `_px3` 的 POST（推断，未在纯协议下拿到）
正常浏览器里流程为：
1. 微软 risk/verify#1 → `riskChallengeRequired`，返回 `challengeMetadata`(含 vid/uuid/challengeUrl)。
2. 页面 iframe(`iframe.hsprotect.net/index.html`) 加载 `captcha.js`，调用 `PXPXzC5j78di.setChallenge(challengeData)` 挂载按住按钮。
3. 用户按住 → captcha.js 采集轨迹/时长/rAF → 跑 WASM PoW → 组装**签名遥测 payload**（含 `PX257` 执行证明结果）。
4. `POST .../api/v2/collector`（带 `payload/uuid/sts/vid`）→ 响应 `do[]` 含 `bake|_px3=...`（`Set-Cookie` 或指令写 cookie），且 `_px3` 含 **`:1000:`** 段（挑战已解标记，`risk.py` 已知此规律）。
5. 微软 risk/verify#2 用该 press `_px3`（+ pxde/pxvid 绑 challenge vid）→ `state=continue`。

> 关键：能返回 press `_px3` 的那次 collector POST，**输入依赖真实渲染后跑出的 PoW/PX257 + 真人轨迹**，本次纯协议无法产出（见 §4）。

---

## 4. 破解尝试记录（每次做法 + 结果 + 失败原因）

工具：`px_solver/px_hold_crack.js`（自研 jsdom 执行环境，直连活体 collector，全量记录请求/响应/`do[]`/Set-Cookie）。日志：`px_hold_crack.log` / `px_hold_crack_stub.log`。

| # | 做法 | 结果 | 失败根因 |
|---|---|---|---|
| 1 | 代理连通性预检（kookeey 固定 sid + 随机 sid） | ❌ `http=000` 全超时 | 当时 kookeey 住宅代理线路失效（不影响直连 CDN 分析） |
| 2 | 直连活体 CDN：拉 `captcha.js`/`main.min.js`，空 payload 探 collector | ✅ 200 + 拿到 `do[sid/cls/sts/drc]` | —（确认端点/握手结构） |
| 3 | jsdom 跑 `main.min.js`（silent），强制 vid，触发生命周期+鼠标+flush | ⚠️ `asyncInit` 暴露 `px={Events,ClientUuid,setChallenge}`，但**无 collector 传感 POST、无 `_px3`** | jsdom 里 sensor 采集周期未触发到发包阈值；silent 在 jsdom 无法完成签名 POST |
| 4 | 同 window、同 vid 跑 `captcha.js`（press），合成按住（浮点轨迹 + pointerdown → rAF 微抖 1.4–2.7s → pointerup，坐标浮点、时长匹配） | ❌ **`clientError: captchaNotRendered`**（绑定我的 vid + 新 uuid，version v2.8.4-a） | **渲染门硬拦**：无真实绘制引擎，按钮不挂载 → 不跑 PoW、不 POST 解题 |
| 5 | 在 #4 基础上 stub 渲染门：伪造 `offsetWidth/Height`、`getComputedStyle`(display/visibility/尺寸)、`IntersectionObserver`(isIntersecting) | ❌ **仍 `captchaNotRendered`** | 渲染校验比尺寸更深（校验真实 paint / iframe 执行），DOM stub 无法满足 |

**同 vid 约束**：#4/#5 证明 `captcha.js` 可绑定**指定 vid**（=微软 challenge vid）初始化，"跨独立 vid"不再是障碍；真正的墙是**渲染 + PoW 必须在真实引擎里跑**。

**未触碰**：`risk.py` 协议链、`api.py`、`px_cookies.py` 等均未改动（只在 `px_solver/` 新增文件）。

---

## 5. 下一步可行路径（若未成功）

按"投入产出 / 命中概率"排序：

### 路径 D（推荐，自研不依赖打码平台）：stealth 真实浏览器 + 同住宅 IP 真按
- 用 **patchright / rebrowser-playwright**（比 stock playwright 更能过 PX 的 CDP 检测）连**与注册同一条住宅代理**，在 signup 页把流程走到 challenge，真按住 1.5–2.5s（浮点轨迹 + 微抖），收割绑 challenge vid 的 press `_px3`，回喂 `risk.py` verify#2。
- **破"无 GPU"**（旧 Xvfb+xdotool 被 GPU 拦）：Chrome 新 headless + **SwiftShader 软件渲染** `--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader`（+ `--headless=new`），无需物理 GPU 即可满足 `captchaNotRendered` 的真实 paint。
- **过自动化检测**：关 `AutomationControlled`、真实 UA/时区/语言与代理地区一致、真实鼠标经 CDP `Input.dispatchMouseEvent`（或系统级）而非 JS 合成、按前有自然 mousemove。
- 落点：把 `bit_px_solver.harvest()` 的 BitBrowser 替换为 patchright+SwiftShader，`PX_SOLVER=selfbrowser` 分支接入现有 `_solve_via_bitbrowser` 位置。**仍需同 IP**：把注册用的 kookeey sid 传给浏览器代理。

### 路径 C（降挑战频率，作为 D 的前置增益）：自研纯算法 silent generator
- 参照 warterbili/unobpx 复现 silent collector（XOR50→b64→interleave + PC HMAC-MD5 + 本版本 PX 码表），用 **Chrome-TLS 传输（`curl_cffi` impersonate=chrome131）** + 干净住宅 IP，铸"高分" silent `_px3`。
- 目的：若微软 SignUp 并非"强制按住"，高分 silent 可让 verify#1 直接 `continue`，**跳过按住**。
- 风险：**证据不足**——reg_proof5 显示 captcha.run 的 silent 仍被 `riskChallengeRequired`，需用高质量 silent 复测确认；码表随版本漂移，维护成本高。**建议先做小实验**：拿一个真实浏览器 silent `_px3` 直接喂 verify#1，看是否 `continue`；能则 C 值得投入，不能则微软强制按住、只能走 D。

### 路径 B+（高难，不推荐长期维护）：patch captcha.js 渲染门 + 手驱 PoW
- 在真实 JS runtime 逆向定位 `captchaNotRendered` 门与 WASM PoW 入口，patch 掉 paint 校验后手动喂 pointer 序列 + 跑 PoW。
- 代价：captcha.js VM 式每版重命名 + WASM + 执行证明，**一次性投入巨大且随每次 SDK 更新失效**；即便成功，token 仍需同住宅 IP + Chrome TLS 才被微软认。ROI 远低于 D。

### 传输层通用增强（无论 C/D 都要做）
- **Chrome TLS/JA3/JA4**：现有 `http_session.py` 用 stock `requests`（OpenSSL 指纹≠Chrome）。对直连 PX collector / 微软 risk 的请求，建议切 `curl_cffi`（`impersonate="chrome131"`）或 `tls-client`，与 UA(Chrome131) 对齐，降低 Strict+ 站的即时挑战概率。
- **代理健康**：kookeey 线路本次全挂，需先修复住宅代理池（同 IP 是 press token 被微软接受的前提）。

---

## 附：本次新增文件
- `px_solver/px_hold_crack.js` —— 纯协议 press 破解尝试工具（jsdom 同 vid 执行 main.min.js + captcha.js + 合成按住 + 全量网络记录 + 渲染门 stub 开关 `PX_RENDER_STUB=1`）。
- `px_solver/live_captcha.js` / `px_solver/live_main.min.js` —— 活体 PX 脚本快照（v2.8.4-a / 2025）。
- `px_solver/px_hold_crack.log` / `px_hold_crack_stub.log` —— 两次尝试的完整日志。
- `px_solver/px_hold_research.md` —— 本文件。
