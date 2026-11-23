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
