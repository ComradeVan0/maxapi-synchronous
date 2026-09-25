"""Тесты корректной сборки URL при кастомном api_url с path-префиксом.

Path-префикс в кастомном api_url (например, у прокси/шлюза на
нестандартных стендах) не должен отбрасываться. См. issue про
set_api_url() с непустым path в базовом URL.
"""

from json import dumps
from unittest.mock import MagicMock

import pytest
from maxapi import Bot
from maxapi.connection.base import BaseConnection
from maxapi.enums.api_path import ApiPath
from maxapi.enums.http_method import HTTPMethod


def _make_response(status=200, json_data=None):
    """Создаёт мок ответа requests."""
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.text = dumps(json_data or {})
    return resp


class TestRequestUrlWithCustomApiUrl:
    """Тесты сборки итогового URL в BaseConnection.request()."""

    @pytest.fixture
    def bot_with_mock_session(self, mock_bot_token):
        """Бот с мок-сессией, готовой отдать успешный ответ."""
        bot = Bot(token=mock_bot_token)
        session = MagicMock()
        session.request = MagicMock(return_value=_make_response())
        bot.session = session
        return bot

    @staticmethod
    def _make_base(bot):
        """BaseConnection, использующий мок-сессию бота."""
        base = BaseConnection()
        base.bot = bot
        base.session = bot.session
        return base

    def test_path_prefix_in_api_url_is_preserved(self, bot_with_mock_session):
        """Path-префикс кастомного api_url не отбрасывается."""
        bot_with_mock_session.set_api_url("https://stand.internal/gateway/v1")

        base = self._make_base(bot_with_mock_session)

        base.request(
            method=HTTPMethod.GET,
            path=ApiPath.MESSAGES,
            is_return_raw=True,
        )

        called_url = bot_with_mock_session.session.request.call_args.kwargs[
            "url"
        ]
        assert called_url == "https://stand.internal/gateway/v1/messages"

    def test_trailing_slash_in_api_url_does_not_double(
        self, bot_with_mock_session
    ):
        """Trailing slash в api_url не даёт двойной слэш в URL."""
        bot_with_mock_session.set_api_url("https://stand.internal/gateway/v1/")

        base = self._make_base(bot_with_mock_session)

        base.request(
            method=HTTPMethod.GET,
            path=ApiPath.ME,
            is_return_raw=True,
        )

        called_url = bot_with_mock_session.session.request.call_args.kwargs[
            "url"
        ]
        assert called_url == "https://stand.internal/gateway/v1/me"

    def test_default_api_url_unchanged(self, bot_with_mock_session):
        """Без кастомного api_url поведение не меняется."""
        base = self._make_base(bot_with_mock_session)

        base.request(
            method=HTTPMethod.GET,
            path=ApiPath.CHATS,
            is_return_raw=True,
        )

        called_url = bot_with_mock_session.session.request.call_args.kwargs[
            "url"
        ]
        assert called_url == bot_with_mock_session.api_url + "/chats"
