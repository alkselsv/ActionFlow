"""Minimal Telegram Bot API client for the selected getUpdates transport."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ALLOWED_UPDATES = ("message", "edited_message", "my_chat_member")
UPDATE_LIMIT = 100


def configured_bot_id(token):
    prefix, separator, secret = (token or "").partition(":")
    return prefix if separator and secret and prefix.isascii() and prefix.isdecimal() else None


class TelegramApiError(RuntimeError):
    """A safe API error that never contains the token-bearing URL."""

    def __init__(self, message, *, code="temporary", retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def _api_error(status, payload):
    if status == 429:
        parameters = payload.get("parameters")
        delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
        return TelegramApiError(
            "Telegram rate limit.",
            code="rate_limited",
            retry_after=delay if type(delay) is int and delay > 0 else None,
        )
    if status == 403:
        return TelegramApiError("Telegram access denied.", code="access_denied")
    if status in (400, 401, 404, 409):
        return TelegramApiError(
            "Telegram rejected the polling configuration.", code="configuration"
        )
    return TelegramApiError("Telegram API temporarily unavailable.")


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
        except HTTPError as error:
            try:
                payload = json.loads(error.read(65536))
            except ValueError, OSError:
                payload = {}
            finally:
                error.close()
            raise _api_error(
                error.code, payload if isinstance(payload, dict) else {}
            ) from None
        except URLError, TimeoutError, OSError:
            raise TelegramApiError(
                "Telegram API request failed. Check the token, network and proxy."
            ) from None
        try:
            payload = json.loads(body.decode("utf-8"))
        except UnicodeDecodeError, ValueError:
            raise TelegramApiError(
                "Telegram API returned an invalid response."
            ) from None
        if not isinstance(payload, dict):
            raise TelegramApiError("Telegram API returned an error.")
        if payload.get("ok") is not True:
            raise _api_error(payload.get("error_code"), payload)
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
            "limit": UPDATE_LIMIT,
            "allowed_updates": json.dumps(list(allowed_updates)),
        }
        if offset is not None:
            params["offset"] = offset
        result = self._call("getUpdates", params, timeout=timeout + 10)
        if not isinstance(result, list):
            raise TelegramApiError("Telegram API returned invalid updates.")
        return [item for item in result if isinstance(item, dict)]
