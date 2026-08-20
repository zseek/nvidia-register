from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse, parse_qs

import requests
from playwright.async_api import Page

from config import CaptchaConfig


class CaptchaSolver(Protocol):
    async def solve(self, page: Page) -> bool:
        ...


class ManualCaptchaSolver:
    async def solve(self, page: Page) -> bool:
        print("\n[2/4] Please solve the hCaptcha manually...")
        for i in range(120):
            if await _is_register_button_enabled(page):
                print(f"  hCaptcha solved ({i}s)")
                return True
            await asyncio.sleep(1)
        print("  hCaptcha timeout")
        return False


@dataclass(frozen=True)
class YesCaptchaSolver:
    client_key: str
    api_url: str
    poll_interval_seconds: int
    timeout_seconds: int

    async def solve(self, page: Page) -> bool:
        print("\n[2/4] Solving hCaptcha with YesCaptcha...")
        site_key = await _get_site_key(page)
        if not site_key:
            print("  hCaptcha sitekey not found")
            return False

        task_id = self._create_task(page.url, site_key)
        token = self._poll_task_result(task_id)
        if not token:
            return False

        await _inject_hcaptcha_token(page, token)
        for i in range(20):
            if await _is_register_button_enabled(page):
                print(f"  hCaptcha solved by YesCaptcha ({i}s)")
                return True
            await asyncio.sleep(1)
        print("  hCaptcha token injected, but #register_button stayed disabled")
        return False

    def _create_task(self, website_url: str, website_key: str) -> str:
        response = requests.post(
            f"{self.api_url}/createTask",
            json={
                "clientKey": self.client_key,
                "task": {
                    "type": "HCaptchaTaskProxyless",
                    "websiteURL": website_url,
                    "websiteKey": website_key,
                },
            },
            timeout=30,
        )
        data = response.json()
        if data.get("errorId"):
            raise RuntimeError(f"YesCaptcha createTask failed: {data}")
        task_id = data.get("taskId")
        if not task_id:
            raise RuntimeError(f"YesCaptcha createTask missing taskId: {data}")
        return str(task_id)

    def _poll_task_result(self, task_id: str) -> str | None:
        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            try:
                response = requests.post(
                    f"{self.api_url}/getTaskResult",
                    json={"clientKey": self.client_key, "taskId": task_id},
                    timeout=30,
                )
                data = response.json()
            except requests.RequestException as exc:
                # 网络抖动不该让整个任务失败，继续轮询到超时为止
                print(f"  YesCaptcha getTaskResult network error: {exc}")
                time.sleep(self.poll_interval_seconds)
                continue

            if data.get("errorId"):
                print(f"  YesCaptcha getTaskResult failed: {data}")
                return None
            if data.get("status") == "ready":
                solution = data.get("solution") or {}
                return solution.get("gRecaptchaResponse") or solution.get("token")
            time.sleep(self.poll_interval_seconds)
        print("  YesCaptcha timeout")
        return None


@dataclass(frozen=True)
class CaptchaRunSolver:
    token: str
    api_url: str
    poll_interval_seconds: int
    timeout_seconds: int

    async def solve(self, page: Page) -> bool:
        print("\n[2/4] Solving hCaptcha with CaptchaRun...")
        site_key = await _get_site_key(page)
        if not site_key:
            print("  hCaptcha sitekey not found")
            return False

        user_agent = await page.evaluate("() => navigator.userAgent")
        task_id, token = self._create_task(page.url, site_key, user_agent)
        if task_id and not token:
            token = self._poll_task_result(task_id)
        if not token:
            return False

        await _inject_hcaptcha_token(page, token)
        for i in range(20):
            if await _is_register_button_enabled(page):
                print(f"  hCaptcha solved by CaptchaRun ({i}s)")
                return True
            await asyncio.sleep(1)
        print("  hCaptcha token injected, but #register_button stayed disabled")
        return False

    def _create_task(self, website_url: str, website_key: str, user_agent: str) -> tuple[str | None, str | None]:
        response = requests.post(
            f"{self.api_url}/v2/tasks",
            headers=self._headers(),
            json={
                "captchaType": "HCaptcha",
                "siteKey": website_key,
                "siteReferer": _site_referer(website_url),
                "userAgent": user_agent,
                "fallbackToActualUA": True,
            },
            timeout=30,
        )
        data = _response_json(response)
        if not response.ok:
            raise RuntimeError(f"CaptchaRun create task failed: {data}")
        task_id = data.get("taskId")
        result = data.get("result") or {}
        token = _extract_hcaptcha_token(result)
        if not task_id and not token:
            raise RuntimeError(f"CaptchaRun create task missing taskId/result: {data}")
        return str(task_id) if task_id else None, token

    def _poll_task_result(self, task_id: str) -> str | None:
        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            response = requests.get(
                f"{self.api_url}/v2/tasks/{task_id}",
                headers=self._headers(content_type=False),
                timeout=30,
            )
            data = _response_json(response)
            if not response.ok:
                print(f"  CaptchaRun get task result failed: {data}")
                return None

            status = str(data.get("status", "")).lower()
            if status == "success":
                return _extract_hcaptcha_token(data.get("response") or data.get("result") or {})
            if status == "fail":
                print(f"  CaptchaRun failed: {data.get('reason') or data}")
                return None
            time.sleep(self.poll_interval_seconds)
        print("  CaptchaRun timeout")
        return None

    def _headers(self, content_type: bool = True) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers


# ---------------------------------------------------------------------------
#  sitekey 捕获（render=explicit 模式下 DOM 无 sitekey，只能从网络请求获取）
# ---------------------------------------------------------------------------

_captured_sitekey: str | None = None


def reset_captcha_state() -> None:
    """重置模块级缓存，供批量注册时每个新账号使用。"""
    global _captured_sitekey
    _captured_sitekey = None


def start_capturing_sitekey(page: Page) -> None:
    """注册网络请求监听器，从 checksiteconfig 请求中捕获 hCaptcha sitekey。

    必须在 create-account 页加载前调用。
    """
    def _on_request(req):
        global _captured_sitekey
        if _captured_sitekey:
            return
        url = req.url
        if "checksiteconfig" in url and "sitekey=" in url:
            try:
                sk = parse_qs(urlparse(url).query).get("sitekey", [None])[0]
                if sk:
                    _captured_sitekey = sk
                    print(f"  sitekey captured: {sk}")
            except Exception:
                pass

    page.on("request", _on_request)


async def _get_site_key(page: Page) -> str | None:
    """获取 sitekey（仅从网络请求缓存中读取）。"""
    if _captured_sitekey:
        return _captured_sitekey
    # 等待网络请求捕获（hCaptcha iframe 可能还在加载）
    for _ in range(30):
        if _captured_sitekey:
            return _captured_sitekey
        await asyncio.sleep(1)
    return None


# ---------------------------------------------------------------------------
#  token 注入（通过拦截的 Angular 回调直接触发 onSuccess）
# ---------------------------------------------------------------------------


async def _inject_hcaptcha_token(page: Page, token: str) -> bool:
    """调用拦截的 __hCaptchaCallback 触发 Angular onSuccess，使 #register_button enable。

    回调由 main.py 的 _ensure_hcaptcha_hook 通过 addInitScript 在
    hcaptcha.render 调用时捕获到 window.__hCaptchaCallback。

    同时把 token 写进 h-captcha-response 隐藏域并置 __hCaptchaInjectedToken 标记，
    后者会让 hook 屏蔽掉 hCaptcha 组件自身失败触发的 expired/error 回调，
    避免刚注入的 token 又被 Angular 清掉。
    """
    result = await page.evaluate(
        r"""(token) => {
            window.__hCaptchaInjectedToken = token;

            // 隐藏域：部分实现在提交时直接读表单值
            let textareaCount = 0;
            for (const name of ['h-captcha-response', 'g-recaptcha-response']) {
                for (const field of document.querySelectorAll(`textarea[name="${name}"]`)) {
                    field.value = token;
                    field.dispatchEvent(new Event('input', {bubbles: true}));
                    field.dispatchEvent(new Event('change', {bubbles: true}));
                    textareaCount += 1;
                }
            }

            let callbackInvoked = false;
            if (typeof window.__hCaptchaCallback === 'function') {
                window.__hCaptchaCallback(token);
                callbackInvoked = true;
            }
            return {callbackInvoked, textareaCount};
        }""",
        token,
    )
    callback_invoked = bool(result.get("callbackInvoked"))
    print(f"  token injected (callback={callback_invoked}, textarea={result.get('textareaCount')})")
    if not callback_invoked:
        print("  WARNING: __hCaptchaCallback 未捕获到，Angular 可能收不到 token")
    return callback_invoked


# ---------------------------------------------------------------------------
#  辅助
# ---------------------------------------------------------------------------


async def _is_register_button_enabled(page: Page) -> bool:
    """检查 #register_button 是否 enabled（hCaptcha 通过后按钮才会 enable）。"""
    result = await page.evaluate(
        """() => {
            const btn = document.querySelector('#register_button');
            return btn ? !btn.disabled : false;
        }"""
    )
    return bool(result)


def _site_referer(website_url: str) -> str:
    parsed = urlparse(website_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return website_url


def _response_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {"status_code": response.status_code, "text": response.text}
    return data if isinstance(data, dict) else {"data": data}


def _extract_hcaptcha_token(data: dict[str, Any]) -> str | None:
    token = data.get("gRecaptchaResponse") or data.get("token")
    return str(token) if token else None


def build_captcha_solver(config: CaptchaConfig) -> CaptchaSolver:
    if config.mode == "manual":
        return ManualCaptchaSolver()
    if config.mode == "yescaptcha":
        if not config.yescaptcha_client_key:
            raise ValueError("yescaptcha_client_key is required")
        return YesCaptchaSolver(
            client_key=config.yescaptcha_client_key,
            api_url=config.yescaptcha_api_url,
            poll_interval_seconds=config.poll_interval_seconds,
            timeout_seconds=config.timeout_seconds,
        )
    if config.mode == "captcharun":
        if not config.captcharun_token:
            raise ValueError("captcharun_token is required")
        return CaptchaRunSolver(
            token=config.captcharun_token,
            api_url=config.captcharun_api_url,
            poll_interval_seconds=config.poll_interval_seconds,
            timeout_seconds=config.timeout_seconds,
        )
    raise ValueError(f"Unsupported captcha mode: {config.mode}")
