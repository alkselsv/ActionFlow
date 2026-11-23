"""Minimal Telegram Bot API client for the selected getUpdates transport."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ALLOWED_UPDATES = ("message", "edited_message", "my_chat_member")


class TelegramApiError(RuntimeError):
    """A safe API error that never contains the token-bearing URL."""


class TelegramClient:
    def __init__(self, token: str, base_url: str = "https://api.telegram.org"):
        if not isinstance(token, str) or not token:
            raise ValueError("A Telegram bot token is required")
        self._endpoint = f"{base_url.rstrip('/')}/bot{token}"

    def __repr__(self):
        return "TelegramClient(<redacted>)"

    def _call(self, method: str, params: dict | None = None, *, timeout: float = 20):
        query = urlencode(params or {})
        url = f"{self._endpoint}/{method}" + (f"?{query}" if query else "")
        request = Request(url, headers={"User-Agent": "ActionFlow/0.1"})
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read()
        except (HTTPError, URLError, TimeoutError, OSError):
            raise TelegramApiError(
                "Telegram API request failed. Check the token, network and proxy."
            ) from None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise TelegramApiError(
                "Telegram API returned an invalid response."
            ) from None
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise TelegramApiError("Telegram API returned an error.")
        return payload.get("result")

    def get_me(self, *, timeout: float = 20) -> dict:
        result = self._call("getMe", timeout=timeout)
        if not isinstance(result, dict) or not result.get("id"):
            raise TelegramApiError("Telegram API returned an invalid bot identity.")
        return result

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: float = 30,
        allowed_updates: tuple[str, ...] = ALLOWED_UPDATES,
    ) -> list[dict]:
        params = {
            "timeout": int(timeout),
            "allowed_updates": json.dumps(list(allowed_updates)),
        }
        if offset is not None:
            params["offset"] = offset
        result = self._call("getUpdates", params, timeout=timeout + 10)
        if not isinstance(result, list):
            raise TelegramApiError("Telegram API returned invalid updates.")
        return [item for item in result if isinstance(item, dict)]
