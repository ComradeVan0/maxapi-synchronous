"""Тесты SSL-настроек requests-клиента (русский доверенный CA)."""

from __future__ import annotations

from maxapi.client.ssl import RUSSIAN_TRUSTED_CA_BUNDLE
from maxapi.connection.base import BaseConnection


def test_russian_trusted_ca_bundle_exists():
    """CA-bundle файл существует рядом с модулем ssl."""
    assert RUSSIAN_TRUSTED_CA_BUNDLE.is_file()


def test_session_uses_russian_trusted_ca():
    """Свежесозданная сессия верифицируется через русский доверенный CA."""
    conn = BaseConnection()
    session = conn._get_session()
    assert session.verify == str(RUSSIAN_TRUSTED_CA_BUNDLE)


def test_external_session_not_overridden():
    """Сессия, заданная извне, не получает принудительный verify."""
    conn = BaseConnection()
    custom = type(conn.session or object())()  # любой объект-заглушка
    conn.session = custom
    assert conn._get_session() is custom
