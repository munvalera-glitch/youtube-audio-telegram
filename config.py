"""
Конфигурация Telegram-бота для скачивания аудио с YouTube.
Загружает переменные окружения из файла .env.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Загружаем .env
load_dotenv()

# Токен Telegram-бота
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Список разрешенных Telegram ID пользователей
_raw_allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS: set[int] = set()
if _raw_allowed:
    for uid in _raw_allowed.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ALLOWED_USER_IDS.add(int(uid))

# Папка для сохранения скачанных аудиодорожек
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./downloads").strip()
download_path = Path(DOWNLOAD_DIR).resolve()
download_path.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR_PATH = str(download_path)

# Путь к файлу cookies (если YouTube требует авторизации / обхода anti-bot)
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
if not COOKIES_FILE and (Path("./cookies.txt").exists()):
    COOKIES_FILE = "./cookies.txt"
if COOKIES_FILE:
    COOKIES_FILE = str(Path(COOKIES_FILE).resolve())


# Путь к базе данных SQLite (пользователи, заявки, права доступа)
DB_PATH = str(Path(os.getenv("DB_PATH", "./bot_data.db")).resolve())


def is_user_allowed(user_id: int) -> bool:
    """Проверяет, разрешен ли доступ данному Telegram-пользователю."""
    if not ALLOWED_USER_IDS:
        # Если список пуст, доступ никому не разрешен до настройки админом
        return False
    return user_id in ALLOWED_USER_IDS
