"""
Модуль скачивания аудио с YouTube с использованием yt-dlp.
Поддерживает ссылки youtube.com/watch, youtube.com/live и youtu.be.
Скачивает лучшую аудиодорожку без видеопотока (предпочтительно M4A).
"""

import os
import re
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Any

import yt_dlp

# Обеспечиваем доступ к Homebrew бинарникам (deno, node, ffmpeg) на macOS
if "/opt/homebrew/bin" not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"/opt/homebrew/bin:{os.environ.get('PATH', '')}"

logger = logging.getLogger(__name__)

# Регулярные выражения для распознавания YouTube-ссылок
YOUTUBE_PATTERNS = [
    re.compile(r"https?://(?:www\.|m\.)?youtube\.com/watch\?[^\s]*v=([a-zA-Z0-9_-]{11})[^\s]*"),
    re.compile(r"https?://(?:www\.|m\.)?youtube\.com/live/([a-zA-Z0-9_-]{11})(?:\?[^\s]*)?"),
    re.compile(r"https?://youtu\.be/([a-zA-Z0-9_-]{11})(?:\?[^\s]*)?"),
    re.compile(r"https?://(?:www\.|m\.)?youtube\.com/shorts/([a-zA-Z0-9_-]{11})(?:\?[^\s]*)?"),
]


class DownloaderError(Exception):
    """Базовое исключение для ошибок скачивания."""
    pass


class InvalidURLError(DownloaderError):
    """Некорректная ссылка на видео."""
    pass


class LivestreamActiveError(DownloaderError):
    """Прямой эфир еще не завершен."""
    pass


class VideoUnavailableError(DownloaderError):
    """Видео недоступно (приватное, удалено, геоблок или ограничения доступа)."""
    pass


class NetworkDownloadError(DownloaderError):
    """Сетевая ошибка при скачивании."""
    pass


class PermissionOrDiskError(DownloaderError):
    """Ошибка прав доступа или дискового пространства."""
    pass


@dataclass
class DownloadResult:
    """Результаты успешного скачивания аудиодорожки."""
    title: str
    duration_seconds: int
    duration_str: str
    audio_format: str
    bitrate_kbps: Optional[int]
    file_size_mb: float
    file_size_bytes: int
    download_time_seconds: float
    speed_mb_s: float
    file_path: str
    chapters: List[Dict[str, Any]]


def extract_youtube_url(text: str) -> Optional[str]:
    """
    Находит первую YouTube-ссылку в тексте сообщения и возвращает нормализованный URL.
    """
    if not text:
        return None

    for pattern in YOUTUBE_PATTERNS:
        match = pattern.search(text)
        if match:
            # Возвращаем найденную ссылку
            return match.group(0).strip()
    return None


def is_valid_youtube_url(url: str) -> bool:
    """Проверяет, является ли строка валидной ссылкой на YouTube."""
    if not url:
        return False
    return any(pattern.search(url) is not None for pattern in YOUTUBE_PATTERNS)


def format_duration(seconds: Optional[int]) -> str:
    """
    Форматирует секунды в человекочитаемую строку: 'X ч Y мин Z сек' или 'Y мин Z сек'.
    """
    if seconds is None or seconds < 0:
        return "Не определена"

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    parts = []
    if hours > 0:
        parts.append(f"{hours} ч")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes} мин")
    parts.append(f"{secs} сек")

    return " ".join(parts)


def format_size(bytes_count: int) -> str:
    """Форматирует размер в байтах в мегабайты."""
    mb = bytes_count / (1024 * 1024)
    return f"{mb:.2f} МБ"


def sanitize_filename(filename: str) -> str:
    """Удаляет из имени файла символы, запрещенные в файловых системах."""
    # Заменяем недопустимые символы на дефис или подчеркивание
    sanitized = re.sub(r'[\\/*?:"<>|]', "_", filename)
    # Сжимаем повторяющиеся пробелы и подчеркивания
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    # Ограничиваем длину имени
    if len(sanitized) > 150:
        sanitized = sanitized[:150].strip()
    return sanitized or "audio"


def update_downloader_engine() -> tuple[bool, str]:
    """
    Обновляет движок yt-dlp через pip до последней актуальной версии.
    Возвращает (успех, номер_версии_или_текст_ошибки).
    """
    import subprocess
    import sys
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0:
            ver_proc = subprocess.run(
                [sys.executable, "-c", "import yt_dlp; print(yt_dlp.__version__)"],
                capture_output=True,
                text=True,
            )
            ver = ver_proc.stdout.strip() or "актуальная"
            logger.info("yt-dlp engine updated to version %s", ver)
            return True, ver
        else:
            logger.error("Failed to update yt-dlp engine: %s", proc.stderr)
            return False, proc.stderr
    except Exception as e:
        logger.exception("Exception during yt-dlp update")
        return False, str(e)


class YouTubeAudioDownloader:
    """
    Класс для проверки и скачивания аудиодорожек с YouTube.
    """

    def __init__(self, download_dir: str):
        self.download_dir = Path(download_dir).resolve()
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def _get_ydl_options(self) -> Dict[str, Any]:
        """
        Возвращает параметры yt-dlp, оптимизированные для скачивания чистого аудио.
        """
        # Безопасный шаблон имени файла
        outtmpl = str(self.download_dir / "%(title).120B [%(id)s].%(ext)s")

        opts = {
            # Приоритет чистого M4A (itag 140), затем любое лучшее чистое аудио (Opus/WebM)
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "retries": 3,
            "fragment_retries": 3,
            "quiet": True,
            "no_warnings": False,
            "socket_timeout": 30,
            "ffmpeg_location": "/opt/homebrew/bin",
        }

        # Используем файл cookies только если он явно задан в конфигурации (.env)
        try:
            import config
            if getattr(config, "COOKIES_FILE", None) and Path(config.COOKIES_FILE).is_file():
                opts["cookiefile"] = config.COOKIES_FILE
                logger.info("Using cookies file from config: %s", config.COOKIES_FILE)
        except ImportError:
            pass

        return opts

    def check_video_info(self, url: str) -> Dict[str, Any]:
        """
        Получает метаданные видео без его скачивания и проверяет готовность к загрузке.
        """
        if not is_valid_youtube_url(url):
            raise InvalidURLError("Предоставленная ссылка не является поддерживаемой ссылкой на YouTube.")

        opts = self._get_ydl_options()
        opts["skip_download"] = True

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise VideoUnavailableError("Не удалось получить информацию о видео.")

                # Проверяем статус прямого эфира
                is_live = info.get("is_live", False)
                live_status = info.get("live_status")
                was_live = info.get("was_live", False)

                if is_live or live_status in ("is_live", "is_upcoming"):
                    raise LivestreamActiveError(
                        "Этот прямой эфир еще идет или запланирован. "
                        "Скачивание возможно только после полного завершения трансляции."
                    )

                return info

        except yt_dlp.utils.DownloadError as e:
            err_msg = str(e)
            logger.error("yt-dlp error during check_video_info: %s", err_msg)
            if "Private video" in err_msg or "Sign in" in err_msg:
                raise VideoUnavailableError("Видео приватное или требует авторизации.")
            elif "Video unavailable" in err_msg or "deleted" in err_msg:
                raise VideoUnavailableError("Видео недоступно или удалено.")
            elif "not available in your country" in err_msg or "georestricted" in err_msg:
                raise VideoUnavailableError("Видео заблокировано для данного региона.")
            elif "The page needs to be reloaded" in err_msg:
                raise VideoUnavailableError("YouTube запросил перезагрузку страницы (проверка защиты).")
            elif "Network is unreachable" in err_msg or "nodename nor servname" in err_msg or "timed out" in err_msg:
                raise NetworkDownloadError(f"Сетевая ошибка при обращении к YouTube: {err_msg}")
            else:
                raise DownloaderError(f"Ошибка проверки видео: {err_msg}")
        except PermissionError as e:
            raise PermissionOrDiskError(f"Ошибка прав доступа к файловой системе: {e}")
        except Exception as e:
            if isinstance(e, DownloaderError):
                raise
            logger.exception("Unexpected error in check_video_info")
            raise DownloaderError(f"Непредвиденная ошибка: {e}")

    def download_audio(self, url: str, max_retries: int = 3) -> DownloadResult:
        """
        Скачивает лучшую доступную аудиодорожку (предпочтительно M4A) в папку downloads.
        При временных сетевых сбоях повторяет попытку до max_retries раз.
        """
        # Сначала проверяем доступность и статус трансляции
        info = self.check_video_info(url)

        title = info.get("title", "Без названия")
        duration = info.get("duration")
        duration_str = format_duration(duration)

        opts = self._get_ydl_options()

        last_error = None
        for attempt in range(1, max_retries + 1):
            start_time = time.perf_counter()
            try:
                logger.info("Starting download attempt %d/%d for '%s'", attempt, max_retries, title)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    # Запуск скачивания
                    download_info = ydl.extract_info(url, download=True)
                    if not download_info:
                        raise VideoUnavailableError("Не удалось получить данные для скачивания.")

                    # Определяем путь к сохраненному файлу
                    downloaded_file = ydl.prepare_filename(download_info)

                    # Проверяем, существует ли файл
                    file_path = Path(downloaded_file)
                    if not file_path.exists():
                        # Возможно, расширение изменилось после конвертации/извлечения (например, в .m4a)
                        # Проверяем файлы с тем же id в папке
                        video_id = download_info.get("id")
                        matching = [
                            f for f in self.download_dir.glob(f"*{video_id}*")
                            if not f.name.endswith(".part") and not f.name.endswith(".ytdl")
                        ]
                        if matching:
                            matching.sort(key=lambda p: (p.suffix.lower() == ".m4a", p.stat().st_mtime), reverse=True)
                            file_path = matching[0]
                        else:
                            raise DownloaderError("Файл не был найден на диске после скачивания.")

                    end_time = time.perf_counter()
                    download_time = max(end_time - start_time, 0.01)

                    file_size_bytes = file_path.stat().st_size
                    file_size_mb = file_size_bytes / (1024 * 1024)
                    speed_mb_s = file_size_mb / download_time

                    audio_format = file_path.suffix.lstrip(".").upper() or download_info.get("ext", "").upper()
                    bitrate = download_info.get("abr") or download_info.get("tbr")
                    bitrate_kbps = int(bitrate) if bitrate else None

                    # Главы/таймкоды
                    raw_chapters = download_info.get("chapters") or []
                    chapters = []
                    for ch in raw_chapters:
                        ch_start = ch.get("start_time", 0)
                        ch_title = ch.get("title", "Без названия")
                        chapters.append({
                            "title": ch_title,
                            "start_time": ch_start,
                            "start_time_str": format_duration(int(ch_start)),
                        })

                    return DownloadResult(
                        title=title,
                        duration_seconds=duration or 0,
                        duration_str=duration_str,
                        audio_format=audio_format,
                        bitrate_kbps=bitrate_kbps,
                        file_size_mb=round(file_size_mb, 2),
                        file_size_bytes=file_size_bytes,
                        download_time_seconds=round(download_time, 2),
                        speed_mb_s=round(speed_mb_s, 2),
                        file_path=str(file_path),
                        chapters=chapters,
                    )

            except (yt_dlp.utils.DownloadError, ConnectionError, OSError) as e:
                err_str = str(e)
                last_error = e
                logger.warning("Attempt %d failed: %s", attempt, err_str)
                if attempt < max_retries:
                    # Пауза перед повторной попыткой
                    time.sleep(2 * attempt)
                    continue
                else:
                    if "Permission denied" in err_str:
                        raise PermissionOrDiskError(f"Нет прав на запись в папку {self.download_dir}")
                    elif "No space left on device" in err_str:
                        raise PermissionOrDiskError("Недостаточно места на диске для сохранения файла.")
                    elif any(net_term in err_str for net_term in ("timed out", "Network is unreachable", "nodename nor servname", "Connection reset")):
                        raise NetworkDownloadError(f"Сетевой сбой после {max_retries} попыток: {err_str}")
                    else:
                        raise DownloaderError(f"Ошибка при скачивании после {max_retries} попыток: {err_str}")

        raise DownloaderError(f"Не удалось скачать аудио: {last_error}")
