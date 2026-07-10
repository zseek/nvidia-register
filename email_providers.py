from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

import requests

from config import AppConfig, CloudflareTempEmailConfig, DuckMailConfig, MailNestConfig


@dataclass(frozen=True)
class TempEmailInbox:
    address: str
    token: str


class TempEmailProvider(Protocol):
    def create_inbox(self, name: str) -> TempEmailInbox:
        ...

    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None:
        ...


class CloudflareTempEmailProvider:
    def __init__(self, config: CloudflareTempEmailConfig):
        self.config = config

    def create_inbox(self, name: str) -> TempEmailInbox:
        response = requests.post(
            f"{self.config.api_url}/admin/new_address",
            headers={"x-admin-auth": self.config.admin_auth, "Content-Type": "application/json"},
            json={"name": name, "domain": self.config.domain, "enablePrefix": False},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        address = data.get("address", "")
        token = data.get("jwt", "")
        if not address or not token:
            raise RuntimeError(f"Email creation failed: {data}")
        return TempEmailInbox(address=address, token=token)

    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None:
        deadline = time.time() + timeout_seconds
        headers = {"Authorization": f"Bearer {inbox.token}"}
        while time.time() < deadline:
            try:
                response = requests.get(
                    f"{self.config.api_url}/api/mails?limit=5&offset=0",
                    headers=headers,
                    timeout=15,
                )
                data = response.json()
                mails = data.get("results") or data.get("data") or []
                for mail in mails:
                    mail_id = mail.get("id") or mail.get("_id")
                    if not mail_id:
                        continue
                    detail_response = requests.get(
                        f"{self.config.api_url}/api/mail/{mail_id}",
                        headers=headers,
                        timeout=15,
                    )
                    code = _extract_verification_code(detail_response.json().get("raw", ""))
                    if code:
                        return code
            except Exception as exc:
                print(f"  email poll: {exc}", flush=True)
            time.sleep(2)
        return None


class DuckMailProvider:
    def __init__(self, config: DuckMailConfig):
        self.config = config

    def create_inbox(self, name: str) -> TempEmailInbox:
        address = f"{name}@{self.config.domain}"
        password = f"dm_{secrets.token_hex(8)}"

        response = requests.post(
            f"{self.config.api_url}/accounts",
            headers=self._account_headers(),
            json={"address": address, "password": password},
            timeout=15,
        )
        response.raise_for_status()

        token_response = requests.post(
            f"{self.config.api_url}/token",
            headers={"Content-Type": "application/json"},
            json={"address": address, "password": password},
            timeout=15,
        )
        token_response.raise_for_status()
        data = token_response.json()
        token = data.get("token", "")
        if not token:
            raise RuntimeError(f"DuckMail token acquisition failed: {data}")
        return TempEmailInbox(address=address, token=token)

    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None:
        deadline = time.time() + timeout_seconds
        headers = {"Authorization": f"Bearer {inbox.token}"}
        while time.time() < deadline:
            try:
                response = requests.get(
                    f"{self.config.api_url}/messages?page=1",
                    headers=headers,
                    timeout=15,
                )
                response.raise_for_status()
                data = response.json()
                messages = data.get("hydra:member") or []
                for message in messages:
                    message_id = message.get("id")
                    if not message_id:
                        continue
                    detail_response = requests.get(
                        f"{self.config.api_url}/messages/{message_id}",
                        headers=headers,
                        timeout=15,
                    )
                    detail_response.raise_for_status()
                    detail = detail_response.json()
                    body = _duckmail_message_body(detail)
                    code = _extract_verification_code(body)
                    if code:
                        return code
            except Exception as exc:
                print(f"  email poll: {exc}", flush=True)
            time.sleep(2)
        return None

    def _account_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers


class MailNestProvider:
    def __init__(self, config: MailNestConfig):
        self.config = config
        if (not self.config.project_code) or (not self.config.api_key):
            raise RuntimeError('project_code 与 api_key 是必须的')

    def create_inbox(self, name: str) -> TempEmailInbox:
        response = requests.post(
            "https://mailnest.top/api/v1/email/temporary/buy",
            json={
                "project_code": self.config.project_code,
                "count": 1,
            },
            headers=self._account_headers(),
            timeout=15,
        )
        if response.status_code == 401:
            raise Exception('身份验证不通过')
        resp_json = response.json()
        if resp_json['code'] != '00000':
            raise RuntimeError(f"MailNest failed: {resp_json}")
        return TempEmailInbox(address=resp_json['data'][0]['email'], token='')

    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None:
        deadline = time.time() + timeout_seconds
        email = inbox.address
        while time.time() < deadline:
            try:
                response = requests.post(
                    f'https://mailnest.top/api/v1/email/receive',
                    json={
                        "email": email,
                    },
                    headers=self._account_headers(),
                    timeout=15,
                )
                if response.status_code == 401:
                    raise Exception('身份验证不通过')
                resp_json = response.json()
                if resp_json['code'] != '00000':
                    raise RuntimeError(f"MailNest failed: {resp_json}")
                if not resp_json['data']:
                    time.sleep(2)
                    continue
                code = _extract_verification_code(resp_json['data'][0]['body'])
                if code:
                    return code
            except Exception as exc:
                print(f"  email poll: {exc}", flush=True)
            time.sleep(2)
        return None

    def _account_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers


def _extract_verification_code(raw_message: str) -> str | None:
    clean = re.sub(r"=\r?\n", "", raw_message)
    index = clean.lower().find("verification code")
    if index >= 0:
        snippet = clean[index : index + 500]
        match = re.search(r"(\d{3})\s*[-–]\s*(\d{3})", snippet)
        if match:
            return match.group(1) + match.group(2)
    match = re.search(r"(?<!\d)(\d{3})[-–](\d{3})(?!\d)", clean)
    if match:
        return match.group(1) + match.group(2)
    return None


def _duckmail_message_body(detail: dict) -> str:
    parts: list[str] = []
    text = detail.get("text")
    if isinstance(text, str) and text.strip():
        parts.append(text)

    html = detail.get("html") or []
    if isinstance(html, list):
        for item in html:
            if isinstance(item, str) and item.strip():
                parts.append(item)
    elif isinstance(html, str) and html.strip():
        parts.append(html)

    return "\n".join(parts)


def build_email_provider(config: AppConfig) -> TempEmailProvider:
    if config.email_provider == "cloudflare_temp_email":
        return CloudflareTempEmailProvider(config.cloudflare_temp_email)
    if config.email_provider == "duckmail":
        return DuckMailProvider(config.duckmail)
    if config.email_provider == "mailnest_temp_email":
        return MailNestProvider(config.mailnest_temp_email)
    raise ValueError(f"Unsupported email provider: {config.email_provider}")
