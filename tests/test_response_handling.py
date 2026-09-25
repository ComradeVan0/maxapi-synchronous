"""Тесты обработки ответов в BaseConnection (issue #199)."""

from unittest.mock import MagicMock, Mock

import pytest
from maxapi import Bot
from maxapi.client.default import DefaultConnectionProperties
from maxapi.connection.base import BaseConnection
from maxapi.enums.http_method import HTTPMethod
from maxapi.enums.upload_type import UploadType
from maxapi.exceptions.max import MaxApiError, MaxUploadFileFailed

HTML_ERROR_BODY = "<html><body><h1>429 Too Many Requests</h1></body></html>"


def _make_response(status, *, text=""):
    """Создаёт мок ответа requests."""
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.text = text
    return resp


def _make_bot_with_response(mock_bot_token, response, **conn_kwargs):
    """Бот с мок-сессией, всегда отдающей переданный ответ."""
    bot = Bot(
        token=mock_bot_token,
        default_connection=DefaultConnectionProperties(**conn_kwargs),
    )
    session = MagicMock()
    session.request = MagicMock(return_value=response)
    bot.session = session
    return bot


def _make_base(bot):
    """BaseConnection, использующий мок-сессию бота."""
    base = BaseConnection()
    base.bot = bot
    base.session = bot.session
    return base


def _make_upload_connection(response):
    """BaseConnection с ботом и сессией, отдающей переданный ответ."""
    session = MagicMock()
    session.post = Mock(return_value=response)

    conn = BaseConnection()
    bot = Mock()
    bot.default_connection = DefaultConnectionProperties()
    bot.session = session
    conn.bot = bot
    conn.session = session
    return conn


def _make_upload_response(status, *, text=""):
    """Мок ответа upload-сервера."""
    resp = MagicMock()
    resp.status_code = status
    resp.ok = status < 400
    resp.text = text
    return resp


class TestUploadFileStatusCheck:
    """Дефект 1: статус ответа upload-сервера проверяется."""

    def test_upload_file_raises_on_error_status(self, tmp_path):
        """upload_file бросает MaxUploadFileFailed при 413."""
        test_file = tmp_path / "big.mp4"
        test_file.write_bytes(b"fake-video")

        response = _make_upload_response(413, text="Request Entity Too Large")
        conn = _make_upload_connection(response)

        with pytest.raises(MaxUploadFileFailed) as exc_info:
            conn.upload_file(
                url="https://upload.example.com",
                path=str(test_file),
                type=UploadType.VIDEO,
            )

        message = str(exc_info.value)
        assert "413" in message
        assert "Request Entity Too Large" in message

    def test_upload_file_buffer_raises_on_error_status(self):
        """upload_file_buffer бросает MaxUploadFileFailed при 500."""
        response = _make_upload_response(500, text="upload backend down")
        conn = _make_upload_connection(response)

        with pytest.raises(MaxUploadFileFailed) as exc_info:
            conn.upload_file_buffer(
                filename="clip",
                url="https://upload.example.com",
                buffer=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100,
                type=UploadType.AUDIO,
            )

        message = str(exc_info.value)
        assert "500" in message
        assert "upload backend down" in message

    def test_upload_file_raises_on_redirect_status(self, tmp_path):
        """3xx от upload-сервера не считается успешной загрузкой."""
        test_file = tmp_path / "photo.png"
        test_file.write_bytes(b"fake-png")

        response = _make_upload_response(302, text="Found")
        conn = _make_upload_connection(response)

        with pytest.raises(MaxUploadFileFailed) as exc_info:
            conn.upload_file(
                url="https://upload.example.com",
                path=str(test_file),
                type=UploadType.IMAGE,
            )

        assert "302" in str(exc_info.value)

    def test_upload_file_returns_body_on_success(self, tmp_path):
        """При успешном статусе возвращается сырое тело ответа."""
        test_file = tmp_path / "photo.png"
        test_file.write_bytes(b"fake-png")

        response = _make_upload_response(200, text='{"token":"t"}')
        conn = _make_upload_connection(response)

        result = conn.upload_file(
            url="https://upload.example.com",
            path=str(test_file),
            type=UploadType.IMAGE,
        )

        assert result == '{"token":"t"}'


class TestRequestNonJsonBody:
    """Дефект 2: не-JSON тело не роняет request()."""

    def test_html_error_body_raises_max_api_error(self, mock_bot_token):
        """text/html при 429 заворачивается в MaxApiError."""
        response = _make_response(429, text=HTML_ERROR_BODY)
        bot = _make_bot_with_response(mock_bot_token, response)
        base = _make_base(bot)

        with pytest.raises(MaxApiError) as exc_info:
            base.request(
                method=HTTPMethod.GET,
                path="/test",
                is_return_raw=True,
            )

        assert exc_info.value.code == 429
        assert exc_info.value.raw == HTML_ERROR_BODY

    def test_empty_error_body_raises_max_api_error(self, mock_bot_token):
        """Пустое тело при 400 не роняет request()."""
        response = _make_response(400, text="")
        bot = _make_bot_with_response(mock_bot_token, response)
        base = _make_base(bot)

        with pytest.raises(MaxApiError) as exc_info:
            base.request(
                method=HTTPMethod.GET,
                path="/test",
                is_return_raw=True,
            )

        assert exc_info.value.code == 400
        assert exc_info.value.raw == ""

    def test_json_error_body_parsed_to_dict(self, mock_bot_token):
        """JSON-тело ошибки по-прежнему попадает в raw как dict."""
        response = _make_response(400, text='{"code": "attachment.not.ready"}')
        bot = _make_bot_with_response(mock_bot_token, response)
        base = _make_base(bot)

        with pytest.raises(MaxApiError) as exc_info:
            base.request(
                method=HTTPMethod.GET,
                path="/test",
                is_return_raw=True,
            )

        assert exc_info.value.raw == {"code": "attachment.not.ready"}


class TestRequestOkBodySemantics:
    """Дефект 2: пустое/не-dict тело успешного ответа."""

    def test_empty_ok_body_raises_max_api_error(self, mock_bot_token):
        """Пустое тело при 200 даёт MaxApiError, а не TypeError."""
        response = _make_response(200, text="")
        bot = _make_bot_with_response(mock_bot_token, response)
        base = _make_base(bot)

        with pytest.raises(MaxApiError) as exc_info:
            base.request(
                method=HTTPMethod.GET,
                path="/test",
                model=MagicMock(),
            )

        assert exc_info.value.code == 200
        assert exc_info.value.raw == ""

    def test_non_dict_ok_body_raises_max_api_error(self, mock_bot_token):
        """JSON-массив при 200 даёт MaxApiError с текстом тела."""
        response = _make_response(200, text="[1, 2, 3]")
        bot = _make_bot_with_response(mock_bot_token, response)
        base = _make_base(bot)

        with pytest.raises(MaxApiError) as exc_info:
            base.request(
                method=HTTPMethod.GET,
                path="/test",
                is_return_raw=True,
            )

        assert exc_info.value.code == 200
        assert exc_info.value.raw == "[1, 2, 3]"

    def test_ok_body_never_calls_model_on_bad_body(self, mock_bot_token):
        """model не вызывается, если тело не является JSON-объектом."""
        response = _make_response(200, text="not json at all")
        bot = _make_bot_with_response(mock_bot_token, response)
        base = _make_base(bot)
        model = MagicMock()

        with pytest.raises(MaxApiError):
            base.request(
                method=HTTPMethod.GET,
                path="/test",
                model=model,
            )

        model.assert_not_called()


class TestRetryExhaustedKeepsBody:
    """Дефект 3: тело серверной ошибки не теряется после ретраев."""

    @pytest.fixture
    def conn_kwargs(self):
        """Быстрый retry для тестов."""
        return {
            "max_retries": 2,
            "retry_on_statuses": (502, 503, 504),
            "retry_backoff_factor": 0.01,
        }

    def test_json_body_preserved_as_dict(self, mock_bot_token, conn_kwargs):
        """JSON-тело 503 попадает в MaxApiError.raw как dict."""
        response = _make_response(503, text='{"code": "service.unavailable"}')
        bot = _make_bot_with_response(mock_bot_token, response, **conn_kwargs)
        base = _make_base(bot)

        with pytest.raises(MaxApiError) as exc_info:
            base.request(
                method=HTTPMethod.GET,
                path="/test",
                is_return_raw=True,
            )

        assert exc_info.value.code == 503
        assert exc_info.value.raw == {"code": "service.unavailable"}
        assert exc_info.value.raw != {"error": "Server error 503"}

    def test_html_body_preserved_as_text(self, mock_bot_token, conn_kwargs):
        """Не-JSON тело 502 попадает в MaxApiError.raw как текст."""
        body = "<html><body><h1>502 Bad Gateway</h1></body></html>"
        response = _make_response(502, text=body)
        bot = _make_bot_with_response(mock_bot_token, response, **conn_kwargs)
        base = _make_base(bot)

        with pytest.raises(MaxApiError) as exc_info:
            base.request(
                method=HTTPMethod.GET,
                path="/test",
                is_return_raw=True,
            )

        assert exc_info.value.code == 502
        assert exc_info.value.raw == body
