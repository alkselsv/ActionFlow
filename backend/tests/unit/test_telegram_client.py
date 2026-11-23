from io import BytesIO
from urllib.error import HTTPError

import pytest

from actionflow.telegram_client import TelegramApiError, TelegramClient


def test_client_repr_and_errors_do_not_expose_token(monkeypatch):
    client = TelegramClient("123456:synthetic-secret")
    assert "123456" not in repr(client)
    assert "synthetic-secret" not in repr(client)

    def fail(*_args, **_kwargs):
        raise OSError("network unavailable")

    monkeypatch.setattr("actionflow.telegram_client.urlopen", fail)
    try:
        client.get_me()
    except TelegramApiError as error:
        assert "synthetic-secret" not in str(error)
        assert "123456" not in str(error)
    else:
        raise AssertionError("TelegramApiError was expected")


def test_http_rate_limit_preserves_retry_after_without_exposing_payload(monkeypatch):
    def fail(request, **kwargs):
        payload = b'{"description":"private data","parameters":{"retry_after":31}}'
        raise HTTPError(request.full_url, 429, "Rate limit", {}, BytesIO(payload))

    monkeypatch.setattr("actionflow.telegram_client.urlopen", fail)
    with pytest.raises(TelegramApiError) as caught:
        TelegramClient("123456:synthetic-secret").get_updates()
    assert caught.value.code == "rate_limited"
    assert caught.value.retry_after == 31
    assert "private data" not in str(caught.value)
    assert "synthetic-secret" not in str(caught.value)
