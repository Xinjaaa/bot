import logging
import os
import time
from dataclasses import dataclass
from threading import Lock
from urllib.parse import quote

import httpx


logger = logging.getLogger("wecom-api")


class WeComAPIError(Exception):
    pass


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _truncate_utf8(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    trimmed = raw[:max_bytes]
    while True:
        try:
            return trimmed.decode("utf-8")
        except UnicodeDecodeError:
            trimmed = trimmed[:-1]


@dataclass
class _TokenCache:
    token: str = ""
    expires_at: float = 0.0


class WeComAPIClient:
    def __init__(self) -> None:
        self.corp_id = _get_required_env("WECOM_CORP_ID")
        self.app_secret = _get_required_env("WECOM_APP_SECRET")
        self.agent_id = int(_get_required_env("WECOM_AGENT_ID"))
        self.base_url = os.getenv("WECOM_API_BASE_URL", "https://qyapi.weixin.qq.com")
        self.timeout = float(os.getenv("WECOM_API_TIMEOUT_SECONDS", "10"))
        self.max_text_bytes = int(os.getenv("WECOM_MAX_TEXT_BYTES", "1800"))
        self._token_cache = _TokenCache()
        self._token_lock = Lock()
        logger.info(
            "wecom api initialized base_url=%s agent_id=%s timeout=%s max_text_bytes=%s",
            self.base_url,
            self.agent_id,
            self.timeout,
            self.max_text_bytes,
        )

    def _request(self, method: str, path: str, *, params: dict[str, str] | None = None, json: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        logger.info("wecom api request method=%s url=%s params=%s json_preview=%r", method, url, params, str(json)[:300])
        with httpx.Client(timeout=self.timeout) as client:
            response = client.request(method, url, params=params, json=json)
            logger.info(
                "wecom api response method=%s url=%s status=%s body_preview=%r",
                method,
                url,
                response.status_code,
                response.text[:500],
            )
            response.raise_for_status()
            data = response.json()
        if data.get("errcode", 0) != 0:
            raise WeComAPIError(f"wecom api error: {data}")
        return data

    def get_access_token(self) -> str:
        now = time.time()
        with self._token_lock:
            if self._token_cache.token and self._token_cache.expires_at - now > 60:
                logger.info("reuse cached access_token expires_in=%s", int(self._token_cache.expires_at - now))
                return self._token_cache.token

            data = self._request(
                "GET",
                "/cgi-bin/gettoken",
                params={"corpid": self.corp_id, "corpsecret": self.app_secret},
            )
            access_token = data["access_token"]
            expires_in = int(data.get("expires_in", 7200))
            self._token_cache = _TokenCache(
                token=access_token,
                expires_at=now + expires_in,
            )
            logger.info("fetched new access_token expires_in=%s", expires_in)
            return access_token

    def send_text_message(self, user_id: str, content: str) -> dict:
        safe_content = _truncate_utf8(content, self.max_text_bytes)
        access_token = self.get_access_token()
        payload = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": self.agent_id,
            "text": {"content": safe_content},
            "safe": 0,
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 1800,
        }
        return self._request(
            "POST",
            "/cgi-bin/message/send",
            params={"access_token": access_token},
            json=payload,
        )


def is_wecom_api_configured() -> bool:
    required_envs = ("WECOM_CORP_ID", "WECOM_APP_SECRET", "WECOM_AGENT_ID")
    return all(os.getenv(name) for name in required_envs)
