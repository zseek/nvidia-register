#!/usr/bin/env python3
"""
nvidia-register — 注册 build.nvidia.com 账号并创建 AI_PLAYGROUNDS_KEY

完整流程（基于真实页面链路，全部实测确认）：
  创建临时邮箱 → build.nvidia.com 填邮箱 → create-account 页填密码 + 过 hCaptcha
  → 验证码页真实键盘输入 → 跳过通行密钥引导页 → 同意/快完成页
  → 创建组织跳过手机验证 → 调 NGC API 建 key → 记录到 CSV

用法:
  pip install -r requirements.txt
  playwright install chromium

  python main.py --init       # 生成 config.toml 配置文件
  # 编辑 config.toml 填入你的信息
  python main.py              # 交互式询问注册数量
  python main.py -n 5         # 直接注册 5 个账号（不询问）
  python main.py --count 3    # 同上

配置文件: config.toml（见 config.toml.example 或 --init 生成）
Ctrl+C  优雅退出：完成当前正在注册的账号后退出。
"""

import asyncio
import json
import signal
import sys
import time

from playwright.async_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from config import AppConfig, describe_config, init_config, load_config
from captcha import build_captcha_solver, reset_captcha_state, start_capturing_sitekey
from email_providers import TempEmailProvider, build_email_provider
from passwords import generate_password
from records import append_account_record

# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def _parse_count(argv: list[str]) -> int | None:
    """从命令行参数解析 -n / --count 的值。"""
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] in ("-n", "--count") and i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                print(f"Error: {args[i]} requires a number, got: {args[i + 1]}")
                sys.exit(1)
        i += 1
    return None

def main_cli() -> None:
    args = sys.argv[1:]

    if args and args[0] == "--init":
        init_config()
        return

    config = load_config()

    count = _parse_count(sys.argv)
    if count is None:
        # 交互式询问
        try:
            raw = input("注册账号数量 (默认 1): ").strip()
            count = int(raw) if raw else 1
        except (ValueError, EOFError):
            count = 1
    if count < 1:
        print("数量必须 >= 1")
        return

    try:
        asyncio.run(run(config, count))
    except KeyboardInterrupt:
        print("\n\nInterrupted. Goodbye!")

# ---------------------------------------------------------------------------
#  注册流程
# ---------------------------------------------------------------------------

# Ctrl+C 优雅退出标志
_shutdown = False

def _handle_sigint():
    global _shutdown
    if _shutdown:
        # 第二次 Ctrl+C → 强制退出
        print("\n\nForce exit!")
        sys.exit(1)
    _shutdown = True
    print("\n\nCtrl+C received. Will exit after current account finishes...")

async def run(config: AppConfig, count: int = 1) -> None:
    email_provider = build_email_provider(config)
    captcha_solver = build_captcha_solver(config.captcha)

    print("=" * 60)
    print("NVIDIA Register + API Key Creator")
    print("-" * 60)
    describe_config(config)
    print(f"  注册数量: {count}")
    print("=" * 60)
    print("  (Ctrl+C 优雅退出：完成当前账号后停止)\n")

    # 注册信号处理（跨平台）
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, _handle_sigint)
    except NotImplementedError:
        # Windows 不支持 add_signal_handler，用 signal.signal 兜底
        signal.signal(signal.SIGINT, lambda *_: _handle_sigint())

    success_count = 0
    fail_count = 0

    async with async_playwright() as p:
        for i in range(count):
            if _shutdown:
                break

            print(f"\n{'#' * 60}")
            print(f"# 账号 {i + 1} / {count}")
            print(f"{'#' * 60}")

            api_key = await _register_one(p, config, email_provider, captcha_solver)
            if api_key:
                success_count += 1
            else:
                fail_count += 1

            # 非最后一个账号时，间隔一下避免频率限制
            if i < count - 1 and not _shutdown:
                print("\n  等待 5 秒后注册下一个...")
                await asyncio.sleep(5)

    # 汇总
    print("\n" + "=" * 60)
    print(f"完成! 成功: {success_count}, 失败: {fail_count}, 总计: {success_count + fail_count}")
    print("=" * 60)

async def _register_one(
    p,
    config: AppConfig,
    email_provider: TempEmailProvider,
    captcha_solver,
) -> str | None:
    """单个账号的完整注册流程。返回 api_key 或 None。"""
    password = generate_password(12)
    reset_captcha_state()  # 重置 sitekey 缓存，确保每个账号独立
    browser = await p.chromium.launch(
        headless=config.browser.headless,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    page = await browser.new_page(viewport={"width": 1280, "height": 800})

    try:
        # 1. 创建临时邮箱
        inbox_name = "nv" + str(int(time.time()))[-8:]
        try:
            inbox = email_provider.create_inbox(inbox_name)
        except Exception as exc:
            print(f"  Email creation failed: {exc}")
            return None
        print(f"\n[1] Email: {inbox.address}")

        # 2. 打开 build.nvidia.com，接受 cookie 弹窗
        print("[2] Opening build.nvidia.com...")
        await page.goto("https://build.nvidia.com/", wait_until="domcontentloaded", timeout=90000)
        await _accept_cookie_banner(page)

        # 3. 点击 Login 打开登录弹窗
        print("[3] Open sign-in modal...")
        if not await _open_signin_modal(page):
            print("  Login button not found")
            await _print_clickable_snapshot(page)
            return None

        # 4. 填邮箱 → Next（跳转到 login.nvgs.nvidia.com/v1/create-account）
        print("[4] Submit email...")
        start_capturing_sitekey(page)
        await _ensure_hcaptcha_hook(page)
        if not await _submit_email_step(page, inbox.address):
            print("  Failed at email step")
            await _print_clickable_snapshot(page)
            return None

        # 5. 注册（填密码 → 过 hCaptcha → 提交 → 验证码）
        ok = await register_account(page, inbox, password, email_provider, captcha_solver, config)
        if not ok:
            print("\nRegistration failed")
            return None

        # 6. 状态机处理注册后跳转，直到 session 有效并建 key
        api_key = await finalize_and_create_key(page, config)

        # 7. 记录到 CSV
        if api_key:
            append_account_record(
                path=config.nvidia.output_csv,
                email=inbox.address,
                password=password,
                api_key=api_key,
            )
            print(f"  Record saved to: {config.nvidia.output_csv}")
            print(f"\n  ✓ {inbox.address} → {api_key[:30]}...")
            return api_key
        else:
            print("\nRegistration succeeded but API Key creation failed")
            return None
    finally:
        await _close_browser(browser, config.browser.close_delay_seconds)

# ---------------------------------------------------------------------------
#  子流程
# ---------------------------------------------------------------------------

async def _accept_cookie_banner(page: Page) -> None:
    """OneTrust cookie 弹窗会用遮罩拦截点击，必须先接受。"""
    try:
        btn = page.locator("#onetrust-accept-btn-handler")
        await btn.wait_for(state="visible", timeout=8000)
        await btn.click()
        print("  cookie accepted")
        await page.locator("div.onetrust-pc-dark-filter").wait_for(state="hidden", timeout=5000)
    except Exception:
        pass
    await asyncio.sleep(1)

async def _open_signin_modal(page: Page) -> bool:
    """点击 header 的 Login，打开 signin 弹窗。

    实测：点 Login 后先出现临时弹窗，1~2 秒后页面自动刷新出真正有效弹窗。
    """
    try:
        login = page.get_by_role("button", name="Login").first
        await login.wait_for(state="visible", timeout=10000)
        await login.click()
    except Exception:
        pass

    # 等待邮箱输入框首次出现（第一个临时弹窗）
    try:
        await page.locator('input[name="email"]').first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass

    # 关键：等页面自动刷新出第二个有效弹窗。刷新会重建 DOM，
    # 等 email 输入框稳定（连续多次拿到同一个可交互输入框）后再返回。
    await _wait_for_stable_email_input(page)
    return await page.locator('input[name="email"]:visible').count() > 0

async def _wait_for_stable_email_input(page: Page, settle_seconds: float = 3.0) -> None:
    """等待 signin 弹窗自动刷新完成，直到可见 email 输入框稳定。"""
    deadline = time.time() + 15
    stable_since = None
    while time.time() < deadline:
        try:
            visible = await page.locator('input[name="email"]:visible').count()
        except Exception:
            visible = 0
        if visible >= 1:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= settle_seconds:
                return
        else:
            stable_since = None
        await asyncio.sleep(0.5)

async def _submit_email_step(page: Page, email: str) -> bool:
    """在有效 signin 弹窗填邮箱并点 Next，跳转到 create-account 页。"""
    email_input = page.locator('input[name="email"]:visible').first
    try:
        await email_input.wait_for(state="visible", timeout=15000)
    except Exception:
        return False

    await email_input.click()
    await email_input.press_sequentially(email, delay=50)
    await asyncio.sleep(0.3)

    next_btn = page.get_by_role("button", name="Next").filter(visible=True).first
    try:
        await next_btn.wait_for(state="visible", timeout=5000)
        for _ in range(20):
            if await next_btn.is_enabled():
                await next_btn.click()
                print("  Next clicked")
                break
            await asyncio.sleep(0.5)
        else:
            print("  Next stayed disabled")
            return False
    except Exception:
        return False

    try:
        await page.wait_for_url("**/login.nvgs.nvidia.com/**", timeout=20000)
        print(f"  navigated to: {page.url[:80]}")
    except Exception:
        await asyncio.sleep(5)
    return True

async def register_account(
    page: Page,
    inbox,
    password: str,
    email_provider: TempEmailProvider,
    captcha_solver,
    config: AppConfig,
) -> bool:
    """create-account 页：填密码 → 过 hCaptcha → 点 #register_button → 验证码页真实键盘输入。"""
    # [1/4] 等待密码字段并填写
    print("\n[1/4] Fill password...")
    try:
        await page.locator("#registration_password").wait_for(state="visible", timeout=30000)
    except PlaywrightTimeoutError:
        print("  password field never appeared")
        await _print_clickable_snapshot(page)
        return False

    await page.fill("#registration_password", password)
    await page.fill("#registration_passwordConfirm", password)
    # 保持登录（可选）
    try:
        checkbox = page.locator("#stay_signin_checkbox_v2-input")
        if await checkbox.count() > 0 and not await checkbox.is_checked():
            await checkbox.check()
    except Exception:
        pass
    print("  password OK")

    # [2/4] 过 hCaptcha 并提交。token 被后端拒绝时页面只弹一个笼统的错误提示
    # （表现为“网络问题”），仍停在 create-account 页，所以这里换一个新 token 重试。
    if not await _solve_captcha_and_submit(page, captcha_solver, config):
        return False

    # [3/4] 等待验证码邮件。同步轮询放到线程里跑，否则会阻塞事件循环，
    # 让 Playwright 在长达 timeout_seconds 的时间内无法处理页面事件。
    print("\n[3/4] Waiting for verification code email...")
    code = await asyncio.to_thread(
        email_provider.poll_verification_code,
        inbox,
        config.captcha.timeout_seconds,
    )
    if not code:
        print("  No verification code received")
        return False
    print(f"  Code: {code}")

    # 等验证码输入页出现（6 个 number 输入框）
    if not await _wait_for_verification_inputs(page, timeout_seconds=45):
        print("  verification inputs not detected")
        await _print_clickable_snapshot(page)
        return False

    # [4/4] 真实键盘输入验证码（React 受控组件，JS setValue 无效）
    # 验证码可能过期或输错，检测到错误提示时请求新验证码并重试
    print("\n[4/4] Type verification code...")
    for code_attempt in range(1, VERIFICATION_CODE_ATTEMPTS + 1):
        if code_attempt > 1:
            print(f"\n  请求新验证码... (第 {code_attempt}/{VERIFICATION_CODE_ATTEMPTS} 次)")
            if not await _request_new_verification_code(page):
                print("  无法请求新验证码")
                return False

            code = await asyncio.to_thread(
                email_provider.poll_verification_code,
                inbox,
                config.captcha.timeout_seconds,
            )
            if not code:
                print("  未收到新验证码")
                return False
            print(f"  新验证码: {code}")

        if not await _type_verification_code(page, code):
            print("  failed to type verification code")
            return False

        # 点"继续"提交验证码
        await _click_continue(page)
        await asyncio.sleep(3)

        # 检查是否有错误提示（验证码无效）
        if await _has_verification_code_error(page):
            print("  验证码无效，需要重新请求")
            continue

        # 验证码通过后 NVIDIA 会插入"创建通行密钥"引导页，先跳过它
        await _skip_passkey_prompt_if_present(page)
        print("\nRegistration submitted!")
        return True

    print("  验证码尝试次数已用尽")
    return False

# hCaptcha token 被后端拒绝时的重试次数（每次都会重新求解一个新 token）
CAPTCHA_SUBMIT_ATTEMPTS = 3
VERIFICATION_CODE_ATTEMPTS = 3

async def _solve_captcha_and_submit(page: Page, captcha_solver, config: AppConfig) -> bool:
    """过 hCaptcha 并点 #register_button，直到后端受理注册。

    NVIDIA 的注册接口是 POST /api/1/frontend/oauth/user/register，请求体里带
    validation.response（hCaptcha token）。token 校验不通过时接口返回非 2xx，
    前端只弹一个笼统的错误提示（看起来像网络问题），页面仍停在 create-account。
    这里直接监听该接口的状态码来判定成败，失败就换新 token 重来。
    """
    for attempt in range(1, CAPTCHA_SUBMIT_ATTEMPTS + 1):
        if attempt > 1:
            print(f"\n  重新求解 hCaptcha 并重试提交 (第 {attempt}/{CAPTCHA_SUBMIT_ATTEMPTS} 次)...")
            await _reset_hcaptcha_widget(page)

        try:
            solved = await captcha_solver.solve(page)
        except Exception as exc:
            # 打码平台不可用（网络超时、额度耗尽等）只应让当前账号失败
            print(f"  Captcha solver error: {exc}")
            solved = False
        if not solved:
            print("  Captcha failed")
            continue

        print(f"\n[2/4] Submit registration (#register_button)... (attempt {attempt})")
        register_result = await _click_register_and_wait_result(page)

        if register_result == "accepted":
            return True
        if register_result == "email_exists":
            # 邮箱已被占用，换 token 也没用
            print("  该邮箱已注册，放弃当前账号")
            return False
        print(f"  注册提交未被受理 ({register_result})")

    print(f"  连续 {CAPTCHA_SUBMIT_ATTEMPTS} 次提交均失败")
    await _print_clickable_snapshot(page)
    return False

async def _click_register_and_wait_result(page: Page) -> str:
    """点 #register_button 并等 user/register 接口响应，返回判定结果。

    返回值: "accepted" | "email_exists" | "rejected" | "no_response" | "not_clickable"
    """
    register_btn = page.locator("#register_button")
    try:
        await register_btn.wait_for(state="visible", timeout=15000)
        for _ in range(30):
            if await register_btn.is_enabled():
                break
            await asyncio.sleep(1)
        else:
            return "not_clickable"
    except Exception as exc:
        print(f"  #register_button not clickable: {exc}")
        return "not_clickable"

    # 先挂上等待器再点击，避免响应比监听更快
    response_waiter = asyncio.create_task(_wait_for_register_response(page))
    try:
        await register_btn.click()
    except Exception as exc:
        response_waiter.cancel()
        print(f"  #register_button click failed: {exc}")
        return "not_clickable"

    return await response_waiter

async def _wait_for_register_response(page: Page, timeout_seconds: int = 45) -> str:
    """等 POST .../oauth/user/register 的响应，按状态码判定注册是否被受理。"""
    try:
        response = await page.wait_for_event(
            "response",
            predicate=lambda resp: "oauth/user/register" in resp.url and resp.request.method == "POST",
            timeout=timeout_seconds * 1000,
        )
    except Exception:
        return "no_response"

    if response.status in (200, 201, 204):
        print(f"  register accepted ({response.status})")
        return "accepted"

    body = ""
    try:
        body = (await response.text())[:300]
    except Exception:
        pass
    print(f"  register rejected ({response.status}): {body}")
    if "CONFLICT" in body or "ALREADY" in body.upper():
        return "email_exists"
    return "rejected"

async def _reset_hcaptcha_widget(page: Page) -> None:
    """清掉上一次注入的 token 并重置 hCaptcha 组件，以便注入新 token。"""
    try:
        await page.evaluate(
            """() => {
                window.__hCaptchaInjectedToken = null;
                if (window.hcaptcha && typeof window.hcaptcha.reset === 'function') {
                    try { window.hcaptcha.reset(); } catch (_) {}
                }
            }"""
        )
    except Exception:
        pass
    await asyncio.sleep(2)

async def _wait_for_verification_inputs(page: Page, timeout_seconds: int) -> bool:
    """等待 6 个验证码数字输入框出现。"""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if await page.locator('input[type="number"]').count() >= 6:
            print("  verification inputs appeared")
            return True
        await asyncio.sleep(1)
    return False

async def _type_verification_code(page: Page, code: str) -> bool:
    """点第一个数字框后逐字符键盘输入，触发 React 状态。"""
    inputs = page.locator('input[type="number"]')
    count = await inputs.count()
    if count < 6:
        return False
    # 聚焦第一个框
    await inputs.first.click()
    for index, digit in enumerate(code[:count]):
        try:
            await inputs.nth(index).click()
        except Exception:
            pass
        await page.keyboard.type(digit, delay=80)
        await asyncio.sleep(0.15)
    await asyncio.sleep(0.5)
    return True

async def _click_continue(page: Page) -> bool:
    """点验证码/同意页的主推进按钮。

    页面语言随 locale 变化，所以中英文名称都试一遍。
    """
    clicked = await _click_button_by_names(page, ("继续", "提交", "Continue", "Submit"))
    if clicked:
        print(f"  clicked [{clicked}]")
        return True
    return False

async def _click_button_by_names(page: Page, names: tuple[str, ...], timeout_ms: int = 5000) -> str | None:
    """按可访问名依次尝试点击可见且可用的按钮，返回命中的名称。"""
    for name in names:
        try:
            button = page.get_by_role("button", name=name).filter(visible=True).first
            if await button.count() > 0 and await button.is_enabled():
                await button.click(timeout=timeout_ms)
                return name
        except Exception:
            continue
    return None

# 跳过通行密钥的二次确认对话框上的"确定"按钮
_CONFIRM_BUTTON_NAMES = ("确定", "OK", "Confirm", "Yes", "是")

async def _confirm_skip_dialog(page: Page) -> bool:
    """点掉"确定要跳过设置通行密钥吗"确认对话框。"""
    for _ in range(10):
        clicked = await _click_button_by_names(page, _CONFIRM_BUTTON_NAMES, timeout_ms=3000)
        if clicked:
            print(f"  passkey 确认对话框：已点击 [{clicked}]")
            return True
        await asyncio.sleep(0.5)
    return False

async def _has_verification_code_error(page: Page) -> bool:
    """检测页面上是否显示"验证码无效"的错误提示。"""
    try:
        # 检测错误提示文字（中英文）
        error_texts = ["验证码无效", "验证码错误", "invalid code", "incorrect code", "code is invalid"]
        for text in error_texts:
            if await page.get_by_text(text, exact=False).count() > 0:
                return True
        return False
    except Exception:
        return False

async def _request_new_verification_code(page: Page) -> bool:
    """点击"请求新验证码"或"重新请求新验证码"链接。"""
    try:
        # 尝试中英文链接文字
        link_texts = ["请求新验证码", "重新请求新验证码", "request new code", "resend code"]
        for text in link_texts:
            link = page.get_by_text(text, exact=False).filter(visible=True).first
            if await link.count() > 0:
                await link.click(timeout=5000)
                print(f"  已点击 [{text}]")
                await asyncio.sleep(2)
                return True
        return False
    except Exception as exc:
        print(f"  点击请求新验证码失败: {exc}")
        return False

async def _skip_passkey_prompt(page: Page) -> bool:
    """跳过"创建通行密钥"引导页（/v1/passkey/prompt-setup）。

    NVIDIA 在邮箱验证之后新增了这一步，页面上有两个按钮：
      #cancelSetupSelect_btn  → "稍后再说"（我们要点的）
      #setUpPasskey_btn       → "立即创建"（会拉起 WebAuthn，自动化环境无法完成）

    点"稍后再说"之后还会弹一个确认对话框（"您确定要跳过设置通行密钥吗？"），
    必须再点"确定"才会真正离开该页。
    """
    try:
        skip_btn = page.locator("#cancelSetupSelect_btn")
        if await skip_btn.count() > 0:
            await skip_btn.first.click(timeout=5000)
            print("  passkey 引导页：已点击 [稍后再说]")
            await _confirm_skip_dialog(page)
            return True
    except Exception:
        pass

    clicked = await _click_button_by_names(page, ("稍后再说", "Maybe later", "Not now", "Skip"))
    if clicked:
        print(f"  passkey 引导页：已点击 [{clicked}]")
        await _confirm_skip_dialog(page)
        return True

    print("  passkey 引导页：未找到跳过按钮")
    await _print_clickable_snapshot(page)
    return False

async def _skip_passkey_prompt_if_present(page: Page, wait_seconds: int = 15) -> bool:
    """等待并跳过可能出现的通行密钥引导页。没出现就直接返回。"""
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if "passkey" in page.url:
            return await _skip_passkey_prompt(page)
        await asyncio.sleep(1)
    return False

async def _wait_for_url_change(page: Page, current_url: str, wait_seconds: int) -> bool:
    """等页面自行跳走。返回 True 表示 URL 已变化。"""
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        await asyncio.sleep(1)
        if page.url != current_url:
            return True
    return False

async def _recover_from_navigation_error(page: Page) -> bool:
    """从 chrome-error 页恢复：先试重新加载，再退回 build.nvidia.com。

    此时账号已注册且 session cookie 已在浏览器里，重新打开站点即可让
    NGC 的 user-context 探测重新生效，不需要重新登录。
    """
    try:
        await page.reload(wait_until="domcontentloaded", timeout=30000)
        if not page.url.startswith("chrome-error"):
            return True
    except Exception:
        pass

    try:
        await page.goto("https://build.nvidia.com/", wait_until="domcontentloaded", timeout=60000)
        return not page.url.startswith("chrome-error")
    except Exception as exc:
        print(f"  恢复导航失败: {exc}")
        return False

async def _ensure_hcaptcha_hook(page: Page) -> None:
    """用 page.add_init_script 在所有后续页面加载前注册 hCaptcha 拦截器。

    hCaptcha render=explicit 模式下，Angular 组件在 hCaptchaLoad 回调中调用
    hcaptcha.render(el, {callback: onSuccess})。hcaptcha.render 内部存储回调。
    必须在 hCaptcha API 脚本创建 window.hcaptcha 时拦截，包装 render 方法，
    在回调注册时捕获到 window.__hCaptchaCallback。

    自动化浏览器里 hCaptcha 自身的挑战常常失败（组件显示"请再试一次"），
    随后会触发 expired-callback / error-callback，让 Angular 清掉我们注入的
    token 并重新禁用 #register_button。这里把这两个回调拦下来：token 注入后
    不再让它们生效，同时让 getResponse() 返回注入的 token，保证 Angular 在
    提交时读到的是有效值。
    """
    await page.add_init_script(
        r"""(() => {
            window.__hCaptchaInjectedToken = null;

            const suppressWhenInjected = (originalCallback, callbackName) => function(...args) {
                if (window.__hCaptchaInjectedToken) {
                    console.debug('[hook] suppressed hCaptcha ' + callbackName);
                    return undefined;
                }
                return originalCallback.apply(this, args);
            };

            // 拦截 hCaptcha API 脚本创建 window.hcaptcha 对象
            let _realHcaptcha = null;
            Object.defineProperty(window, 'hcaptcha', {
                configurable: true,
                enumerable: true,
                get() { return _realHcaptcha; },
                set(val) {
                    _realHcaptcha = val;
                    if (!val) { return; }

                    if (typeof val.render === 'function') {
                        const origRender = val.render.bind(val);
                        val.render = function(el, opts) {
                            if (opts && typeof opts.callback === 'function') {
                                window.__hCaptchaCallback = opts.callback;
                            }
                            // 组件自身挑战失败时不允许清掉已注入的 token
                            if (opts && typeof opts['expired-callback'] === 'function') {
                                opts['expired-callback'] = suppressWhenInjected(
                                    opts['expired-callback'], 'expired-callback'
                                );
                            }
                            if (opts && typeof opts['error-callback'] === 'function') {
                                opts['error-callback'] = suppressWhenInjected(
                                    opts['error-callback'], 'error-callback'
                                );
                            }
                            if (opts && typeof opts['chalexpired-callback'] === 'function') {
                                opts['chalexpired-callback'] = suppressWhenInjected(
                                    opts['chalexpired-callback'], 'chalexpired-callback'
                                );
                            }
                            return origRender(el, opts);
                        };
                    }

                    // Angular 提交前可能改用 getResponse() 取值
                    if (typeof val.getResponse === 'function') {
                        const origGetResponse = val.getResponse.bind(val);
                        val.getResponse = function(...args) {
                            if (window.__hCaptchaInjectedToken) {
                                return window.__hCaptchaInjectedToken;
                            }
                            return origGetResponse(...args);
                        };
                    }
                }
            });
        })()"""
    )

async def _print_clickable_snapshot(page: Page) -> None:
    buttons = await page.evaluate(
        r"""() => Array.from(document.querySelectorAll(
            'button, [role="button"], input[type="button"], input[type="submit"]'
        )).slice(0, 20).map((element) => ({
            text: [element.innerText, element.textContent, element.value, element.getAttribute('aria-label')]
                .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim(),
            disabled: Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true'),
            visible: window.getComputedStyle(element).display !== 'none' && element.getClientRects().length > 0
        }))"""
    )
    print("  clickable snapshot:")
    print(json.dumps(buttons, ensure_ascii=False, indent=2))

# ---------------------------------------------------------------------------
#  阶段 C：注册后跳转 + 建 key
# ---------------------------------------------------------------------------

async def finalize_and_create_key(page: Page, config: AppConfig) -> str | None:
    """注册提交后依次处理页面跳转，直到 session 有效并建 key。

    真实跳转链（实测确认）：
      验证码提交 → passkey/prompt-setup 页(点"稍后再说") → signin-redirect
      → consent 页(点"提交") → select-account(填组织名)
      → complete-profile(session 已有效, 直接建 key)
    """
    print("\n[阶段C] 处理注册后跳转，直到 session 有效...")
    deadline = time.time() + 240
    last_url = ""

    while time.time() < deadline:
        # 每轮先尝试直接建 key（session 可能已经有效）
        org_name = await _get_org_name(page)
        if org_name:
            print(f"  session 有效，orgName: {org_name}")
            return await _create_key_in_browser(page, org_name, config)

        url_now = page.url
        if url_now != last_url:
            print(f"  当前页面: {url_now[:90]}")
            last_url = url_now

        # 跳转链中某一跳加载失败会停在 chrome-error 页，只能重新导航把流程接回去
        if url_now.startswith("chrome-error"):
            print("  页面加载失败，重新打开 build.nvidia.com 以恢复流程...")
            if not await _recover_from_navigation_error(page):
                await asyncio.sleep(3)
            last_url = ""
            continue

        # 通行密钥引导页 → 点"稍后再说"跳过。
        # 跳过动作已在 register_account 里做过，此处 URL 可能只是还没来得及跳转，
        # 所以先给它一点时间自行离开，避免重复点击与无意义的告警。
        if "passkey" in url_now:
            if await _wait_for_url_change(page, url_now, wait_seconds=5):
                continue
            await _skip_passkey_prompt(page)
            await asyncio.sleep(3)
            continue

        # 创建组织页（利用组织名跳过手机验证）
        if "select-account" in url_now or "cloudaccounts.nvidia.com" in url_now:
            print("  创建组织页：填组织名...")
            await _create_org(page, config.nvidia.account_name)
            await asyncio.sleep(4)
            continue

        # consent 页 → 点提交
        if "consent" in url_now or "static-login.nvidia.com" in url_now:
            print("  consent 页：点提交...")
            await _click_continue(page)
            await asyncio.sleep(3)
            continue

        # signin-redirect、complete-profile 等 → 等待跳转
        await asyncio.sleep(2)

    print("  阶段C 超时，未能建 key")
    return None

async def _get_org_name(page: Page) -> str | None:
    """在浏览器上下文内 fetch user-context（credentials:include），拿 orgName。"""
    try:
        result = await page.evaluate(
            """async () => {
                try {
                    const resp = await fetch('https://api.ngc.nvidia.com/user-context', {
                        credentials: 'include',
                        headers: {'accept': 'application/json'}
                    });
                    if (!resp.ok) return {ok: false, status: resp.status};
                    const data = await resp.json();
                    return {ok: true, orgName: data.orgName || null};
                } catch (e) {
                    return {ok: false, error: String(e)};
                }
            }"""
        )
    except Exception:
        return None
    if result and result.get("ok"):
        return result.get("orgName")
    return None

async def _create_key_in_browser(page: Page, org_name: str, config: AppConfig) -> str | None:
    """在浏览器上下文内 POST 建 key（credentials:include），返回 nvapi-... key。"""
    print("  POST /keys/type/AI_PLAYGROUNDS_KEY...")
    payload = {
        "expiryDate": config.nvidia.key_expiry_date,
        "name": config.nvidia.key_name,
        "type": "AI_PLAYGROUNDS_KEY",
        "policies": [
            {
                "product": "nv-cloud-functions",
                "scopes": ["invoke_function"],
                "resources": [{"id": "*", "type": "account-functions"}],
            }
        ],
    }
    result = await page.evaluate(
        """async ({orgName, payload}) => {
            try {
                const resp = await fetch(
                    `https://api.ngc.nvidia.com/v3/orgs/${orgName}/keys/type/AI_PLAYGROUNDS_KEY`,
                    {
                        method: 'POST',
                        credentials: 'include',
                        headers: {'content-type': 'application/json', 'accept': '*/*'},
                        body: JSON.stringify(payload)
                    }
                );
                const text = await resp.text();
                let data = null;
                try { data = JSON.parse(text); } catch (_) {}
                return {status: resp.status, data, text: text.slice(0, 300)};
            } catch (e) {
                return {status: 0, error: String(e)};
            }
        }""",
        {"orgName": org_name, "payload": payload},
    )

    status = result.get("status")
    if status not in (200, 201):
        print(f"  建 key 失败: {status}: {result.get('text') or result.get('error')}")
        return None

    data = result.get("data") or {}
    api_key = (
        (data.get("apiKey") or {}).get("value", "")
        or (data.get("result") or {}).get("apiKey", {}).get("value", "")
    )
    if api_key:
        print(f"\nAI_PLAYGROUNDS_KEY: {api_key}")
        return api_key
    print("  响应中未找到 apiKey.value")
    return None

async def _create_org(page: Page, org_name: str) -> bool:
    """在 select-account 页填组织名并创建（跳过手机验证的关键）。"""
    text_input = page.locator('input[type="text"]:visible').first
    if await text_input.count() == 0:
        return False
    await text_input.click()
    await text_input.fill(org_name)
    await asyncio.sleep(0.5)

    btn = page.get_by_role("button", name="Create NVIDIA Cloud Account").first
    try:
        await btn.wait_for(state="visible", timeout=5000)
        for _ in range(10):
            if await btn.is_enabled():
                await btn.click()
                print("  clicked [Create NVIDIA Cloud Account]")
                return True
            await asyncio.sleep(0.5)
    except Exception:
        pass
    return False

async def _close_browser(browser, delay: int) -> None:
    print(f"\nBrowser will close in {delay} seconds...")
    await asyncio.sleep(delay)
    await browser.close()

if __name__ == "__main__":
    main_cli()
