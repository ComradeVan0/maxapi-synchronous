from __future__ import annotations

import base64
import mimetypes
import os
import re
from datetime import datetime
from io import BytesIO
from json import loads
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlparse

import backoff
import puremagic
from puremagic.main import PureError
from requests import Response, Session
from requests.exceptions import ConnectionError

from ..client.ssl import SSLAdapter
from ..enums.api_path import ApiPath
from ..exceptions.download_file import DownloadFileError
from ..exceptions.max import (
    InvalidToken,
    MaxApiError,
    MaxConnection,
    MaxUploadFileFailed,
)
from ..loggers import logger_bot
from ..types.bot_mixin import BotMixin
from ..utils.runtime import bind_bot
from ..utils.upload_limits import check_upload_size

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pydantic import BaseModel

    from ..bot import Bot
    from ..enums.http_method import HTTPMethod
    from ..enums.upload_type import UploadType


DOWNLOAD_CHUNK_SIZE = 65536

#: Максимальная длина тела ответа в тексте исключения.
ERROR_DETAILS_LIMIT = 512


class _RetryableServerError(Exception):
    """Внутреннее исключение для retry при серверных ошибках.

    Attributes:
        status: HTTP-статус ответа сервера.
        body: Прочитанное тело ответа. Пустая строка, если тело
            не читалось или прочитать его не удалось.
    """

    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self.body = body
        super().__init__(f"Server error {status}")


class NamedBytesIO(BytesIO):
    """
    BytesIO с поддержкой атрибута .name для единообразия с файловыми объектами.
    """

    __slots__ = ("name",)
    name: str | None

    def __init__(
        self, buffer: bytes = b"", *, name: str | None = None
    ) -> None:
        super().__init__(buffer)
        self.name = name


def _on_backoff(details: dict[str, Any]) -> None:
    """Логирование при retry."""
    wait = details["wait"]
    tries = details["tries"]
    exc = details.get("exception")
    if isinstance(exc, _RetryableServerError):
        logger_bot.warning(
            "Серверная ошибка %d, попытка %d, жду %.1fс",
            exc.status,
            tries,
            wait,
        )
    elif isinstance(exc, ConnectionError):
        logger_bot.warning(
            "Ошибка соединения (%s), попытка %d, жду %.1fс",
            exc,
            tries,
            wait,
        )


def _read_response_text(response: Response) -> str:
    """Безопасно читает тело ответа и возвращает его как текст.

    Args:
        response: Ответ requests.

    Returns:
        str: Тело ответа. Пустая строка, если тело прочитать
            не удалось.
    """

    try:
        return response.text
    except Exception as e:
        logger_bot.warning("Не удалось прочитать тело ответа: %s", e)
        return ""


def _read_error_payload(resp: Response) -> dict[str, Any]:
    """Прочитать тело ошибочного ответа, не падая на не-JSON.

    Args:
        resp: Ответ, тело которого нужно прочитать.

    Returns:
        Разобранный JSON-объект, ``{"error": <тело>}`` для остальных
        форматов или пустой словарь, если тело прочитать не удалось.
    """

    try:
        payload = resp.json()
    except Exception:
        text = _read_response_text(resp)
        return {"error": text} if text else {}

    if isinstance(payload, dict):
        return payload
    return {"error": payload} if payload is not None else {}


def _decode_response_body(text: str) -> str | dict[str, Any]:
    """Разбирает тело ответа в dict, если это JSON-объект.

    Args:
        text: Текст тела ответа.

    Returns:
        str | dict: Разобранный JSON-объект либо исходный текст,
            если тело пустое или не является JSON-объектом.
    """

    parsed = _parse_json_object(text)

    return text if parsed is None else parsed


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Парсит тело ответа как JSON-объект.

    Args:
        text: Текст тела ответа.

    Returns:
        dict | None: Разобранный JSON-объект либо None, если тело
            пустое, не является валидным JSON или не является объектом.
    """

    if not text.strip():
        return None

    try:
        parsed = loads(text)
    except ValueError:
        return None

    return parsed if isinstance(parsed, dict) else None


def _error_details(raw: dict[str, Any]) -> str:
    """Отрендерить тело ошибки для текста исключения.

    Не-JSON тело ``_read_error_payload`` кладёт одной строкой под ключ
    ``error``. Её режем напрямую: ``str(raw)`` на многомегабайтной
    странице от прокси заново материализовал бы её целиком (да ещё и
    с экранированием) только ради того, чтобы отбросить всё после
    ``ERROR_DETAILS_LIMIT``.

    Args:
        raw: Тело ответа.

    Returns:
        Диагностика, обрезанная до ``ERROR_DETAILS_LIMIT`` символов.
    """

    text = raw.get("error")
    if len(raw) == 1 and isinstance(text, str):
        details = text
    else:
        details = str(raw)

    if len(details) > ERROR_DETAILS_LIMIT:
        return f"{details[:ERROR_DETAILS_LIMIT]}…"
    return details


def _invalid_token_message(raw: dict[str, Any]) -> str:
    """Собрать текст ``InvalidToken`` с усечённой диагностикой.

    Args:
        raw: Тело ответа 401.

    Returns:
        Сообщение об ошибке; тело обрезается до
        ``ERROR_DETAILS_LIMIT`` символов, чтобы многомегабайтная
        страница от прокси не утекла в логи целиком.
    """

    message = "Неверный токен!"
    if not raw:
        return message

    return f"{message} Ответ API: {_error_details(raw)}"


class BaseConnection(BotMixin):
    """
    Базовый класс для всех методов API.

    Содержит общую логику выполнения запроса (сериализация, отправка
    HTTP-запроса, обработка ответа).
    """

    API_URL = "https://platform-api2.max.ru"
    RETRY_DELAY = 2
    ATTEMPTS_COUNT = 5
    AFTER_MEDIA_INPUT_DELAY = 2.0

    def __init__(self) -> None:
        """
        Инициализация BaseConnection.

        Атрибуты:
            bot: Экземпляр бота.
            session: requests-сессия.
            after_input_media_delay: Задержка после ввода медиа.
        """

        self.bot: Bot | None = None
        self.session: Session | None = None
        self.after_input_media_delay: float = self.AFTER_MEDIA_INPUT_DELAY
        self.api_url = self.API_URL

    def set_api_url(self, url: str) -> None:
        """
        Установка API URL для запросов.

        Args:
            url: Новый API URL.
        """

        self.api_url = url.rstrip("/")

    def _get_session(self) -> Session:
        """Возвращает активную HTTP-сессию, создавая при необходимости.

        Свежесозданная сессия использует :class:`maxapi.client.ssl.SSLAdapter`
        (системное хранилище + русский доверенный CA). Если сессия задана
        извне (``self.session``), она используется как есть.
        """
        if self.session is None:
            self.session = Session()
            self.session.mount("https://", SSLAdapter())
        return self.session

    def request(
        self,
        method: HTTPMethod,
        path: ApiPath | str,
        model: BaseModel | Any = None,
        *,
        is_return_raw: bool = False,
        **kwargs: Any,
    ) -> Any | BaseModel:
        """
        Выполняет HTTP-запрос к API с автоматическим retry
        при серверных ошибках.

        При получении HTTP-статуса из списка ``retry_on_statuses``
        (по умолчанию 502, 503, 504) запрос повторяется до
        ``max_retries`` раз с экспоненциальной задержкой.

        Args:
            method: HTTP-метод (GET, POST и т.д.).
            path: Путь до конечной точки.
            model: Pydantic-модель для десериализации ответа,
                если is_return_raw=False.
            is_return_raw: Если True — вернуть сырой ответ,
                иначе — результат десериализации.
            **kwargs: Дополнительные параметры (params, headers, json).

        Returns:
            model | dict | Error: Объект модели, dict или ошибка.

        Raises:
            RuntimeError: Если бот не инициализирован.
            MaxConnection: Ошибка соединения.
            InvalidToken: Ошибка авторизации (401).
            MaxApiError: Ошибка API (после исчерпания retry), а также
                если успешный ответ не содержит JSON-объекта.
        """

        bot = self._ensure_bot()
        conn = bot.default_connection
        retry_statuses = conn.retry_on_statuses

        path_str = path.value if isinstance(path, ApiPath) else path
        url = bot.api_url + path_str

        kwargs.setdefault("timeout", conn.timeout)
        kwargs.setdefault("headers", bot.headers)

        @backoff.on_exception(
            backoff.expo,
            (ConnectionError, _RetryableServerError),
            max_tries=conn.max_retries + 1,
            factor=conn.retry_backoff_factor,
            on_backoff=_on_backoff,
        )
        def _do_request() -> Response:
            resp = self._get_session().request(
                method=method.value,
                url=url,
                **kwargs,
            )

            if resp.status_code == 401:
                raise InvalidToken(
                    _invalid_token_message(_read_error_payload(resp))
                )

            if resp.status_code in retry_statuses:
                raise _RetryableServerError(
                    resp.status_code, _read_response_text(resp)
                )

            return resp

        try:
            response = _do_request()
        except ConnectionError as e:
            raise MaxConnection(f"Ошибка при отправке запроса: {e}") from e
        except _RetryableServerError as e:
            raw = _decode_response_body(e.body)
            raise MaxApiError(code=e.status, raw=raw) from e

        text = _read_response_text(response)

        if not response.ok:
            raw = _decode_response_body(text)
            raise MaxApiError(code=response.status_code, raw=raw)

        parsed = _parse_json_object(text)

        if parsed is None:
            # API всегда отвечает JSON-объектом: пустое или не-JSON
            # тело при 2xx — такой же сбой, как и не-2xx ответ.
            raise MaxApiError(code=response.status_code, raw=text)

        if is_return_raw:
            return parsed

        model = model(**parsed)  # type: ignore

        return bind_bot(model, bot)

    @staticmethod
    def _read_upload_response(response: Response) -> str:
        """
        Проверяет статус ответа upload-сервера и возвращает его тело.

        Args:
            response: Ответ upload-сервера.

        Returns:
            str: Сырой .text ответ от сервера.

        Raises:
            MaxUploadFileFailed: Если статус ответа не успешный
                или тело ответа не удалось прочитать.
        """

        try:
            text = response.text
        except Exception as e:
            raise MaxUploadFileFailed(
                f"Не удалось прочитать ответ upload-сервера: {e}"
            ) from e

        # upload-сервер обязан отвечать строго 2xx (3xx — не успех).
        if not 200 <= response.status_code < 300:
            raise MaxUploadFileFailed(
                f"Ошибка при загрузке файла: HTTP {response.status_code}, "
                f"ответ: {text}"
            )

        return text

    def upload_file(self, url: str, path: str, type: UploadType) -> str:
        """
        Загружает файл на сервер.

        При превышении лимита загрузки MAX пишется предупреждение
        в логгер `bot`, загрузка не прерывается.

        Args:
            url: URL загрузки.
            path: Путь к файлу.
            type: Тип файла.

        Returns:
            str: Сырой .text ответ от сервера.

        Raises:
            MaxUploadFileFailed: Если upload-сервер вернул не-2xx ответ.
        """

        with open(path, "rb") as f:
            file_data = f.read()

        path_object = Path(path)
        basename = path_object.name
        mime_type = mimetypes.guess_type(path)[0] or f"{type.value}/*"

        check_upload_size(len(file_data), type, name=basename)

        bot = self._ensure_bot()

        response = self._get_session().post(
            url=url,
            files={"data": (basename, file_data, mime_type)},
            headers=bot.headers,
        )
        return self._read_upload_response(response)

    def upload_file_buffer(
        self, filename: str, url: str, buffer: bytes, type: UploadType
    ) -> str:
        """
        Загружает файл из буфера.

        При превышении лимита загрузки MAX пишется предупреждение
        в логгер `bot`, загрузка не прерывается.

        Args:
            filename: Имя файла.
            url: URL загрузки.
            buffer: Буфер данных.
            type: Тип файла.

        Returns:
            str: Сырой .text ответ от сервера.

        Raises:
            MaxUploadFileFailed: Если upload-сервер вернул не-2xx ответ.
        """

        check_upload_size(len(buffer), type, name=filename)

        try:
            matches = puremagic.magic_string(buffer[:4096])
            if matches:
                mime_type = matches[0].mime_type
                ext = mimetypes.guess_extension(mime_type) or ""
            else:
                mime_type = f"{type.value}/*"
                ext = ""
        except (OSError, ValueError, AttributeError, PureError):
            mime_type = f"{type.value}/*"
            ext = ""

        basename = f"{filename}{ext}"

        bot = self._ensure_bot()

        response = self._get_session().post(
            url=url,
            files={"data": (basename, buffer, mime_type)},
            headers=bot.headers,
        )
        return self._read_upload_response(response)

    def _fetch_response(self, url: str) -> Response:
        """
        Выполняет GET-запрос с retry при серверных ошибках.

        Args:
            url: URL файла.

        Returns:
            Response: Объект ответа.

        Raises:
            DownloadFileError: При ошибке сети или HTTP.
        """
        bot = self._ensure_bot()
        conn = bot.default_connection
        session = self._get_session()

        @backoff.on_exception(
            backoff.expo,
            (ConnectionError, _RetryableServerError),
            max_tries=conn.max_retries + 1,
            factor=conn.retry_backoff_factor,
            on_backoff=_on_backoff,
        )
        def _do_fetch() -> Response:
            resp = session.get(url)
            if resp.status_code in conn.retry_on_statuses:
                resp.close()
                raise _RetryableServerError(resp.status_code)
            return resp

        try:
            response = _do_fetch()
        except ConnectionError as e:
            raise DownloadFileError(f"Ошибка при скачивании файла: {e}") from e
        except _RetryableServerError as e:
            raise DownloadFileError(
                f"Ошибка при скачивании файла: HTTP {e.status}"
            ) from e

        if not response.ok:
            response.close()
            raise DownloadFileError(
                f"Ошибка при скачивании файла: HTTP {response.status_code}"
            )

        return response

    def _fetch_content_stream(
        self,
        response: Response,
        *,
        chunk_size: int = DOWNLOAD_CHUNK_SIZE,
    ) -> Iterator[bytes]:
        """
        Генератор, который отдаёт чанки файла по мере скачивания.

        Args:
            response: Предварительно полученный Response.
            chunk_size: Размер чанка в байтах.

        Yields:
            bytes: Чанки данных файла.

        Raises:
            DownloadFileError: При недопустимом статусе ответа.
        """
        if not response.ok:
            raise DownloadFileError(
                f"Ошибка при скачивании: HTTP {response.status_code}"
            )

        try:
            for chunk in response.iter_content(chunk_size=chunk_size):
                yield chunk
        finally:
            response.close()

    @staticmethod
    def _get_image_id(r: str) -> str | None:
        """
        Извлекает уникальную часть из токена изображения ссылки вида
        https://i.oneme.ru/i?r=image_token_base64url

        Args:
            r: Параметр из url.

        Returns:
            str: Уникальная часть токена.
            None: В случае ошибки или неверного формата.
        """
        # Добавляем паддинг и конвертируем base64url
        r += "=" * (-len(r) % 4)
        # Конвертируем base64url в стандартный base64
        r = r.replace("-", "+").replace("_", "/")
        try:
            data = base64.b64decode(r)
        except Exception:
            return None

        if len(data) < 50:
            return None

        # Заголовок и хвост одинаковы для ссылок одного бота.
        # Уникальный идентификатор изображения для текущего бота.
        image_id = base64.urlsafe_b64encode(data[18:-16]).rstrip(b"=").decode()
        return image_id

    def _capture_filename(self, response: Response) -> str:
        """
        Получает имя файла из заголовков ответа.

        Используется в download_file / download_bytes_io.

        Args:
            response: Ответ сервера с заголовками файла.

        Returns:
            str: Имя файла из заголовков.
            Если не удалось определить, возвращается default
            в формате %y%m%d_%H%M%S.ext.
        """
        filename = ext = ""
        datetime_str = datetime.now().strftime("%y%m%d_%H%M%S")

        try:
            cd = response.headers.get("Content-Disposition")
            if cd:
                for part in (p.strip() for p in cd.split(";")):
                    if part.lower().startswith("filename="):
                        filename = part.split("=", 1)[1].strip('"').strip("'")
                        break
                if filename:
                    ext = Path(filename).suffix
            else:
                # Нет Content-Disposition — fallback на имя из URL (PR #112).
                url_str = str(response.url) if response.url else ""
                if url_str.startswith(("http://", "https://")):
                    parsed = urlparse(url_str)
                    url_name = Path(parsed.path).name
                    if url_name:
                        filename = url_name
                        ext = Path(filename).suffix
                        if not ext:
                            content_type = response.headers.get(
                                "Content-Type", ""
                            )
                            if content_type:
                                g_ext = mimetypes.guess_extension(content_type)
                                if g_ext:
                                    ext = g_ext
                                    filename = f"{filename}{ext}"

            # Серверы Max возвращают имя файла дважды закодированным.
            if filename and re.search(r"%[0-9A-Fa-f]{2}", filename):
                filename = unquote(filename, encoding="utf-8")

            if filename:
                filename = Path(filename).name  # защита от path traversal

            # Специальные случаи для i.oneme.ru (стикеры, изображения).
            url_str = str(response.url) if response.url else ""
            if url_str.startswith(("http://", "https://")):
                parsed = urlparse(url_str)
                host = parsed.hostname or ""
                url_path = parsed.path.rstrip("/")
                query = parse_qs(parsed.query)

                if host == "i.oneme.ru":
                    content_type = response.headers.get("Content-Type", "")
                    guessed_ext = (
                        mimetypes.guess_extension(content_type)
                        if content_type
                        else ""
                    )

                    # is_sticker
                    if url_path == "/getSmile":
                        ext = guessed_ext or ext
                        if not ext or ext == ".bin":
                            ext = ".png"
                        smile_id = query.get("smileId", [None])[0]
                        if smile_id:
                            filename = f"sticker_{smile_id}{ext}"
                        else:
                            filename = f"sticker_{datetime_str}{ext}"

                    # is_image
                    elif url_path == "/i":
                        ext = guessed_ext or ext
                        if not ext or ext == ".bin":
                            ext = ".webp"
                        r_value = query.get("r", [None])[0]
                        if r_value:
                            image_id = self._get_image_id(r_value)
                            if image_id:
                                filename = f"image_{image_id}{ext}"
                            else:
                                filename = f"image_{datetime_str}{ext}"
                        else:
                            filename = f"image_{datetime_str}{ext}"

            # Если имя не определилось — fallback на file.ext по Content-Type.
            if not filename:
                content_type = response.headers.get("Content-Type", "")
                if content_type:
                    ext = mimetypes.guess_extension(content_type) or ""
                    filename = f"file{ext}"

            # Финальный fallback, если имя всё ещё пустое.
            if not filename or filename.startswith("."):
                if not ext:
                    ext = ".bin"
                filename = f"{datetime_str}{ext}"

        except (AttributeError, TypeError, ValueError) as e:
            logger_bot.warning(
                "Не удалось определить имя файла из заголовков: %s", e
            )
            if not filename:
                filename = f"{datetime_str}.bin"

        return filename

    @staticmethod
    def _check_file_exists(path: Path | str) -> Path:
        """
        Если файл существует, возвращает новый свободный путь для сохранения
        Windows style:
        - file_name.ext
        - file_name(2).ext
        - file_name(3).ext

        Args:
            path: Путь к файлу.

        Returns:
            pathlib.Path: Свободное имя файла с путём для сохранения.
        """
        path = Path(path)

        if path.exists():
            max_num = 1  # Один уже существует
            fname, ext = path.stem, path.suffix
            pattern = re.compile(
                rf"^{re.escape(fname)}\((\d+)\){re.escape(ext)}$"
            )

            # Сканируем директорию
            dest = path.parent
            for existing_path in dest.iterdir():
                match = pattern.match(existing_path.name)
                if match:
                    num = int(match.group(1))
                    if num > max_num:
                        max_num = num

            path = dest / f"{fname}({max_num + 1}){ext}"

        return path

    def download_file(
        self,
        url: str,
        destination: Path | str,
        *,
        filename: Path | str | None = None,
        chunk_size: int = DOWNLOAD_CHUNK_SIZE,
    ) -> Path:
        """
        Скачивает файл по URL и сохраняет на диск.

        URL можно получить из payload вложения:
        - Изображение: ``attachment.payload.url``
        - Видео: ``attachment.urls.mp4_720`` (или другое разрешение)
        - Аудио/Файл: ``attachment.payload.url``
        - Стикер: ``attachment.payload.url``

        Метод работает не через общий ``request()``, поскольку
        ответом является бинарный поток, а не JSON.

        Если файл существует, возвращается новый свободный путь
        для сохранения (Windows style):
        - file_name.ext
        - file_name(2).ext
        - file_name(3).ext

        Args:
            url: URL файла для скачивания (из payload.url вложения).
            destination: Путь к директории для сохранения файла.
            filename: Имя файла для сохранения. Если не указано,
                используется имя от сервера или значение по умолчанию.
            chunk_size: Размер чанка при потоковом чтении
                (по умолчанию 64 КБ).

        Returns:
            Path: Полный путь к скачанному файлу.

        Raises:
            DownloadFileError: При ошибке скачивания.
            FileExistsError, NotADirectoryError, PermissionError, OSError:
                при ошибках файловой системы.
        """
        dest = Path(destination)
        final_path: Path | None = None

        # Получаем ответ для определения имени файла из заголовков.
        response = self._fetch_response(url)

        try:
            os.makedirs(dest, exist_ok=True)
        except (FileExistsError, NotADirectoryError, PermissionError, OSError):
            # Если передан файл вместо директории, путь ошибочен
            # или нет прав доступа.
            response.close()
            raise

        try:
            if filename:
                # Выделяем только имя файла,
                # на случай если переменная содержит путь.
                filename = Path(filename).name
            else:
                filename = self._capture_filename(response)

            final_path = self._check_file_exists(dest / filename)
            with open(final_path, "wb") as f:
                f.writelines(
                    self._fetch_content_stream(response, chunk_size=chunk_size)
                )
        except Exception:
            # При любой ошибке удаляем частично записанный файл.
            if final_path and final_path.exists():
                final_path.unlink()
            raise
        finally:
            response.close()

        return final_path

    def download_bytes_io(
        self,
        url: str,
        *,
        chunk_size: int = DOWNLOAD_CHUNK_SIZE,
    ) -> NamedBytesIO:
        """
        Скачивает файл по URL и возвращает file-like объект в памяти.

        Внимание: весь файл загружается в оперативную память.
        Не используйте для файлов >100–200 МБ без контроля.

        Args:
            url: URL файла.
            chunk_size: Размер чанка при потоковом чтении.

        Returns:
            NamedBytesIO: Содержимое файла с атрибутом .name.
            Наследуется от io.BytesIO.
            Для zero-copy передачи используйте .getbuffer(),
            для получения bytes — .read() или .getvalue().

        Raises:
            DownloadFileError: При ошибке скачивания.
        """
        bio = NamedBytesIO()

        response = self._fetch_response(url)
        bio.name = self._capture_filename(response)

        try:
            for chunk in self._fetch_content_stream(
                response,
                chunk_size=chunk_size,
            ):
                bio.write(chunk)
        finally:
            response.close()

        bio.seek(0)  # обязательно переходим в начало

        return bio

    def download_bytes(
        self,
        url: str,
        *,
        chunk_size: int = DOWNLOAD_CHUNK_SIZE,
    ) -> bytes:
        """
        Скачивает файл по URL и возвращает bytes в памяти.

        Внимание: весь файл загружается в оперативную память.
        Не используйте для файлов >100–200 МБ без контроля.

        Args:
            url: URL файла.
            chunk_size: Размер чанка при потоковом чтении.

        Returns:
            bytes: Содержимое файла.

        Raises:
            DownloadFileError: При ошибке скачивания.
        """
        bio = self.download_bytes_io(url=url, chunk_size=chunk_size)

        return bio.read()
