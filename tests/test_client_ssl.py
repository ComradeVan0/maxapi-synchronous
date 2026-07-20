"""Тесты SSL-настроек requests-клиента (системное хранилище + русский CA)."""

from __future__ import annotations

from maxapi.client.ssl import (
    RUSSIAN_TRUSTED_CA_BUNDLE,
    SSLAdapter,
    create_default_ssl_context,
)
from maxapi.connection.base import BaseConnection


def test_russian_trusted_ca_bundle_exists():
    """CA-bundle файл существует рядом с модулем ssl."""
    assert RUSSIAN_TRUSTED_CA_BUNDLE.is_file()


def test_ssl_context_loads_ca():
    """SSL-контекст (system + русский CA) создаётся и грузит CA."""
    ctx = create_default_ssl_context()
    assert ctx.get_ca_certs()  # системные + pem


def test_session_mounts_ssl_adapter():
    """Свежесозданная сессия использует SSLAdapter для https."""
    conn = BaseConnection()
    session = conn._get_session()
    adapter = session.get_adapter("https://example.com")
    assert isinstance(adapter, SSLAdapter)


def test_external_session_not_overridden():
    """Сессия, заданная извне, не получает принудительный adapter."""
    conn = BaseConnection()
    conn.session = object()
    assert conn._get_session() is conn.session
