"""SSL-настройки HTTP-клиента (requests): системное хранилище + русский CA.

API MAX использует сертификат, подписанный российским доверенным CA,
который может отсутствовать в системном хранилище ОС. SSL-контекст
комбинирует системное хранилище и CA-bundle ``russiantrustedca.pem`` и
монтируется на сессию через :class:`SSLAdapter`
(см. ``BaseConnection._get_session``).

Внимание: ``session.verify = pem`` доверял бы ТОЛЬКО pem — этого
недостаточно (цепочка MAX API требует и системные CA). Поэтому используем
полноценный SSL-контекст (аналог upstream'ого aiohttp-коннектора).
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

from requests.adapters import HTTPAdapter

#: Путь к CA-bundle русского доверенного CA (рядом с этим модулем).
RUSSIAN_TRUSTED_CA_BUNDLE = Path(__file__).with_name("russiantrustedca.pem")


def create_default_ssl_context() -> ssl.SSLContext:
    """SSL-контекст: системное хранилище + русский доверенный CA.

    Доверяем системному хранилищу (``ssl.create_default_context``) и
    ДОПОЛНИТЕЛЬНО — русскому CA (``load_verify_locations``).
    """
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=str(RUSSIAN_TRUSTED_CA_BUNDLE))
    return context


class SSLAdapter(HTTPAdapter):
    """HTTPAdapter с кастомным SSL-контекстом для requests.

    По умолчанию использует :func:`create_default_ssl_context`
    (системное хранилище + русский CA).
    """

    def __init__(
        self,
        ssl_context: ssl.SSLContext | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.ssl_context = ssl_context or create_default_ssl_context()
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        kwargs["ssl_context"] = self.ssl_context
        super().init_poolmanager(*args, **kwargs)
