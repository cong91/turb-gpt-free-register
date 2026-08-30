# Turb GPT Free Register

ChatGPT / OpenAI 账号自动注册与 Codex OAuth 授权工具。当前项目支持三套注册驱动：

- **protocol**：原纯协议注册，基于 `curl_cffi` + Sentinel/PoW。
- **roxy**：RoxyBrowser 指纹浏览器 + Selenium 自动化注册，兼容新版页面流，例如 `create-account/password`、`about-you` 年龄/生日表单、地区本地化页面等。
- **cloak**：CloakBrowser + Playwright 适配层自动化注册，支持免费 binary、无头模式、humanize、固定 fingerprint seed、代理 geoip。
- **browser_use**：Browser Use Cloud stealth Chromium + Playwright（可选住宅代理，无需本机安装 Roxy）。
- **skyvern**：Skyvern Browser Sessions 云端浏览器 + Playwright CDP。

项目提供 **CLI** 和 **本地 WebUI** 两种使用方式。日常推荐使用 WebUI。

> 项目说明：本项目基于 [xiaoguzuiniu/gpt-free-register](https://github.com/xiaoguzuiniu/gpt-free-register) 进行改造与扩展。

- TG 交流群：[https://t.me/+gu_cvEKq_vcyZWRl](https://t.me/+gu_cvEKq_vcyZWRl)

> 开源版说明：仓库只保留源码、配置模板和文档；运行时账号、Token、邮箱池、Codex 凭证、日志等真实数据均已通过 `.gitignore` 排除。

### Runtime state database

应用业务状态统一保存在根目录的 `app_state.sqlite3`：账号、邮箱池、注册任务、provider quota、batch assignment、OTP 去重和 Roxy profile catalog 都以它为 source of truth。JSON/TXT/HTML、Codex credential 文件以及 Roxy archive/log 仅作为导出或 artefact；运行时不会从旧版 SQLite、JSON、TXT 或 ledger 文件隐式导入状态。

---

## 功能概览

### 注册

- 批量注册 ChatGPT 账号。
- 支持注册驱动切换：
  - `REGISTRATION_DRIVER = "protocol"`
  - `REGISTRATION_DRIVER = "roxy"`
  - `REGISTRATION_DRIVER = "cloak"`
  - `REGISTRATION_DRIVER = "browser_use"`
  - `REGISTRATION_DRIVER = "skyvern"`
- 支持 RoxyBrowser 一号一环境：自动创建、打开、关闭、删除 Roxy Profile。
- 支持 Roxy 无头启动：`ROXY_OPEN_HEADLESS=True`。
- 支持 CloakBrowser：免费 binary、无头模式、humanize、固定 fingerprint seed、按出口 IP 自动匹配语言/时区/WebRTC。
- Roxy / Cloak / Browser Use / Skyvern 浏览器注册统一强制使用 OpenAI 注册密码：
  - 即使填邮箱后直接进入邮箱验证码页，也会先切换到 `create-account/password` 设置密码；
  - 无法进入或填写密码页时直接失败，不降级为 OTP-only；
  - `about-you/profile` 页面直接输入年龄数字；
  - `about-you/profile` 页面输入年月日生日；
  - React Aria birthday select / spinbutton 年月日控件；
  - 不同出口 IP / 不同页面语言下按钮顺序变化导致的三方登录误点问题。

### 邮箱来源

支持多种邮箱来源：

- Outlook 邮箱池：`email----password----clientId----refreshToken`
- Cloudflare 域名邮箱 + QQ 邮箱 IMAP 收信（`cloudflare_domain`）
- Cloudflare Worker 临时邮箱：自动创建 + JWT 取码（`cloudflare`，兼容 cloudflare_temp_email）
- 通用 API 邮箱：`email----取码地址`
- Gmail API URL 邮箱：`email----取码URL`，轮询 API 响应 `code=601`（等待）、`code=602`（失败/退款）、`code=0`（成功）
- GPTMail 临时邮箱 API：运行时随机生成邮箱并自动收取验证码
- TinyHost 临时邮箱 API：从全量在线域名中选择域名，生成随机邮箱并自动收取验证码（`tinyhost`）
- Paymesh MAIL card：`POST /api/v1/redeem` 领取邮箱，`GET /api/v1/order/lookup` 自动收取验证码（`paymesh`）
- `EMAIL_SOURCE` 支持多个来源组合，例如：

```python
EMAIL_SOURCE = "outlook,generic_api,gmail_api_url"
```

- MailNest-迈巢：Outlook 临时邮箱

### Codex OAuth

- 注册成功后可自动跑 Codex OAuth。
- 可在 WebUI 配置开启 `AUTO_CODEX_FOR_FREE_AFTER_REGISTER`：注册后先查套餐，只有确认是 Free 且 `plus_trial_eligible` 明确为 `False`（不是 Free Plus）才会自动创建 Codex 补跑任务；该选项会自动开启注册后套餐查询。
- Codex 授权驱动可选：
  - `CODEX_OAUTH_DRIVER = "protocol"`
  - `CODEX_OAUTH_DRIVER = "roxy"`
  - `CODEX_OAUTH_DRIVER = "cloak"`
  - `CODEX_OAUTH_DRIVER = "browser_use"`
  - `CODEX_OAUTH_DRIVER = "same_as_registration"`
- 支持 CPA 管理接口生成授权 URL，并提交 OAuth callback。
- 支持接码平台：
  - GrizzlySMS
  - ViOTP
  - 本地 L/H 取号服务，见 `L_API.md` / `H_API.md`
- 手机验证支持自动取号、填号、收码、提交、失败换号重试。
- Codex 凭证保存到 SQLite 的 `codex_accounts` 表。

### WebUI

- 批量启动注册任务。
- 实时查看任务日志。
- 动态调整注册线程数，提交后新任务立即使用最新值。
- 批量补跑 Codex，补跑线程数每次提交即时生效。
- 管理账号、邮箱池、Codex 凭证；账号页支持复制全部/选中整行，邮箱池列表展示导入时间、已用时间和状态。
- 配置页支持热加载，保存后无需重启。
- Roxy 团队/项目可在配置页获取并保存。
- 代理池配置支持 Proxy.vn 代理旋转：注册、Codex OAuth/补跑、查活、套餐、提链、2FA、改邮箱和 Codex Agent 等账号 workflow 都通过持久 lease 取 proxy；同一 `scope/lane` 复用 proxy TTL，`keyxoay` 在所有 scope 之间全局不重复。

### Proxy.vn 代理旋转

在 WebUI 的「代理池」中开启「Proxy.vn 代理旋转」，填写主 API Key，并选择 `http` 或 `socks5`。对应环境变量如下，API Key 只放在 `.env`：

```dotenv
ROTATING_PROXY_ENABLED=true
ROTATING_PROXY_API_KEY=你的_proxy.vn_API_key
ROTATING_PROXY_PROTOCOL=http
ROTATING_PROXY_NHAMANG=random
ROTATING_PROXY_TINHTHANH=0
ROTATING_PROXY_WHITELIST=
```

批量注册的 `workers` 会映射为稳定的 lane（`index % workers`）。lane 有未过期 lease 时不会重复请求 API；proxy TTL 到期才调用 `proxyxoay.shop/api/get.php`。配置页状态区会分别显示 workflow scope（例如 `registration:0`、`codex_retry:0`），且只展示脱敏 key、assignment 和 proxy，不展示主 API Key。

### 数据存储

- 迁移完成后，账号、邮箱库、任务、Codex 凭证以及 provider quota、batch assignment、OTP 去重和 Roxy profile catalog 运行时统一存储在项目根目录 `app_state.sqlite3`。核心业务表包括 `accounts`、`email_pool`、`registration_jobs`、`codex_accounts` 和 `codex_agent_accounts`。
- 中央数据库使用 rollback journal（`journal_mode=DELETE`）、`synchronous=FULL`、超时等待和常用字段索引，以兼容 CDK/Gmail CDK 等 provider store。WebUI 的账号、套餐状态、邮箱池、Codex 和任务分页直接执行 SQLite `COUNT(*) + LIMIT/OFFSET`。
- `turb.sqlite3` 是迁移前 origin 的离线输入，不是迁移后的运行时 source of truth。应用启动不会再从旧 SQLite、JSON、TXT 或 Codex credential 文件隐式导入状态。
- `app_state.sqlite3*` 和 `turb.sqlite3*` 属于运行时/迁移数据，已加入 `.gitignore`，必须纳入备份策略；迁移完成后仍应保留原始 `turb.sqlite3` 和 SQLite snapshot，直到 smoke test 通过。

#### Split SQLite migration runbook

迁移只在停止所有写入进程后执行，并且不会修改两个 source 文件。先在项目根目录运行只读 audit：

```powershell
python -m core.sqlite_state_migration audit `
  --app-state .\app_state.sqlite3 `
  --turb .\turb.sqlite3
```

Sau đó chạy rehearsal không ghi file bằng `--dry-run`:

```powershell
python -m core.sqlite_state_migration migrate `
  --app-state .\app_state.sqlite3 `
  --turb .\turb.sqlite3 `
  --target .\app_state.migrated.sqlite3 `
  --backup-dir .\migration-backups `
  --dry-run
```

Khi audit đúng, tạo target mới và snapshot trong một thư mục backup mới hoặc trống; không dùng target trùng với source và không ghi đè snapshot đã tồn tại:

```powershell
python -m core.sqlite_state_migration migrate `
  --app-state .\app_state.sqlite3 `
  --turb .\turb.sqlite3 `
  --target .\app_state.migrated.sqlite3 `
  --backup-dir .\migration-backups
```

Service dùng SQLite backup API để snapshot, giữ nguyên toàn bộ bảng của `app_state.sqlite3`, rồi chỉ merge năm bảng authoritative từ `turb.sqlite3`. Duplicate cùng khóa và cùng nội dung được bỏ qua; schema hoặc row khác nội dung sẽ dừng và xóa target sinh ra. Kết quả có integrity check, foreign-key check, schema/count/digest verification và migration marker `migration:application_state:1`, không chứa payload row.

Sau khi target validation thành công, giữ nguyên source và snapshot. Dừng application, giữ lại app-state cũ rồi promote target; các lệnh PowerShell sau cố ý không dùng `-Force`:

```powershell
if (Test-Path .\app_state.sqlite3.pre-unified) { throw "rollback copy already exists" }
Move-Item -LiteralPath .\app_state.sqlite3 -Destination .\app_state.sqlite3.pre-unified
Move-Item -LiteralPath .\app_state.migrated.sqlite3 -Destination .\app_state.sqlite3
```

Sau đó mới khởi động smoke test; marker trong target khiến core repository chuyển sang `app_state.sqlite3`. Nếu smoke test lỗi, dừng application và rollback bằng cách giữ target lỗi để điều tra rồi khôi phục bản cũ:

```powershell
Move-Item -LiteralPath .\app_state.sqlite3 -Destination .\app_state.sqlite3.failed-unified
Move-Item -LiteralPath .\app_state.sqlite3.pre-unified -Destination .\app_state.sqlite3
```

Không xóa source hoặc snapshot. `turb.sqlite3` chỉ được archive sau khi đã xác nhận runtime đọc đúng `app_state.sqlite3`.

---

## 环境要求

- Python 3.10+
- Node.js 18+
- 可用代理、系统代理/VPN，或 RoxyBrowser 代理环境
- 如使用 Roxy 注册：需要本机 RoxyBrowser API 可访问
- 如使用 Cloak 注册：首次运行会自动下载 Cloak Chromium binary；`CLOAK_GEOIP=True` 需要 `cloakbrowser[geoip]` 依赖
- 如启用 Codex 自动授权：需要接码平台配置

安装依赖：

```bash
pip install -r requirements.txt
node --version
```

### 密钥配置（.env）

重要 API Key 请放在项目根目录 `.env`，不要写进 `config/*.py`。

```bash
cp .env.example .env
# 编辑 .env，例如：
# BROWSER_USE_API_KEY=...
# ROXY_API_TOKEN=...
```

当前支持从 `.env` 读取的密钥：

- `WEBUI_AUTH_CODE`（WebUI 登录授权码）
- `WEBUI_SESSION_SECRET`（可选，Session Cookie 签名密钥）
- `BROWSER_USE_API_KEY`
- `SKYVERN_API_KEY`
- `ROXY_API_TOKEN`
- `QQ_IMAP_PASSWORD`
- `CLOUDFLARE_API_KEY` / `CLOUDFLARE_CUSTOM_AUTH`（`EMAIL_SOURCE=cloudflare` 时）
- `CPA_MANAGEMENT_KEY`
- `SMS_API_KEY`
- `L_ADMIN_AUTH_CODE`
- `H_ADMIN_AUTH_CODE`

WebUI 配置页保存这些字段时会写入 `.env`（不是 config 源码）。

---

## 快速开始

### Windows 一键启动

双击项目根目录的 `start-local.bat` 即可启动。

命令行方式：

```bat
start-local.bat
```

脚本会自动检查 Python 3.10+ / Node.js 18+、创建 `.venv`、按需安装 `requirements.txt`、在缺失时从 `.env.example` 创建 `.env`，然后启动 WebUI 并打开浏览器。

常用参数：

```bat
start-local.bat -Port 5057
start-local.bat -AuthCode "你的授权码"
start-local.bat -NoBrowser
start-local.bat -CheckOnly
```

启动前脚本会先 force-close `-Port` 指定端口上的监听进程（默认 `5057`），避免旧 WebUI 或残留进程造成端口冲突。`-CheckOnly` 只检查环境和依赖，不启动 WebUI，也不会关闭现有进程或提交注册任务。需要更多 PowerShell 参数时，`start-local.bat` 会原样转发给 `start-local.ps1`。

### WebUI 授权码

WebUI 启动后，除 `/login` 外所有页面和 `/api/*` 接口都会校验授权码。推荐在 `.env` 中配置：

```dotenv
WEBUI_AUTH_CODE=你的授权码
```

也可以启动时直接传入：

```bash
python web.py --auth-code 你的授权码
```

优先级：`--auth-code` > `.env`/环境变量。若都未设置，启动时会在日志中生成并打印本次临时授权码。接口调用可使用登录后的 Cookie，或传 `X-Auth-Code: <授权码>` / `Authorization: Bearer <授权码>`。

`WEBUI_SESSION_SECRET` 可选；未设置时会从固定授权码派生稳定的 Session 签名密钥，修改授权码后已有登录会自动失效。

### 1. 配置邮箱源

#### Outlook 邮箱池

复制示例文件：

```bash
cp 用于注册的邮箱.txt.example 用于注册的邮箱.txt
```

每行格式：

```text
email----password----clientId----refreshToken
```

也可以在 WebUI 的「邮箱池」页面导入。

#### 通用 API 邮箱

每行格式：

```text
email----code_url
```

在 `config/email.py` 设置：

```python
EMAIL_SOURCE = "generic_api"
```

或使用组合来源：

```python
EMAIL_SOURCE = "outlook,generic_api,mailnest"
```

#### Gmail API URL 邮箱

专用于 MailsAPI 类接口。导入格式：

```text
email----取码URL
```

在 WebUI 邮箱池页面选择「Gmail API URL」导入，或在 `config/email.py` 设置：

```python
EMAIL_SOURCE = "gmail_api_url"
```

轮询响应码规则：
- `{"code": 601}` — 等待中，继续轮询
- `{"code": 602}` — 提供商错误，标记邮箱为失败并记录退款提示
- `{"code": 0, "data": {"code": "123456"}}` — 成功，返回验证码

邮箱池业务状态保存在 `app_state.sqlite3`；`用于注册的Gmail API邮箱.json` 仅为同步导出。

#### QAN8 Gmail API lazy provider

QAN8 provider dùng tài liệu API chính thức tại [shop.qan8.com/api-docs](https://shop.qan8.com/api-docs). Cấu hình:

```dotenv
EMAIL_SOURCE=qan8_gmail_api
QAN8_API_BASE=https://shop.qan8.com
QAN8_API_KEY=your_qan8_api_key
QAN8_GMAIL_SKU_ID=your_gmail_sku_id
QAN8_ALIASES_PER_SOURCE=12
```

Số worker hiệu dụng cũng là số lane và số source đang hoạt động. Ví dụ `workers=5` tạo tối đa 5 source Gmail gốc khác nhau, mỗi lane giữ một source và xử lý alias của lane theo thứ tự. Client sinh alias từ mail gốc; mọi alias của source dùng chung `code_url` để nhận OTP. Khi một lane hết alias, lane đó mới mua thêm đúng một source; các lane khác không bị đổi source. QAN8 delivery phải trả về đúng một bản ghi `email----code_url`, vì quantity luôn là 1.

Chi tiết lifecycle, recovery và contract delivery xem [docs/qan8_gmail_api_lazy.md](docs/qan8_gmail_api_lazy.md).

#### GPTMail 临时邮箱

在 WebUI 的「配置 → 邮箱 / OTP」填写 `GPTMail API Key`，然后将邮箱来源设置为：

```python
EMAIL_SOURCE = "gptmail"
```

也可以在项目根目录 `.env` 中填写：

```dotenv
GPTMAIL_API_KEY=你的_GPTMail_API_Key
```

服务地址固定为 `https://mail.chatgpt.org.uk`。未填写 Key 时，任务会提示填写 `GPTMail API Key`，不会使用公共测试 Key。

#### TinyHost 临时邮箱（`tinyhost`）

TinyHost 不需要 API Key。注册任务创建邮箱时，程序调用 `GET /api/all-domains/` 获取全部在线域名，再生成一个合法的 `user`（即邮箱地址的 local-part），形成 `user@domain`。TinyHost 文档没有单独的 create-user endpoint；收件箱通过 `GET /api/email/{domain}/{user}/` 由这两个路径参数定位。验证码轮询调用：

```dotenv
EMAIL_SOURCE=tinyhost
TINYHOST_API_BASE=https://tinyhost.shop
TINYHOST_REQUEST_TIMEOUT=20
TINYHOST_RANDOM_LOCAL_LENGTH=12
```

收到邮件后按 TinyHost 的 `sender`、`subject`、`body`、`html_body` 字段提取 OpenAI 六位验证码，然后继续使用现有注册流程。若 ChatGPT 在 `about-you` 提交后明确返回“不支持此邮箱”，程序会把该邮箱所属 domain 记录为 `disabled`，后续从全量列表中跳过该 domain。TinyHost 文档说明邮件和不活跃用户会在 3 天后清理；API 也有按 IP/endpoint 的限流，请按实际额度设置并发。

#### Paymesh MAIL card（`paymesh`）

在 WebUI 注册页选择 `Paymesh MAIL card`，每行输入一个 card；API 地址默认是 `https://sms.paymesh.cn`。也可以通过配置指定：

```dotenv
EMAIL_SOURCE=paymesh
PAYMESH_API_BASE=https://sms.paymesh.cn
PAYMESH_REQUEST_TIMEOUT=30
PAYMESH_OTP_MAX_WAIT=180
PAYMESH_ACCOUNTS_PER_CDK=6
```

Provider 通过 `POST /api/v1/redeem`（body 为 `{"code":"..."}`）领取邮箱，再轮询 `GET /api/v1/order/lookup?code=...&poll=true` 获取 OTP。`PAYMESH_REQUEST_TIMEOUT` 只限制每次 HTTP 请求；`PAYMESH_OTP_MAX_WAIT` 限制每轮 OTP 轮询，默认 180 秒，Roxy 注册最多等待 3 轮并在前两轮超时后重发。每个 card 最多分配 6 个邮箱别名；运行时 ledger 仅保存 card 哈希，不写入原始 card。账号注册成功后会把原始 card 作为 `source_cdk` 写入受保护的账号数据，用于追溯来源。

#### Cloudflare Worker 临时邮箱（`cloudflare`）

兼容 `cloudflare_temp_email` 类 Worker：注册时自动创建域名邮箱，并用 JWT 轮询收件箱提取 OpenAI 六位验证码。  
（与下方 `cloudflare_domain` / QQ IMAP 方案不同，请勿混用标识。）

```dotenv
EMAIL_SOURCE=cloudflare
CLOUDFLARE_API_BASE=https://你的-worker-api-域名
CLOUDFLARE_API_KEY=你的_ADMIN_PASSWORD
CLOUDFLARE_AUTH_MODE=x-admin-auth
# admin 创建时常用：
# CLOUDFLARE_PATH_ACCOUNTS=/admin/new_address
CLOUDFLARE_DEFAULT_DOMAINS=你的收信域名.com
```

匿名模式可将 `CLOUDFLARE_AUTH_MODE=none` 且 Key 留空，创建路径默认 `/api/new_address`；若被 Turnstile 拦截请改用 admin 模式。更多字段见 WebUI「配置 → 邮箱 / OTP」或 `.env.example`。

#### Cloudflare 域名邮箱（`cloudflare_domain`）

在 `config/email.py` 设置：

```python
EMAIL_SOURCE = "cloudflare_domain"
EMAIL_DOMAIN = "你的域名"
QQ_EMAIL = "你的QQ邮箱"
QQ_IMAP_PASSWORD = "QQ邮箱IMAP授权码"
```

Cloudflare Email Routing 需要把域名邮件转发到 QQ 邮箱。此模式不调用 Worker 创建接口，仅本地生成地址并通过 QQ IMAP 取件。

#### MailNest-迈巢 Outlook 临时邮箱

可直接在 Web-UI 中配置 API Key 与项目代码`MAIL_NEST_PROJECT_CODE`，也可以在配置文件中配置。

- `api-key`获取页面：https://mailnest.top/account
- 项目代码获取页面：https://mailnest.top/buy-email。默认为`chatgpt001`，可以直接使用

---

### 2. 配置注册驱动

编辑 `config/roxybrowser.py`，或直接在 WebUI「配置」页修改。

#### 使用 RoxyBrowser 注册

```python
REGISTRATION_DRIVER = "roxy"  # 可选 protocol / roxy / cloak
ROXY_API_BASE = "http://127.0.0.1:50100"
ROXY_API_TOKEN = "你的Roxy API Key"
ROXY_WORKSPACE_ID = "你的workspaceId"
ROXY_PROJECT_ID = "你的projectId"
ROXY_ONE_PROFILE_PER_ACCOUNT = True
ROXY_DELETE_PROFILE_AFTER_RUN = True
ROXY_CREATE_USE_PROXY_POOL = True
```

##### NordVPN accessToken → 独立 Roxy 代理

Nếu chỉ có NordVPN accessToken (giống cách JNMBrowser cấu hình), vào WebUI
`配置 → NordVPN WireGuard`, bật `启用独立代理`, rồi điền:

```env
NORDVPN_ACCESS_TOKEN=your_nordvpn_access_token
NORDVPN_WG_COUNTRY_FILTER=JP
NORDVPN_WG_ENABLED=True
```

Khi công tắc này bật, chế độ không cần NordVPN desktop/CLI. Tắt
`NORDVPN_WG_ENABLED` sẽ luôn ngừng dùng NordVPN, kể cả khi accessToken còn lưu.
Mỗi task sẽ thực hiện theo thứ tự:

1. Dùng Bearer token lấy `nordlynx_private_key` từ NordVPN Core API.
2. Chọn một server NordLynx online khác với các server vừa dùng.
3. Tạo một SOCKS5 cục bộ bằng `wireproxy`.
4. Ghi SOCKS5 vào `proxyInfo` của `/browser/create`, sau đó mới `/browser/open`.
5. Dừng `wireproxy` và xóa file config tạm khi task kết thúc.

Nếu máy chưa có `wireproxy.exe`, chương trình tự tải release đã pin và kiểm tra
SHA-256 vào `data/tools/wireproxy/`. Có thể tắt bằng
`NORDVPN_WG_AUTO_DOWNLOAD=False` hoặc điền đường dẫn riêng tại
`NORDVPN_WG_WIREPROXY_EXE`.

Lưu ý:

- Để trống `ROXY_PROFILE_ID`; NordVPN proxy chỉ được attach chắc chắn khi tạo profile mới.
- Access token và NordLynx private key không được gửi vào Roxy profile hoặc log.
- Token mode tự vô hiệu hóa cơ chế NordVPN CLI auto-rotation và không ép workers về 1.
- Proxy explicit truyền từ caller vẫn có độ ưu tiên cao hơn NordVPN token mode.

如要无头：

```python
ROXY_OPEN_HEADLESS = True
```


#### 使用 CloakBrowser 注册

如需改用 CloakBrowser，先安装依赖：

```bash
pip install -r requirements.txt
```

然后在 `config/roxybrowser.py` 或 WebUI 配置页把注册驱动改为：

```python
REGISTRATION_DRIVER = "cloak"
```

再在 `config/codex.py` 或 WebUI「CPA / Codex」分组设置 Codex 授权驱动：

```python
CODEX_OAUTH_DRIVER = "same_as_registration"  # 跟随注册驱动
# 或单独指定："protocol" / "roxy" / "cloak" / "browser_use"
```

CloakBrowser 专用配置在 `config/cloakbrowser.py`：

```python
CLOAK_HEADLESS = False          # True=无头；False=显示窗口
CLOAK_HUMANIZE = True           # 人工鼠标/键盘/滚动行为
CLOAK_GEOIP = True              # 按当前出口 IP 自动匹配语言/时区/WebRTC
CLOAK_LOCALE = ""               # 留空自动；也可强制如 ja-JP / en-US
CLOAK_TIMEZONE = ""             # 留空自动；也可强制如 Asia/Tokyo
CLOAK_LICENSE_KEY = ""          # 留空使用免费 binary；填 Pro key 使用最新版
CLOAK_FINGERPRINT_SEED = ""     # 留空每次随机；固定值=固定指纹
CLOAK_USER_DATA_DIR = ""        # 留空临时环境；填路径可持久化 profile
```

说明：

- `CLOAK_GEOIP=True` 会按当前出口 IP 自动生成 `locale / timezone / Accept-Language`，并传给 CloakBrowser 与 Playwright context。
- 如果你通过项目代理池使用代理，请在 `config/proxy.py` 的 `PROXY_POOL` 填写代理；如果你使用系统代理/VPN，也会按当前实际出口 IP 自动定位。
- 免费版没有在项目侧限制窗口数；本项目每个注册任务会启动一个 CloakBrowser 实例，即一个实例一套指纹。
- WebUI 中，`Codex授权驱动` 位于「CPA / Codex」分组，对应 `config/codex.py` 的 `CODEX_OAUTH_DRIVER`。

#### 使用协议注册

```python
REGISTRATION_DRIVER = "protocol"
```

协议注册会使用 `curl_cffi`、Sentinel/PoW、代理池等配置。

#### 使用 Browser Use Cloud 注册

```python
REGISTRATION_DRIVER = "browser_use"
```

并在 `config/browser_use.py` 或 WebUI「配置 → Browser Use」填写：

```python
BROWSER_USE_API_KEY = "你的 Browser Use API Key"
BROWSER_USE_PROXY_COUNTRY_CODE = "jp"   # 可选：us/sg/de...
BROWSER_USE_USE_PROXY = True
BROWSER_USE_FAST_MODE = True       # 推荐开启：减少 Browser Use 额外等待
BROWSER_USE_LOG_TIMING = True      # 输出阶段耗时日志，方便定位慢点
BROWSER_USE_SESSION_TIMEOUT = 240  # Browser Use keepAlive/timeout，单位分钟；创建远端浏览器时保持活跃更久
```

如希望注册成功后也用 Browser Use 自动跑 Codex OAuth：

```python
ENABLE_CODEX_AUTO = True
CODEX_OAUTH_DRIVER = "browser_use"
# 或 CODEX_OAUTH_DRIVER = "same_as_registration"，当 REGISTRATION_DRIVER="browser_use" 时自动跟随
```

依赖：

```bash
uv pip install playwright --python .venv/bin/python
# 或
pip install playwright
```

说明：

- Browser Use 走远端 stealth Chromium，通过 Playwright `connect_over_cdp` 控制。
- `BROWSER_USE_SESSION_TIMEOUT=240` 会在 Browser Use 创建/连接远端浏览器时设置较长 keepAlive（connect URL 的 `timeout` 参数，单位分钟），避免等待邮箱 OTP、短信或 callback 时云端会话提前回收；代码会限制到 `1~240`。
- 如果第一次进入邮箱验证码页且邮箱里实际已有验证码，但程序没取到，通常是 Outlook 取件链路抖动：Graph TLS/REST/IMAP 某一轮失败、短轮询切片过短、或 `after_ts` 过滤边界过紧。Browser Use 驱动已放宽 Outlook 单轮取件切片、提前记录验证码过滤时间，并会在等待邮箱 OTP 超时后尝试点击重发继续等待；重发入口使用 DOM 结构/位置/属性启发式定位，不依赖页面文案或 OCR/文字识别。可在「邮箱 / OTP」把 `OTP_MAX_WAIT` 调大到 `180~240`，`OUTLOOK_FETCH_MODE` 优先用 `auto`。
- Outlook 取件日志会显示验证码来源：`source=graph`、`source=outlook_rest`、`source=imap_new`、`source=imap_entra_outlook`、`source=remote_graph` 或 `source=remote_imap`，便于判断是哪条链路成功取码。
- `BROWSER_USE_FAST_MODE=True` 会跳过大部分人工节奏等待；`BROWSER_USE_LOG_TIMING=True` 会打印连接、打开页面、邮箱、OTP、手机、callback 等阶段耗时。
- 支持作为 Codex OAuth 授权驱动：`CODEX_OAUTH_DRIVER="browser_use"`，可完成授权页面、邮箱 OTP、手机短信验证与 callback 捕获。
- Proxy.vn rotating lease 会通过 Browser Use Cloud 的 custom proxy session 传入具体 IP/端口；若配置为 Skyvern，当前 Cloud API 不支持该类 custom proxy，程序会直接报错而不会偷偷改走其他出口。
- 适合不想安装本机 Roxy、又想要 session 隔离 + 云端代理的场景。
- 免费额度/并发以 Browser Use 官方定价页为准。

---

### Roxy Profile Manager độc lập

WebUI có tab **Roxy Profiles** riêng, không dùng chung lifecycle với đăng ký hoặc Codex OAuth. Manager dùng một `ROXY_API_TOKEN` + `ROXY_WORKSPACE_ID` để tạo, sửa, liệt kê, mở/đóng và quản lý nhiều profile do chính manager sở hữu.

Hai chế độ mở luôn tách biệt:

- **Mở Roxy chuẩn** gọi `/browser/open` cho profile remote đang active; giữ control plane/fingerprint của Roxy.
- **Mở local thử nghiệm** giải mã full-folder artifact v2 vào staging rồi chạy `RoxyChrome.exe --user-data-dir=<staging>` qua loopback CDP. Chế độ này chỉ cam kết `browser_state_only`, không cam kết fingerprint/proxy/sync tương đương Roxy.

Artifact:

- `.rpa` v1 chỉ chứa metadata đã mã hóa và không thể mở local.
- `.rpa2` v2 chứa snapshot browser folder đã mã hóa AES-256-GCM, manifest SHA-256 theo file và source core version.
- Archive remote luôn là soft-delete vào Roxy Trash (`isSoftDelete=true`), chỉ thực hiện sau khi `.rpa2` đã decrypt/verify thành công. Manager không tự động permanent-delete.
- Khi đóng local, manager checkpoint staging thành `.rpa2` mới; nếu checkpoint lỗi, staging được giữ và state chuyển `OFFLINE_UNVERIFIED`.
- Khi mở Roxy chuẩn, manager cố capture một browser-state signature đã băm (platform/language/timezone/screen/WebGL/GPU). `.rpa2` chỉ mang hash này nếu capture thành công; local open sau đó báo `matched`, `mismatched` hoặc `unknown`. Kết quả này không phải cam kết fingerprint-equivalent.
- Catalog hiện dùng schema v3 và fail-closed với database schema cũ; không tự migrate hoặc xóa runtime catalog.

Cấu hình trong `.env`:

```env
ROXY_PROFILE_ARCHIVE_KEY=
ROXY_PROFILE_OFFLINE_OPEN_SUPPORTED=true
ROXY_PROFILE_ROXY_CHROME_PATH=
ROXY_PROFILE_CACHE_ROOT=
ROXY_PROFILE_OFFLINE_STAGING_DIR=
ROXY_PROFILE_ALLOW_CORE_VERSION_MISMATCH=false
```

`ROXY_PROFILE_ARCHIVE_KEY` phải là URL-safe base64 giải mã đúng 32 byte và không được trả qua API/UI. Manual disposable-profile gate trên target RoxyChrome đã hoàn tất với parity `matched`; local open mặc định bật nhưng vẫn có thể tắt bằng biến môi trường và luôn được gắn nhãn `browser_state_only`.

---

### 3. 配置代理

编辑 `config/proxy.py`：

```python
PROXY_POOL = [
    "http://user:pass@host:port",
]
```

Roxy 一号一环境开启 `ROXY_CREATE_USE_PROXY_POOL=True` 时，会从这里随机取代理写入 Roxy Profile。

---

### 4. 配置 Codex OAuth

如不需要 Codex，关闭：

```python
ENABLE_CODEX_AUTO = False
```

如需要自动授权：

```python
ENABLE_CODEX_AUTO = True
# config/codex.py
CODEX_OAUTH_DRIVER = "browser_use"  # 可选 protocol / roxy / cloak / browser_use / skyvern / same_as_registration
```

接码配置在 `config/codex.py`：

```python
SMS_PROVIDER = "hero"     # 可选 grizzly / viotp / hero / l / h
SMS_MAX_RETRIES = 10
SMS_CODE_WAIT = 120
SMS_POLL_INTERVAL = 5

# ViOTP dùng cấu hình riêng; serviceId được JNMBrowser chọn từ /service/getv2.
VIOTP_API_BASE = "https://api.viotp.com"
VIOTP_API_TOKEN = "你的 ViOTP token"
VIOTP_SERVICE_ID = "1234"   # OpenAI | ChatGPT tại thời điểm kiểm tra
VIOTP_COUNTRY = "vn"
VIOTP_NETWORK = "VINAPHONE"

# GrizzlySMS 继续使用通用字段：
SMS_API_KEY = "你的 GrizzlySMS key"
SMS_SERVICE = "openai"
SMS_COUNTRY = "国家代码"

# HeroSMS 使用 SMS-Activate-compatible API；OpenAI / ChatGPT service code 为 dr。
# HERO_SMS_COUNTRY=auto 时按实时 cost 从低到高扫描；sticky country 只在同价位优先，较贵 sticky 等低价候选失败后再试；max price 只是硬上限。
HERO_SMS_API_BASE = "https://hero-sms.com/stubs/handler_api.php"
HERO_SMS_API_KEY = "你的 HeroSMS API key"
HERO_SMS_SERVICE = "dr"
HERO_SMS_COUNTRY = "auto"
HERO_SMS_MAX_PRICE = "0.1"
HERO_SMS_COUNTRY_MIN_ATTEMPTS = 4
HERO_SMS_COUNTRY_HIGH_FAILURE_RATE = 0.75

# 若 SMS_PROVIDER="h"，H 固定复用：
#   SMS_SERVICE -> H projectId
#   SMS_COUNTRY -> H country
H_API_BASE = "http://localhost:8788"
H_ADMIN_AUTH_CODE = "你的H后台授权码"
```

ViOTP 在 `/session/getv2` 返回完成或过期状态；其公开 API 没有主动 `cancel` / `complete` 接口，因此程序失败换号时只清理本地会话记录并等待平台自动过期。

HeroSMS 在 `HERO_SMS_COUNTRY=auto` 时先调用 `getPrices&service=dr`，过滤库存大于 0 且 `cost <= HERO_SMS_MAX_PRICE` 的全部国家；`HERO_SMS_MAX_PRICE` 是唯一的硬价格上限，当前示例为 `0.1`，不会自动超过该值，也不是一个起始价。候选直接按当前 offer 的实际 `cost` 从低到高排序，有多少个低于 `0.05` 就按实际价格逐个尝试，再继续到 `0.1`，不使用固定价格档位；多 worker 只在完全相同的 cost 中轮换，避免并发分配打乱低价优先顺序。每个 worker lane 都有独立的 country 记忆：本 lane 最近成功的 country 若仍有库存且与当前最低价相同，则优先复用；如果它更贵，则先让更低价候选尝试，低价候选失败后才回到 sticky country；任何 `NO_NUMBERS`/`WRONG_MAX_PRICE` 都会继续扫描当前候选池，直到取号成功或候选耗尽。每个 country 的 Codex 手机验证成功/失败，以及取号时的即时无库存结果都会写入 `app_state.sqlite3`，health 会跨不同价格 profile 汇总。单次收不到 OTP 或 verify 错误不会立即高风险封禁，默认累计至少 4 次且失败率达到 75% 才降为低优先级兜底 country；后续成功会清除该 country 的最近失败标记并恢复本 lane 复用。拿到验证码后使用 `setStatus=6` 完成，失败时使用 `setStatus=8` 取消。价格和库存是动态数据，已成功 country 仍会重新经过当前价格/库存筛选，不能把某个 country ID 视为永久最低价。
在 Cloak/Roxy 浏览器流程中，Hero 返回的 E.164 号码还用于自动选择 OpenAI 表单的对应国家/区号，避免号码前缀和 country selector 不一致导致 `whatsapp_channel` 或号码发送失败。

CPA 授权地址来源：

```python
CODEX_AUTH_URL_SOURCE = "cpa"
CPA_MANAGEMENT_URL = "你的CPA管理地址"
CPA_MANAGEMENT_KEY = "你的CPA管理密钥"
```

sub2api 导出默认值在 WebUI「配置」的 Codex 分组设置，也可写入 `.env`：

```dotenv
SUB2API_GROUP_IDS=14
SUB2API_PRIORITY=1
SUB2API_MODEL=gpt-5.4-mini,gpt-5.5,gpt-5.6-luna,gpt-5.6-terra
```

`SUB2API_GROUP_IDS` 每行填写一个分组 ID。`SUB2API_MODEL` 支持逗号或换行分隔多个 model；每个 model 会生成一个 `model -> model` 映射。分组和优先级会随 OAuth callback、Agent Token 导入及 Codex 补跑一起发送，无需再逐个账号手动配置。

---

## 使用方式

## WebUI 推荐方式

推荐使用项目根目录单脚本后台管理：

```bash
./webui.sh start      # 启动
./webui.sh stop       # 关闭
./webui.sh restart    # 重启
./webui.sh status     # 状态
./webui.sh logs       # 查看实时日志
```

脚本默认启动 `http://127.0.0.1:5000`，日志写入 `logs/webui.log`，PID 写入 `run/webui.pid`。

可通过环境变量调整：

```bash
PORT=8000 OPEN_BROWSER=1 ./webui.sh start
HOST=0.0.0.0 PORT=5000 ./webui.sh restart
AUTH_CODE=你的授权码 ./webui.sh start
```

也可以直接前台启动：

启动：

```bash
python web.py --open-browser
```

默认地址：

```text
http://127.0.0.1:5000
```

可指定端口：

```bash
python web.py --port 8000 --open-browser
```

允许局域网访问：

```bash
python web.py --host 0.0.0.0 --port 5000
```

WebUI 页面说明：

| 页面 | 功能 |
|---|---|
| 注册 | 设置注册数量、线程数，启动批量注册，查看任务和日志 |
| 账号 | 查看账号、复制 token、补跑 Codex、批量删除账号 |
| Codex 授权 | 查看/下载/删除 SQLite 中的 Codex 凭证 |
| 邮箱池 | 导入邮箱、筛选来源、标记可用/失败、删除邮箱 |
| 配置 | 修改运行配置并热加载，含 Roxy、Codex、邮箱、代理、人工节奏等 |

### 线程数说明

- 注册线程数在每次点击「开始注册」时读取。
- 如果线程数和上次不同，新提交任务会使用新线程池。
- 旧线程池里已经排队/运行的任务会继续跑完，不会被强制取消。
- Codex 批量补跑每次都会按本次提交的补跑线程数创建独立线程池。

---

## CLI 使用方式

注册 1 个：

```bash
python main.py
```

批量注册 10 个，3 线程：

```bash
python main.py -n 10 --workers 3 --continue-on-fail
```

详细日志：

```bash
python main.py -n 1 --verbose
```

参数：

| 参数 | 说明 | 默认 |
|---|---|---|
| `-n, --count` | 注册数量 | 1 |
| `--workers` | 并发线程数 | 1 |
| `--delay` | 每次注册结束后的间隔秒数 | 0 |
| `--continue-on-fail` | 单个失败后继续 | False |
| `--verbose` | DEBUG 日志 | False |

---

## Codex 补跑

WebUI 账号页可单个或批量补跑 Codex。

CLI 单独补跑：

```bash
python tools/test_codex_oauth.py --email <已注册邮箱> --verbose
```

补跑会消耗：

- 1 次邮箱 OTP
- 1 个接码号码

补跑日志在：

```text
注册日志/codex-retry-邮箱.log
```

---

## 注册密码说明

所有 browser 注册驱动（Roxy、Cloak、Browser Use、Skyvern）如果遇到新版流程：

```text
/create-account/password
```

会自动设置密码。

密码来源：

1. 优先使用 `config/register.py`：

```python
REGISTER_PASSWORD = "你的固定密码"
```

2. 如果为空，自动生成 14 位强密码，包含大写、小写、数字、符号。

保存位置：

- 账号 `extra_json.registration_password`
- SQLite `accounts.payload` 中的 `extra_json.registration_password`

注意：账号表里的 `password` 字段仍用于 Outlook 邮箱素材密码，不会被 OpenAI 注册密码覆盖。

---

## 重要配置文件

| 文件 | 说明 |
|---|---|
| `config/roxybrowser.py` | 注册驱动、Roxy API、Roxy 环境生命周期 |
| `config/cloakbrowser.py` | CloakBrowser 无头/humanize/geoip/语言时区/指纹 seed |
| `config/codex.py` | Codex OAuth、授权驱动、CPA 管理接口、接码平台 |
| `config/email.py` | 邮箱来源、OTP 轮询、QQ IMAP、域名邮箱、Cloudflare Worker 临时邮箱 |
| `config/proxy.py` | 代理池 |
| `config/register.py` | 默认邮箱、密码、显示名 |
| `config/twofa.py` | 2FA 开关 |
| `config/humanize.py` | 随机停顿/人工节奏 |
| `config/flow_trigger.py` | 注册成功后触发 Flow |
| `config/browser.py` | 协议模式浏览器指纹 |
| `config/openai_protocol.py` | OpenAI OAuth/Sentinel 参数 |

WebUI 配置页保存后会调用热加载；Roxy、Codex、邮箱、代理、人工节奏等常用项可立即生效。

---

## 数据与产物

| 路径 | 内容 |
|---|---|
| `app_state.sqlite3` | 迁移后的唯一运行时数据库，包含核心业务表和 provider state |
| `turb.sqlite3` | 迁移前 origin；仅作为受控离线 migration input，保留作 rollback 证据 |
| 旧 JSON/TXT/Codex 文件 | 导出或 legacy 输入；central runtime 不会隐式导入 |
| `注册日志/` | 注册任务日志、Codex 补跑日志 |

---

## 当前主流程

### Roxy 注册流程

```text
创建/打开 Roxy Profile
  ↓
打开 chatgpt.com/auth/login
  ↓
按 DOM 技术属性定位邮箱输入框，避免误点 Google/Apple/Microsoft
  ↓
提交邮箱表单
  ↓
如进入 create-account/password：设置密码并提交
  ↓
等待邮箱验证码页
  ↓
读取邮箱 OTP 并提交
  ↓
如进入 about-you/profile：填写姓名 + 年龄或生日
  ↓
进入 ChatGPT，读取 /api/auth/session accessToken
  ↓
可选 2FA
  ↓
可选 Codex OAuth
  ↓
保存账号到 SQLite
  ↓
关闭/删除 Roxy Profile
```

### Codex Roxy 授权流程

```text
获取 Codex 授权地址（CPA 或 local PKCE）
  ↓
Roxy 打开授权页
  ↓
邮箱登录 + 邮箱 OTP
  ↓
手机号验证：取号 → 填号 → 发送 → 等短信 → 填 OTP
  ↓
等待 consent/workspace/callback
  ↓
提交 callback 给 CPA 或本地换 token
  ↓
保存 Codex 凭证到 SQLite
```

---

## 常见问题

### 配置保存后没生效？

WebUI 配置页保存后会热加载。Codex 补跑线程启动前也会重新热加载一次配置。

如果你直接手改 `config/*.py`，CLI 进程需要重启；WebUI 建议在配置页修改。

### Roxy 无头保存后仍弹窗口？

检查：

```python
ROXY_OPEN_HEADLESS = True
```

并确认 Roxy 版本支持 `/browser/open` 的 `headless` 参数。日志会打印实际传入的 `headless`。

### 出口 IP 不是日本时点到 Google 登录？

当前 Roxy 注册邮箱入口已改为只按 DOM 技术属性定位，并排除三方登录按钮。不会再靠按钮文字匹配“Continue”。

### Codex 显示 `Check your phone` 被误判失败？

已兼容：`Check your phone / Enter the verification code...` 会识别为手机验证码页，进入等待短信验证码流程。

### 手机 OTP 提交后日志曾显示失败，但后面成功？

已修复：提交手机 OTP 后会等待页面离开手机号流程或 callback，不再 3 秒后用旧页面文案误判失败。

### Codex 失败但注册成功怎么办？

账号会保存，Codex 状态会标记失败。可以在 WebUI 账号页点击补跑，或使用：

```bash
python tools/test_codex_oauth.py --email <邮箱> --verbose
```

### Cloudflare Worker 邮箱怎么配？

将 `EMAIL_SOURCE` 设为 `cloudflare`，并配置 `CLOUDFLARE_API_BASE` 等（见上文「Cloudflare Worker 临时邮箱」）。  
注意与 `cloudflare_domain`（QQ IMAP 转发）不是同一来源。

### 没有接码平台能注册吗？

可以。关闭：

```python
ENABLE_CODEX_AUTO = False
```

注册主流程不依赖接码，Codex 自动授权才需要。

---

## 项目结构

```text
.
├── main.py                         # CLI 入口
├── web.py                          # WebUI 入口
├── config/                         # 配置
│   ├── roxybrowser.py              # RoxyBrowser 注册/Codex 驱动
│   ├── cloakbrowser.py             # CloakBrowser 注册驱动配置
│   ├── browser_use.py              # Browser Use Cloud 配置
│   ├── codex.py                    # Codex OAuth / 授权驱动 / CPA / 接码
│   ├── email.py                    # 邮箱来源/OTP
│   ├── proxy.py                    # 代理池
│   ├── register.py                 # 默认注册信息
│   └── ...
├── core/
│   ├── roxy_registration.py        # Roxy / 浏览器注册页面流程
│   ├── cloakbrowser_registration.py # Cloak 注册入口
│   ├── cloakbrowser_driver.py      # Cloak Playwright→Selenium 风格适配层
│   ├── browser_use_registration.py # Browser Use + Playwright 注册流程
│   ├── browser_use_client.py       # Browser Use CDP 客户端
│   ├── roxy_codex_oauth.py         # Roxy / Cloak 浏览器 Codex OAuth 页面流程
│   ├── roxybrowser_client.py       # Roxy API 客户端
│   ├── registration_service.py     # WebUI 注册线程池
│   ├── codex_oauth.py              # Codex 协议/Roxy/Cloak 调度
│   ├── email_provider.py           # 邮箱来源调度
│   ├── cf_temp_mail_client.py      # Cloudflare Worker 临时邮箱
│   ├── sms_provider.py             # 接码平台
│   ├── account_export.py           # 注册后处理与 SQLite 保存
│   └── db.py                       # SQLite 数据库 persistence layer
├── webui/
│   ├── app.py                      # Flask API
│   ├── config_editor.py            # 配置读写/热加载
│   └── templates/index.html        # 单页控制台
├── sentinel/
│   ├── sdk.js
│   └── sentinel-runner.js
├── tools/
│   └── test_codex_oauth.py         # Codex 单独补跑
└── L_API.md                        # 本地 L 接码接口说明
```

---

## 使用建议

- 日常批量使用 WebUI，不建议直接同时开多个 CLI 进程。
- 注册线程数建议不超过可用代理数。
- Roxy 一号一环境建议保持开启，降低环境污染。
- 调试页面问题时可临时设置：

```python
ROXY_KEEP_BROWSER_OPEN = True
ROXY_OPEN_HEADLESS = False
```

- 调试完再改回自动关闭/删除环境。

---

## 🙏 致谢

- [LINUX DO](https://linux.do) — 社区交流与用户反馈
- [RoxyBrowser](https://roxybrowser.cn/invite/NvH4Jx) — 免费提供 5 个窗口
- [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) — Stealth Chromium / Playwright 自动化指纹浏览器支持
- [browser-use](https://github.com/browser-use/browser-use) — Browser Use Cloud / Playwright CDP 云端浏览器能力支持
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) — Codex OAuth 凭证格式参考
- [curl_cffi](https://github.com/yifeikong/curl_cffi) — 底层 HTTP 库，提供 TLS 指纹 impersonate 能力

---

## License

MIT
