from .base import MaxError
from .download_file import DownloadFileError, NotAvailableForDownload
from .max import (
    InvalidToken,
    MaxApiError,
    MaxConnection,
    MaxIconParamsException,
    MaxUploadFileFailed,
)

__all__ = [
    "DownloadFileError",
    "InvalidToken",
    "MaxApiError",
    "MaxConnection",
    "MaxError",
    "MaxIconParamsException",
    "MaxUploadFileFailed",
    "NotAvailableForDownload",
]
