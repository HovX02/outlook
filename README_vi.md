<div align="center">

# Outlook Auto Register

Đăng ký hàng loạt Microsoft Outlook thuần giao thức · Giải PX · Bảng điều khiển Web · Proxy pool / account pool / kiểm tra sống

<p>
  <a href="https://github.com/lxf746/outlook-auto-register/stargazers"><img src="https://img.shields.io/github/stars/lxf746/outlook-auto-register?style=flat-square&logo=github&color=FFB003" alt="Stars" /></a>
  <a href="https://github.com/lxf746/outlook-auto-register/releases/latest"><img src="https://img.shields.io/github/v/release/lxf746/outlook-auto-register?style=flat-square&logo=github&color=22c55e" alt="Release" /></a>
  <a href="https://github.com/lxf746/outlook-auto-register/network/members"><img src="https://img.shields.io/github/forks/lxf746/outlook-auto-register?style=flat-square&logo=github&color=3b82f6" alt="Forks" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/lxf746/outlook-auto-register?style=flat-square&color=f97316" alt="License" /></a>
</p>

<p>
  <a href="#vấn-đề-được-giải-quyết">Vấn đề được giải quyết</a>
  &nbsp;·&nbsp;
  <a href="#xem-nhanh">Ảnh chụp</a>
  &nbsp;·&nbsp;
  <a href="#bắt-đầu-nhanh">Bắt đầu nhanh</a>
  &nbsp;·&nbsp;
  <a href="#proxy-pool--ipwo">Proxy đề xuất</a>
  &nbsp;·&nbsp;
  <a href="scripts/ANTIBAN.md">Chống ban</a>
  &nbsp;·&nbsp;
  <a href="README.md">中文</a>
  &nbsp;·&nbsp;
  <a href="README_en.md">English</a>
</p>

<img src="assets/screenshots/批次注册.png" alt="Log đăng ký batch Outlook" width="92%" />

</div>

---

> **Kho lưu trữ**: [`lxf746/outlook-auto-register`](https://github.com/lxf746/outlook-auto-register)

> Chỉ dành cho học tập và nghiên cứu. Không dùng cho mục đích thương mại trái phép. Bạn tự chịu trách nhiệm tuân thủ Điều khoản dịch vụ Microsoft và mọi hậu quả phát sinh.

**Một câu**: Đăng ký Outlook hàng loạt thuần giao thức + bảng điều khiển Web — từ giải PX, gắn proofs đến xác minh đọc thư Graph, toàn bộ trực quan hóa.

## Vấn đề được giải quyết

Đa số script đăng ký Outlook chỉ trả lời “gửi HTTP thế nào”. Còn thiếu: PerimeterX, xoay proxy, proofs email khôi phục, dùng token sau đăng ký, vận hành batch, kiểm tra sống. Dự án này nối trọn pipeline.

| | Script thường gặp | Outlook Auto Register |
|---|---|---|
| Cách làm | Tự động hóa trình duyệt / bán protocol | **HTTP thuần** (Fluent Web API + PX solver), không cần browser |
| Captcha | Thủ công / một nhà cung cấp | captcha.run press/silent; hỗ trợ CapSolver / EzCaptcha |
| Proxy | Một `HTTP_PROXY` | **Proxy pool SQLite**: kiểm tra, thống kê, sticky binding |
| Proofs | Hay skip (dễ ban) | Pool IMAP khôi phục / Cloudflare catch-all |
| Đầu ra | File text đơn giản | Combo 4/6 đoạn + SQLite |
| Vận hành | Không có | Web: đăng ký batch, account pool, kiểm tra sống, keepalive |
| Đọc thư | Phụ thuộc IMAP | Graph / Outlook REST / Thunderbird scope |

## Xem nhanh

### Đăng ký batch — log trực tiếp + tiến độ

Cấu hình bên trái; SSE bên phải: kiểm tra proxy → PX → CreateAccount → proofs → token Graph đọc được. Tóm tắt thời gian mỗi batch ở cuối.

![Đăng ký batch](assets/screenshots/批次注册.png)

### IMAP / keepalive — làm mới token + xác minh thư

Định tuyến theo scope sang Graph / Outlook REST / IMAP; hỗ trợ keepalive hàng loạt.

![IMAP keepalive](assets/screenshots/IMAP保活.png)

## Khả năng chính

**Đăng ký**

- **Thuần giao thức**: OAuth PKCE → signup.live.com → CheckAvailableSigninName → risk/verify hai bước → CreateAccount → slt → proofs → OAuth thư
- **PX**: PerimeterX thuần protocol + captcha.run
- **Kiểu seller**: username chữ thường 10–12 ký tự, mật khẩu chữ thường+số 11–14
- **Proofs khôi phục**: pool IMAP / `cf_domain`; không skip mặc định

**Vận hành**

- **Web console**: batch, log SSE, chế độ dry-run
- **Proxy pool**: SQLite, kiểm tra, đường cong thành công, sticky
- **Account pool**: trạng thái, export combo, kiểm tra sống hàng loạt
- **Keepalive**: gia hạn `refresh_token` + xác minh đọc thư

## Luồng đăng ký

```
OAuth (PKCE)
  → signup.live.com/signup
  → CheckAvailableSigninName
  → risk/initialize
  → PX preload
  → risk/verify #1 / #2
  → CreateAccount
  → slt login
  → proofs (email khôi phục)
  → refresh_token OAuth thư
```

## Bắt đầu nhanh

### Yêu cầu

- Python 3.11+
- **Proxy residential** (bắt buộc; country khớp exit proxy)
- API key captcha.run (khuyến nghị)

### Cài đặt

```bash
git clone https://github.com/lxf746/outlook-auto-register.git
cd outlook-auto-register

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

### Web console (khuyến nghị)

```bash
.venv/bin/uvicorn webapp.server:app --host 0.0.0.0 --port 8890
```

Mở `http://127.0.0.1:8890` → thêm proxy → đăng ký batch → xem/export combo.

> **Dry-run mặc định bật** lần đầu; bỏ chọn trước khi đăng ký thật.

### CLI

```bash
OUTLOOK_MAIL_TOKEN_MODE=login_exe python main.py \
  --proxy 'gate.example.com:1000:user:pass-US-{sid}' \
  --country US -v

REG_PROXY_RETRIES=6 python main.py --count 10 --concurrency 2 \
  --country US --proxy 'gate.example.com:1000:user:pass-US-{sid}' -v
```

## Định dạng đầu ra

| Mode | Mô tả |
|---|---|
| `graph` | 4 đoạn: `email----password----client_id----refresh_token` |
| `graph_recovery` / `login_exe` | 6 đoạn + email/mật khẩu khôi phục |
| `dual` | 6 đoạn + token SSO thứ hai |

### Proxy pool · <a href="https://www.ipwo.net/?ref=githubregister"><u>IPWO</u></a> (residential)

<a href="https://www.ipwo.net/?ref=githubregister"><img src="assets/screenshots/ipwo-banner.png" alt="IPWO residential proxy" width="100%" /></a>

<a href="https://www.ipwo.net/?ref=githubregister"><u>https://www.ipwo.net/?ref=githubregister</u></a>

Proxy residential IPWO phủ **195+** khu vực, IP tĩnh/động, hỗ trợ **HTTP / HTTPS / SOCKS5** — phù hợp đăng ký tài khoản, tự động hóa trình duyệt và xử lý dữ liệu web. **Thành viên nhóm tác giả:** tặng **2GB proxy động** không điều kiện. Mã giảm giá: **`0204`**

## Biến môi trường

Xem [`.env.example`](.env.example). Quan trọng: `CAPTCHA_RUN_API_KEY`, `HTTP_PROXY`, `OUTLOOK_MAIL_TOKEN_MODE`, `OUTLOOK_RECOVERY_BACKEND`.

## Lưu ý

1. Bắt buộc proxy residential; `--country` khớp vùng proxy
2. Không spam cùng session — dễ `AADSTS7005106 riskBlock`
3. Cấu hình pool khôi phục trước khi đăng ký thật
4. Concurrency **1–2**, jitter **3–8 giây** — xem [ANTIBAN.md](scripts/ANTIBAN.md)

## Chống ban

Xem [`scripts/ANTIBAN.md`](scripts/ANTIBAN.md).

## Links

| Liên kết | Mô tả |
|---|---|
| Nhóm QQ **1040827527** | Trao đổi cộng đồng |
| [LINUX DO](https://linux.do/) | Thảo luận cộng đồng |
| [中文](README.md) · [English](README_en.md) | Đa ngôn ngữ |

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=lxf746/outlook-auto-register&type=Date)](https://star-history.com/#lxf746/outlook-auto-register&Date)

> Nếu biểu đồ hiện thông báo hạn chế API GitHub, đó là lỗi tạm thời của star-history.com — không ảnh hưởng kho này.

## License

[GPL-3.0](LICENSE)
