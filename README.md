# nvidia-register

自动注册 NVIDIA BUILD 账号并创建 api key

## 功能特点

- **全自动流程**：创建临时邮箱 → 注册 → 过验证码 → 创建组织 → 建 Key → 记录 CSV，全流程自动化
- **批量注册**：支持单次注册多个账号，交互式询问或 `-n` 参数直接指定
- **验证码**：支持 手动过验证(`manual`)和全自动过验证(`yescaptcha`、`captcharun`)两种hCaptcha 处理方式
- **邮箱服务**：支持 `cloudflare_temp_email`（自部署）、 `duckmail`（DuckMail API）和 `mailnest`（Outlook 临时邮箱）
- **随机密码**：每次注册自动生成 12 位密码（大小写 + 数字）
- **自动跳过手机验证**：利用组织名注册跳过手机号要求，并创建长效 API Key
- **CSV 记录**：每次注册成功立即追加 `email,password,apikey` 到 CSV 文件
- **优雅退出**：`Ctrl+C` 完成当前账号后安全退出

## 项目结构

```
├── main.py              # 主入口 + 流程编排
├── config.py            # 配置加载（config.toml）
├── email_providers.py   # 临时邮箱服务抽象层
├── captcha.py           # 验证码处理
├── passwords.py         # 随机密码生成
├── records.py           # CSV 记录写入
├── config.toml          # 配置文件
└── config.toml.example  # 配置示例
```

## 前置条件

- Python 3.11+
- Chromium 浏览器（Playwright 自动下载）
- **临时邮箱服务**（当前支持 `cloudflare_temp_email` 自部署 和 `duckmail`）
- （可选）[YesCaptcha](https://yescaptcha.com/i/57yzUt) / [CaptchaRun](https://captcha-run.com/sso?inviter=ad8fbc2f-9721-430e-87a9-1898fa0177b4) 密钥（用于全自动过 hCaptcha）

## 安装

```bash
pip install -r requirements.txt
playwright install chromium
```

## 配置

```bash
# 生成配置文件模板
python main.py --init
```

编辑生成的 `config.toml`：

```toml
email_provider = "cloudflare_temp_email"

[cloudflare_temp_email]
api_url = "https://mail.your-server.com"
admin_auth = "your_admin_key"
domain = "your-domain.com"

[duckmail]
api_url = "https://api.duckmail.sbs"
domain = "duckmail.sbs"
api_key = ""

[mailnest_temp_email]
api_key = ""
project_code = "nvidia001"

[captcha]
mode = "manual" # manual | yescaptcha | captcharun
yescaptcha_client_key = ""
yescaptcha_api_url = "https://api.yescaptcha.com"
captcharun_token = ""
captcharun_api_url = "https://api.captcha-run.com"
poll_interval_seconds = 3
timeout_seconds = 180

[nvidia]
output_csv = "accounts.csv"
key_name = "api"
account_name = "NVIDIA Build"
key_expiry_date = "2126-05-08T08:00:00Z"

[browser]
headless = false
close_delay_seconds = 5
```

| 配置项 | 说明                                                                              |
|--------|---------------------------------------------------------------------------------|
| `email_provider` | 临时邮箱服务类型（支持 `cloudflare_temp_email` / `duckmail`）                               |
| `cloudflare_temp_email.api_url` | 邮箱服务 API 地址                                                                     |
| `cloudflare_temp_email.admin_auth` | 邮箱服务管理员密钥                                                                       |
| `cloudflare_temp_email.domain` | 邮箱域名                                                                            |
| `duckmail.api_url` | DuckMail API 地址（默认 `https://api.duckmail.sbs`）                                  |
| `duckmail.domain` | DuckMail 邮箱域名，例如 `duckmail.sbs` 或你的私有域名                                         |
| `duckmail.api_key` | DuckMail 私有域 API Key，使用公共域名时可留空                                                 |
| `mailnest_temp_email.api_key` | MailNest 的 API Key，获取页面 `https://mailnest.top/account`                          |
| `mailnest_temp_email.project_code` | MailNest 中英伟达的项目代码，默认为 `nvidia001`，可以直接使用。获取页面 `https://mailnest.top/buy-email` |
| `captcha.mode` | `manual` 手动过验证，`yescaptcha` 使用 YesCaptcha API，`captcharun` 使用 CaptchaRun API    |
| `captcha.yescaptcha_client_key` | YesCaptcha 客户端密钥（mode=yescaptcha 时必填）                                           |
| `captcha.yescaptcha_api_url` | YesCaptcha API 地址（默认 `https://api.yescaptcha.com`）                              |
| `captcha.captcharun_token` | CaptchaRun Authorization Token（mode=captcharun 时必填）                             |
| `captcha.captcharun_api_url` | CaptchaRun API 地址（默认 `https://api.captcha-run.com`）                             |
| `captcha.poll_interval_seconds` | 验证码结果轮询间隔（秒）                                                                    |
| `captcha.timeout_seconds` | 验证码等待超时时间（秒）                                                                    |
| `nvidia.output_csv` | 记录输出 CSV 文件路径                                                                   |
| `nvidia.key_name` | API Key 名称                                                                      |
| `nvidia.account_name` | 创建组织账户时填入的名称（用于跳过手机验证）                                                          |
| `nvidia.key_expiry_date` | API Key 过期时间（默认 ~100 年）                                                         |
| `browser.headless` | 是否无头模式运行浏览器                                                                     |
| `browser.close_delay_seconds` | 完成后浏览器关闭延迟秒数                                                                    |

## 使用

```bash
# 交互式询问注册数量
python main.py

# 直接指定注册数量（不询问）
python main.py -n 5
python main.py --count 3
```

批量注册时每个账号使用独立的浏览器会话，间隔 5 秒。`Ctrl+C` 优雅退出：完成当前正在注册的账号后停止，显示成功/失败汇总。

每次注册成功会自动追加记录到 `accounts.csv`：

```csv
email,password,apikey
nv12345678@your-domain.com,aB3dE5fG7hI9,nvapi-xxxx...
```

## 注册流程

```
build.nvidia.com (填邮箱) → login.nvgs.nvidia.com (填密码 + hCaptcha)
→ 验证码页 (键盘输入) → 同意页 (提交) → 创建组织 (跳过手机验证)
→ NGC API (建 Key) → CSV 记录
```

## 扩展邮箱服务

当前已支持 `cloudflare_temp_email` 和 `duckmail`，后续仍可通过实现 `TempEmailProvider` 协议扩展：

```python
class TempEmailProvider(Protocol):
    def create_inbox(self, name: str) -> TempEmailInbox: ...
    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None: ...
```

在 `email_providers.py` 中添加新 Provider 并注册到 `build_email_provider()` 即可。

## 注意事项

- hCaptcha **手动模式**必须人工完成验证
- 注册包含验证码轮询（最长 3 分钟）
- 浏览器窗口会在完成后自动关闭（可配置延迟）
- 批量注册时每个账号独立浏览器会话，互不影响
- 第二次 `Ctrl+C` 强制退出