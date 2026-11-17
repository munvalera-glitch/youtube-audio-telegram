"""
Автоматические тесты основных функций и логики модуля downloader.
"""

from unittest.mock import patch, MagicMock
import pytest

from downloader import (
    extract_youtube_url,
    is_valid_youtube_url,
    format_duration,
    format_size,
    sanitize_filename,
    YouTubeAudioDownloader,
    DownloadResult,
    InvalidURLError,
    LivestreamActiveError,
    VideoUnavailableError,
)
from bot import format_report_message
from database import (
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_PENDING,
    STATUS_REJECTED,
    Database,
)
from pathlib import Path
from segmenter import (
    build_part_path,
    extract_timestamp_from_ocr_lines,
    format_clean_audio_titles,
    format_timestamp,
    parse_manual_timestamps,
    parse_time_str,
)


class TestUrlParsing:
    def test_extract_youtube_url_standard(self):
        url = "https://www.youtube.com/watch?v=TdOcWQdNIRs"
        assert extract_youtube_url(url) == url

    def test_extract_youtube_url_live(self):
        url = "https://www.youtube.com/live/TdOcWQdNIRs"
        assert extract_youtube_url(url) == url

    def test_extract_youtube_url_short(self):
        url = "https://youtu.be/TdOcWQdNIRs"
        assert extract_youtube_url(url) == url

    def test_extract_youtube_url_from_text(self):
        text = "Вот запись служения: https://www.youtube.com/live/TdOcWQdNIRs посмотри пожалуйста"
        assert extract_youtube_url(text) == "https://www.youtube.com/live/TdOcWQdNIRs"

    def test_invalid_url(self):
        assert extract_youtube_url("https://vimeo.com/12345678") is None
        assert extract_youtube_url("Просто текст без ссылок") is None
        assert not is_valid_youtube_url("https://vk.com/video123_456")


class TestFormatters:
    def test_format_duration(self):
        assert format_duration(None) == "Не определена"
        assert format_duration(45) == "45 сек"
        assert format_duration(125) == "2 мин 5 сек"
        assert format_duration(3600) == "1 ч 0 мин 0 сек"
        assert format_duration(10772) == "2 ч 59 мин 32 сек"

    def test_format_size(self):
        assert format_size(1024 * 1024) == "1.00 МБ"
        assert format_size(100 * 1024 * 1024) == "100.00 МБ"
        assert format_size(150 * 1024 * 1024 + 512 * 1024) == "150.50 МБ"

    def test_sanitize_filename(self):
        raw = 'Воскресное служение: "Христос воскрес" / 2026? <test>|*'
        clean = sanitize_filename(raw)
        for bad_char in r'\/*?:"<>|':
            assert bad_char not in clean


class TestLivestreamAndAvailabilityCheck:
    def test_invalid_url_raises_exception(self, tmp_path):
        dl = YouTubeAudioDownloader(download_dir=str(tmp_path))
        with pytest.raises(InvalidURLError):
            dl.check_video_info("https://example.com/not-youtube")

    @patch("yt_dlp.YoutubeDL")
    def test_active_livestream_detected(self, mock_ydl_class, tmp_path):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = {
            "title": "Прямой эфир",
            "is_live": True,
            "live_status": "is_live",
            "was_live": False,
        }
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        dl = YouTubeAudioDownloader(download_dir=str(tmp_path))
        with pytest.raises(LivestreamActiveError) as exc_info:
            dl.check_video_info("https://www.youtube.com/live/TdOcWQdNIRs")
        assert "еще идет или запланирован" in str(exc_info.value)

    @patch("yt_dlp.YoutubeDL")
    def test_completed_livestream_allowed(self, mock_ydl_class, tmp_path):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = {
            "title": "Воскресное служение",
            "is_live": False,
            "live_status": "was_live",
            "was_live": True,
            "duration": 7200,
        }
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        dl = YouTubeAudioDownloader(download_dir=str(tmp_path))
        info = dl.check_video_info("https://www.youtube.com/live/TdOcWQdNIRs")
        assert info["title"] == "Воскресное служение"


class TestReportFormatting:
    def test_report_message_formatting(self):
        sample = DownloadResult(
            title="Воскресное служение(2026-11-01)",
            duration_seconds=10772,
            duration_str="2 ч 59 мин 32 сек",
            audio_format="M4A",
            bitrate_kbps=129,
            file_size_mb=166.26,
            file_size_bytes=174338000,
            download_time_seconds=24.5,
            speed_mb_s=6.78,
            file_path="/downloads/test.m4a",
            chapters=[
                {"title": "Прославление", "start_time": 0, "start_time_str": "0 сек"},
                {"title": "Проповедь", "start_time": 3600, "start_time_str": "1 ч 0 мин 0 сек"},
            ],
        )

        msg = format_report_message(sample)
        assert "✅ <b>Аудиодорожка успешно скачана.</b>" in msg
        assert "Воскресное служение(2026-11-01)" in msg
        assert "2 ч 59 мин 32 сек" in msg
        assert "M4A" in msg
        assert "129 кбит/с" in msg
        assert "166.26 МБ" in msg
        assert "24.5 сек" in msg
        assert "6.78 МБ/с" in msg
        assert "/downloads/test.m4a" in msg
        assert "Прославление" in msg
        assert "Проповедь" in msg


class TestSegmenter:
    def test_format_and_parse_time(self):
        assert format_timestamp(274) == "00:04:34"
        assert format_timestamp(5956) == "01:39:16"
        assert parse_time_str("00:04:34") == 274
        assert parse_time_str("01:39:16") == 5956
        assert parse_time_str("32:12") == 1932

    def test_parse_manual_timestamps(self):
        text = "00:04:34 - 00:32:12\n00:42:20 - 01:39:16"
        res = parse_manual_timestamps(text)
        assert res == (274, 1932, 2540, 5956)

        text_short = "Прославление 4:30 - 32:00, Проповедь 41:00 - 1:39:00"
        res2 = parse_manual_timestamps(text_short)
        assert res2 == (270, 1920, 2460, 5940)

        assert parse_manual_timestamps("некорректный текст") is None
        assert parse_manual_timestamps("00:30:00 - 00:10:00\n00:40:00 - 01:00:00") is None

    def test_build_part_path(self):
        orig = Path("/downloads/Воскресное служение(2026-11-01) [TdOcWQdNIRs].m4a")
        w_path = build_part_path(orig, "Прославление")
        s_path = build_part_path(orig, "Проповедь")
        assert w_path.name == "Воскресное служение(2026-11-01) - Прославление [TdOcWQdNIRs].m4a"
        assert s_path.name == "Воскресное служение(2026-11-01) - Проповедь [TdOcWQdNIRs].m4a"

    def test_format_clean_audio_titles(self):
        w_title, s_title = format_clean_audio_titles(
            '＂Церковь - это мы!＂ Валерий Мун( 2026-11-15) [n7NyKfmTkx0]',
            worship_leader="Пак Марина",
            is_hip=False,
        )
        assert w_title == "Прославление_Пак Марина_2026-11-15"
        assert s_title == 'Валерий Мун — "Церковь - это мы!" (2026-11-15)'

        w_hip, s_hip = format_clean_audio_titles(
            '＂Церковь - это мы!＂ Валерий Мун( 2026-11-15) [n7NyKfmTkx0]',
            worship_leader="Тен Александр",
            is_hip=True,
        )
        assert w_hip == "ХиП прославление_Тен Александр_2026-11-15"
        assert s_hip == 'ХиП проповедь — Валерий Мун — "Церковь - это мы!" (2026-11-15)'


class TestDatabase:
    def test_bootstrap_and_persistence(self, tmp_path):
        db_file = str(tmp_path / "test_bot.db")
        db1 = Database(db_file)
        db1.bootstrap_admins({111111})

        assert db1.is_user_allowed(111111)
        assert db1.is_user_admin(111111)
        assert not db1.is_user_allowed(222222)

        # Заявка нового пользователя
        rec = db1.upsert_application(
            user_id=222222,
            username="ivan_petrov",
            tg_name="Ivan",
            full_name="Иван Петров",
            ministry="Молодежное служение",
            reason="Для публикации проповедей",
        )
        assert rec.status == STATUS_PENDING
        assert not db1.is_user_allowed(222222)

        # Одобрение администратором
        db1.set_status(222222, STATUS_APPROVED)
        assert db1.is_user_allowed(222222)
        assert not db1.is_user_admin(222222)

        # Проверка сохранения после "перезапуска" (нового экземпляра Database)
        db2 = Database(db_file)
        assert db2.is_user_allowed(222222)
        loaded = db2.get_user(222222)
        assert loaded is not None
        assert loaded.full_name == "Иван Петров"
        assert loaded.ministry == "Молодежное служение"

        # Редактирование и блокировка / восстановление
        db2.update_user_info(222222, ministry="Медиа служение")
        assert db2.get_user(222222).ministry == "Медиа служение"

        db2.set_admin(222222, True)
        assert db2.is_user_admin(222222)

        db2.set_status(222222, STATUS_BLOCKED)
        assert not db2.is_user_allowed(222222)
        assert not db2.is_user_admin(222222)

        db2.set_status(222222, STATUS_APPROVED)
        assert db2.is_user_allowed(222222)

        # Отклонение заявки
        db2.upsert_application(333333, None, "Test", "Тест Тестов", "Хор", "Слушать песни")
        db2.set_status(333333, STATUS_REJECTED)
        assert not db2.is_user_allowed(333333)
        stats = db2.get_stats()
        assert stats["total"] == 3
        assert stats["approved"] == 2
        assert stats["rejected"] == 1


class TestScreenshotOcr:
    def test_extract_current_over_total(self):
        lines = [
            (0.95, "12:34"),
            (0.45, "Воскресное служение"),
            (0.18, "3:37 / 2:10:05"),
        ]
        assert extract_timestamp_from_ocr_lines(lines) == 217

    def test_extract_hhmmss_over_total(self):
        lines = [
            (0.20, "1:12:08 / 2:10:05"),
        ]
        assert extract_timestamp_from_ocr_lines(lines) == 4328

    def test_extract_fallback_ignores_status_bar_and_ampm(self):
        lines = [
            (0.96, "9:41"),
            (0.80, "4:15 PM"),
            (0.22, "00:42:20"),
            (0.55, "12:30"),
        ]
        assert extract_timestamp_from_ocr_lines(lines) == 2540

    def test_extract_returns_none_when_no_time(self):
        assert extract_timestamp_from_ocr_lines([(0.5, "Привет мир")]) is None

    def test_db_split_ocr_times(self, tmp_path):
        import json

        db_file = str(tmp_path / "test_ocr.db")
        db = Database(db_file)
        db.save_split_session(1001, "sess-1", "/tmp/test.mp3", "Test Service", awaiting_timestamps=True)
        sess = db.get_awaiting_split_session(1001)
        assert sess is not None
        assert json.loads(sess["ocr_times"]) == []

        db.update_split_ocr_times(1001, [274, 1932, 2540, 5956])
        sess2 = db.get_awaiting_split_session(1001)
        assert sess2 is not None
        assert json.loads(sess2["ocr_times"]) == [274, 1932, 2540, 5956]
