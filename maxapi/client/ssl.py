"""SSL-настройки HTTP-клиента (requests): доверенный российский CA.

API MAX использует сертификат, подписанный российским доверенным CA,
который может отсутствовать в системном хранилище ОС. CA-bundle
``russiantrustedca.pem`` подключается к ``requests`` через ``verify``
(см. ``BaseConnection._get_session``).
"""

from __future__ import annotations

from pathlib import Path

#: Путь к CA-bundle русского доверенного CA (рядом с этим модулем).
RUSSIAN_TRUSTED_CA_BUNDLE = Path(__file__).with_name("russiantrustedca.pem")
