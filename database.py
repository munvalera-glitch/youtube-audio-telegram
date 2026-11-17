"""
Модуль хранения данных (SQLite) для системы регистрации пользователей,
заявок на доступ и административного управления правами.
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional


STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_BLOCKED = "blocked"

VALID_STATUSES = {STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_BLOCKED}


@dataclass
class UserRecord:
    """Модель записи пользователя в базе данных."""
    user_id: int
    username: Optional[str]
    tg_name: str
    full_name: str
    ministry: str
    reason: str
    status: str
    is_admin: bool
    created_at: str
    updated_at: str


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_date_key(title: str) -> str:
    """Извлекает дату из названия и приводит к формату YYYY-MM-DD для сортировки по возрастанию."""
    m_ymd = re.search(r"(\d{4})[-./](\d{2})[-./](\d{2})", title)
    if m_ymd:
        return f"{m_ymd.group(1)}-{m_ymd.group(2)}-{m_ymd.group(3)}"
    m_dmy = re.search(r"(\d{2})[-./](\d{2})[-./](\d{4})", title)
    if m_dmy:
        return f"{m_dmy.group(3)}-{m_dmy.group(2)}-{m_dmy.group(1)}"
    return "9999-99-99"


def _row_to_user(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        user_id=row["user_id"],
        username=row["username"],
        tg_name=row["tg_name"] or "",
        full_name=row["full_name"] or "",
        ministry=row["ministry"] or "",
        reason=row["reason"] or "",
        status=row["status"],
        is_admin=bool(row["is_admin"]),
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


class Database:
    """Класс для работы с базой данных пользователей и заявок (SQLite)."""

    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path).resolve())
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        """Создает таблицы при первом запуске."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    tg_name TEXT NOT NULL DEFAULT '',
                    full_name TEXT NOT NULL DEFAULT '',
                    ministry TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS split_sessions (
                    user_id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    is_hip INTEGER NOT NULL DEFAULT 0,
                    leader TEXT,
                    awaiting_timestamps INTEGER NOT NULL DEFAULT 0,
                    ocr_times TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Миграция для существующей БД без колонки ocr_times
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(split_sessions)").fetchall()]
            if "ocr_times" not in cols:
                conn.execute("ALTER TABLE split_sessions ADD COLUMN ocr_times TEXT NOT NULL DEFAULT '[]'")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS worship_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    date_key TEXT NOT NULL,
                    duration INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def bootstrap_admins(self, admin_ids: set[int]) -> None:
        """
        Гарантирует, что все ID из ALLOWED_USER_IDS (.env) присутствуют в БД
        со статусом 'approved' и правами администратора.
        """
        if not admin_ids:
            return
        now = _now_str()
        with self._connect() as conn:
            for uid in admin_ids:
                existing = conn.execute(
                    "SELECT user_id FROM users WHERE user_id = ?", (uid,)
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO users (
                            user_id, username, tg_name, full_name, ministry, reason,
                            status, is_admin, created_at, updated_at
                        ) VALUES (?, NULL, '', ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            uid,
                            "Главный администратор",
                            "Администрация",
                            "Первоначальный доступ из .env",
                            STATUS_APPROVED,
                            now,
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE users
                        SET status = ?, is_admin = 1, updated_at = ?
                        WHERE user_id = ?
                        """,
                        (STATUS_APPROVED, now, uid),
                    )
            conn.commit()

    def get_user(self, user_id: int) -> Optional[UserRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return _row_to_user(row) if row else None

    def is_user_allowed(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        return bool(user and user.status == STATUS_APPROVED)

    def is_user_admin(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        return bool(user and user.status == STATUS_APPROVED and user.is_admin)

    def get_admins(self) -> List[UserRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users WHERE status = ? AND is_admin = 1 ORDER BY created_at ASC",
                (STATUS_APPROVED,),
            ).fetchall()
            return [_row_to_user(r) for r in rows]

    def upsert_application(
        self,
        user_id: int,
        username: Optional[str],
        tg_name: str,
        full_name: str,
        ministry: str,
        reason: str,
    ) -> UserRecord:
        """Сохраняет новую или повторную заявку на доступ."""
        now = _now_str()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT user_id, is_admin FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, username, tg_name, full_name, ministry, reason,
                        status, is_admin, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (user_id, username, tg_name, full_name, ministry, reason, STATUS_PENDING, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET username = ?, tg_name = ?, full_name = ?, ministry = ?, reason = ?,
                        status = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (username, tg_name, full_name, ministry, reason, STATUS_PENDING, now, user_id),
                )
            conn.commit()
        return self.get_user(user_id)  # type: ignore

    def list_users(self, filter_mode: str = "all") -> List[UserRecord]:
        """
        Возвращает список пользователей по фильтру:
        - 'all': все пользователи
        - 'approved': с активным доступом
        - 'pending': ожидающие рассмотрения заявки
        - 'no_access': без доступа (pending, rejected, blocked)
        """
        query = "SELECT * FROM users"
        params: tuple = ()
        if filter_mode == "approved":
            query += " WHERE status = ?"
            params = (STATUS_APPROVED,)
        elif filter_mode == "pending":
            query += " WHERE status = ?"
            params = (STATUS_PENDING,)
        elif filter_mode == "no_access":
            query += " WHERE status != ?"
            params = (STATUS_APPROVED,)
        query += " ORDER BY is_admin DESC, CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 WHEN 'blocked' THEN 2 ELSE 3 END, updated_at DESC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [_row_to_user(r) for r in rows]

    def set_status(self, user_id: int, status: str) -> Optional[UserRecord]:
        if status not in VALID_STATUSES:
            raise ValueError(f"Недопустимый статус: {status}")
        now = _now_str()
        with self._connect() as conn:
            # Если пользователя блокируют или отклоняют, снимаем флаг админа для безопасности
            if status in (STATUS_BLOCKED, STATUS_REJECTED):
                conn.execute(
                    "UPDATE users SET status = ?, is_admin = 0, updated_at = ? WHERE user_id = ?",
                    (status, now, user_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET status = ?, updated_at = ? WHERE user_id = ?",
                    (status, now, user_id),
                )
            conn.commit()
        return self.get_user(user_id)

    def set_admin(self, user_id: int, is_admin: bool) -> Optional[UserRecord]:
        now = _now_str()
        with self._connect() as conn:
            if is_admin:
                # При назначении администратором автоматически даем доступ
                conn.execute(
                    "UPDATE users SET is_admin = 1, status = ?, updated_at = ? WHERE user_id = ?",
                    (STATUS_APPROVED, now, user_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET is_admin = 0, updated_at = ? WHERE user_id = ?",
                    (now, user_id),
                )
            conn.commit()
        return self.get_user(user_id)

    def update_user_info(
        self,
        user_id: int,
        full_name: Optional[str] = None,
        ministry: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Optional[UserRecord]:
        updates = []
        params: list = []
        if full_name is not None:
            updates.append("full_name = ?")
            params.append(full_name.strip())
        if ministry is not None:
            updates.append("ministry = ?")
            params.append(ministry.strip())
        if reason is not None:
            updates.append("reason = ?")
            params.append(reason.strip())

        if not updates:
            return self.get_user(user_id)

        updates.append("updated_at = ?")
        params.append(_now_str())
        params.append(user_id)

        with self._connect() as conn:
            conn.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?",
                tuple(params),
            )
            conn.commit()
        return self.get_user(user_id)

    def get_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, is_admin, COUNT(*) as cnt FROM users GROUP BY status, is_admin"
            ).fetchall()
        stats = {
            "total": 0,
            "approved": 0,
            "pending": 0,
            "blocked": 0,
            "rejected": 0,
            "admins": 0,
        }
        for r in rows:
            cnt = r["cnt"]
            st = r["status"]
            stats["total"] += cnt
            if st in stats:
                stats[st] += cnt
            if r["is_admin"] and st == STATUS_APPROVED:
                stats["admins"] += cnt
        return stats

    def save_split_session(
        self,
        user_id: int,
        session_id: str,
        file_path: str,
        title: str,
        is_hip: bool = False,
        leader: Optional[str] = None,
        awaiting_timestamps: bool = False,
    ) -> None:
        now = _now_str()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO split_sessions (
                    user_id, session_id, file_path, title, is_hip, leader, awaiting_timestamps, ocr_times, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    file_path = excluded.file_path,
                    title = excluded.title,
                    is_hip = excluded.is_hip,
                    leader = excluded.leader,
                    awaiting_timestamps = excluded.awaiting_timestamps,
                    ocr_times = '[]',
                    updated_at = excluded.updated_at
                """,
                (user_id, session_id, file_path, title, int(is_hip), leader, int(awaiting_timestamps), now),
            )
            conn.commit()

    def update_split_ocr_times(self, user_id: int, times: List[int]) -> None:
        now = _now_str()
        with self._connect() as conn:
            conn.execute(
                "UPDATE split_sessions SET ocr_times = ?, updated_at = ? WHERE user_id = ?",
                (json.dumps(times), now, user_id),
            )
            conn.commit()

    def get_split_session_by_id(self, session_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM split_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_awaiting_split_session(self, user_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM split_sessions WHERE user_id = ? AND awaiting_timestamps = 1",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def clear_split_session(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM split_sessions WHERE user_id = ?", (user_id,))
            conn.commit()

    def get_setting(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()

    def delete_setting(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            conn.commit()

    def add_worship_post(
        self,
        channel_id: str,
        message_id: int,
        file_id: str,
        title: str,
        duration: int = 0,
    ) -> None:
        now = _now_str()
        date_key = normalize_date_key(title)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM worship_posts WHERE channel_id = ? AND message_id = ?",
                (str(channel_id), message_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO worship_posts (
                        channel_id, message_id, file_id, title, date_key, duration, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (str(channel_id), message_id, file_id, title, date_key, duration, now),
                )
                conn.commit()

    def get_worship_posts_sorted(self, channel_id: str) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM worship_posts
                WHERE channel_id = ?
                ORDER BY date_key ASC, id ASC
                """,
                (str(channel_id),),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_worship_post_message_id(self, post_id: int, new_message_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE worship_posts SET message_id = ? WHERE id = ?",
                (new_message_id, post_id),
            )
            conn.commit()
