import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from downloader import format_duration, sanitize_filename

logger = logging.getLogger(__name__)

FFMPEG_BIN = "/opt/homebrew/bin/ffmpeg"
OCR_TOOL_BIN = str(Path(__file__).resolve().parent / "ocr_tool")

# Лимит Telegram Bot API составляет 50 МБ.
# Берем порог 47 МБ (47 * 1024 * 1024 байт), чтобы гарантированно проходить лимит отправки.
MAX_TELEGRAM_SIZE_BYTES = 47 * 1024 * 1024


@dataclass
class SplitResult:
    """Результат нарезки файла служения на Прославление и Проповедь."""
    worship_path: str
    worship_size_mb: float
    worship_duration_str: str
    worship_duration_sec: int
    sermon_path: str
    sermon_size_mb: float
    sermon_duration_str: str
    sermon_duration_sec: int
    original_deleted: bool
    worship_reencoded_kbps: Optional[int] = None
    sermon_reencoded_kbps: Optional[int] = None


def format_timestamp(seconds: float) -> str:
    """Форматирует секунды в строку HH:MM:SS."""
    sec = max(0, int(round(seconds)))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_time_str(t_str: str) -> Optional[int]:
    """
    Парсит время в формате HH:MM:SS или MM:SS в секунды.
    Примеры: '01:39:16' -> 5956, '32:12' -> 1932, '4:30' -> 270.
    """
    parts = [p.strip() for p in t_str.strip().split(":")]
    try:
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            return h * 3600 + m * 60 + s
        elif len(parts) == 2:
            m, s = int(parts[0]), int(parts[1])
            return m * 60 + s
    except ValueError:
        return None
    return None


def parse_manual_timestamps(text: str) -> Optional[Tuple[int, int, int, int]]:
    """
    Извлекает 4 таймкода (начало/конец прославления и начало/конец проповеди)
    из текста пользователя.
    Поддерживает форматы ЧЧ:ММ:СС и ММ:СС:
      00:04:34 - 00:32:12
      00:42:20 - 01:39:16
    """
    if not text:
        return None

    time_tokens = re.findall(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", text)
    if len(time_tokens) < 4:
        return None

    parsed = [parse_time_str(tok) for tok in time_tokens[:4]]
    if any(p is None for p in parsed):
        return None

    w_start, w_end, s_start, s_end = parsed[0], parsed[1], parsed[2], parsed[3]  # type: ignore
    if w_end <= w_start or s_end <= s_start:
        return None

    return w_start, w_end, s_start, s_end


WORSHIP_LEADERS = [
    "Тен Александр",
    "Пак Марина",
    "Юн Вероника",
    "Сон Дмитрий",
    "Цой Морана",
    "Цой Марина",
]


def format_clean_audio_titles(
    raw_title: str,
    worship_leader: Optional[str] = None,
    is_hip: bool = False,
) -> Tuple[str, str]:
    """
    Формирует чистые названия без технической информации:
    - Обычное служение:
      • Прославление: 'Прославление_Ведущий_дата'
      • Проповедь: 'Имя проповедника — "Название проповеди" (дата)'
    - Служение ХиП (is_hip=True):
      • Прославление: 'ХиП прославление_Ведущий_дата'
      • Проповедь: 'ХиП проповедь — Имя проповедника — "Название проповеди" (дата)'
    """
    clean = raw_title.replace("＂", '"').replace("（", "(").replace("）", ")")
    # Убираем технический ID видео в квадратных скобках и суффиксы
    clean = re.sub(r"\s*\[[A-Za-z0-9_-]{6,15}\]\s*$", "", clean)
    clean = re.sub(r"\s*-\s*(Прославление|Проповедь)\s*$", "", clean, flags=re.IGNORECASE).strip()

    # Ищем дату в скобках или отдельно: (2026-11-15), 2026-11-15, 15.11.2026
    date_str = None
    date_match = re.search(r"\(\s*(\d{4}[-./]\d{2}[-./]\d{2}|\d{2}[-./]\d{2}[-./]\d{4})\s*\)", clean)
    if date_match:
        date_str = date_match.group(1).strip()
        clean = (clean[: date_match.start()] + " " + clean[date_match.end() :]).strip()
    else:
        bare_date = re.search(r"\b(\d{4}[-./]\d{2}[-./]\d{2}|\d{2}[-./]\d{2}[-./]\d{4})\b", clean)
        if bare_date:
            date_str = bare_date.group(1).strip()
            clean = (clean[: bare_date.start()] + " " + clean[bare_date.end() :]).strip()

    # Убираем пометку ХиП из чистого остатка названия, чтобы не дублировать ее
    clean = re.sub(r"\bХиП\b", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s+", " ", clean).strip(" -–—()")

    # 1. Название для Прославления: Прославление_Ведущий_дата (или ХиП прославление_Ведущий_дата)
    w_prefix = "ХиП прославление" if is_hip else "Прославление"
    w_parts = [w_prefix]
    if worship_leader:
        w_parts.append(worship_leader.strip())
    if date_str:
        w_parts.append(date_str)
    worship_title = "_".join(w_parts)

    # 2. Название для Проповеди: Имя проповедника, название проповеди и дата (+ префикс ХиП проповедь при is_hip)
    m_quote_first = re.match(r'^["«„](.+?)["»“]\s*(.+)$', clean)
    m_quote_second = re.match(r'^(.+?)\s*["«„](.+?)["»“]$', clean)

    if m_quote_first:
        topic = m_quote_first.group(1).strip()
        preacher = m_quote_first.group(2).strip(" -–—")
        if preacher:
            sermon_core = f'{preacher} — "{topic}"'
        else:
            sermon_core = f'"{topic}"'
    elif m_quote_second:
        preacher = m_quote_second.group(1).strip(" -–—")
        topic = m_quote_second.group(2).strip()
        if preacher:
            sermon_core = f'{preacher} — "{topic}"'
        else:
            sermon_core = f'"{topic}"'
    else:
        sermon_core = clean if clean else "Проповедь"

    if is_hip:
        if sermon_core and sermon_core.lower() != "проповедь":
            sermon_base = f"ХиП проповедь — {sermon_core}"
        else:
            sermon_base = "ХиП проповедь"
    else:
        sermon_base = sermon_core

    sermon_title = f"{sermon_base} ({date_str})" if date_str else sermon_base
    return worship_title, sermon_title


def build_part_path(original_path: Path, part_label: str) -> Path:
    """
    Формирует безопасное имя для отделенной части служения:
    'Название [ID].m4a' -> 'Название - Прославление [ID].m4a'
    """
    stem = original_path.stem
    suffix = original_path.suffix
    match = re.match(r"^(.*?)(\s*\[[^\]]+\])$", stem)
    if match:
        base_title, vid_tag = match.group(1).strip(), match.group(2)
        new_stem = f"{base_title} - {part_label}{vid_tag}"
    else:
        new_stem = f"{stem} - {part_label}"
    return original_path.parent / f"{new_stem}{suffix}"


def calculate_safe_bitrate_kbps(duration_sec: int, target_mb: float = 43.0) -> int:
    """
    Вычисляет оптимальный битрейт (в кбит/с), чтобы аудиофайл гарантированно
    поместился в лимит отправки Telegram Bot API (< 50 МБ).
    """
    dur = max(1, duration_sec)
    raw_kbps = int((target_mb * 1024 * 8) / dur)
    return max(48, min(112, raw_kbps))


def cut_audio_segment(
    source_path: Path,
    target_path: Path,
    start_sec: int,
    end_sec: int,
) -> Optional[int]:
    """
    Вырезает аудиофрагмент.
    1. Сначала выполняет мгновенную нарезку без перекодирования (-c copy).
    2. Если размер полученного файла превышает лимит Telegram (47 МБ),
       автоматически снижает битрейт через аппаратный кодек Apple AudioToolbox (aac_at),
       чтобы файл гарантированно уложился в 50 МБ.
    Возвращает новый битрейт (int), если потребовалось сжатие, иначе None.
    """
    cmd_copy = [
        FFMPEG_BIN,
        "-y",
        "-v", "error",
        "-ss", str(start_sec),
        "-to", str(end_sec),
        "-i", str(source_path),
        "-c", "copy",
        "-movflags", "+faststart",
        str(target_path),
    ]
    proc = subprocess.run(cmd_copy, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not target_path.exists() or target_path.stat().st_size == 0:
        raise RuntimeError(f"Ошибка при нарезке файла {target_path.name}: {proc.stderr}")

    # Проверяем размер: если укладывается в 47 МБ, оставляем оригинал без перекодирования
    if target_path.stat().st_size <= MAX_TELEGRAM_SIZE_BYTES:
        return None

    duration_sec = max(1, end_sec - start_sec)
    target_kbps = calculate_safe_bitrate_kbps(duration_sec, target_mb=42.5)
    logger.info(
        "Segment %s is %.2f MB (> 47 MB limit). Re-encoding with aac_at at %d kbps...",
        target_path.name,
        target_path.stat().st_size / (1024 * 1024),
        target_kbps,
    )

    temp_target = target_path.with_suffix(".reenc.m4a")
    cmd_reenc = [
        FFMPEG_BIN,
        "-y",
        "-v", "error",
        "-ss", str(start_sec),
        "-to", str(end_sec),
        "-i", str(source_path),
        "-c:a", "aac_at",
        "-b:a", f"{target_kbps}k",
        "-movflags", "+faststart",
        str(temp_target),
    ]
    proc_re = subprocess.run(cmd_reenc, capture_output=True, text=True, timeout=300)
    if proc_re.returncode != 0 or not temp_target.exists() or temp_target.stat().st_size == 0:
        cmd_reenc_sw = [
            FFMPEG_BIN,
            "-y",
            "-v", "error",
            "-ss", str(start_sec),
            "-to", str(end_sec),
            "-i", str(source_path),
            "-c:a", "aac",
            "-b:a", f"{target_kbps}k",
            "-movflags", "+faststart",
            str(temp_target),
        ]
        proc_sw = subprocess.run(cmd_reenc_sw, capture_output=True, text=True, timeout=300)
        if proc_sw.returncode != 0 or not temp_target.exists():
            raise RuntimeError(f"Ошибка при снижении битрейта {target_path.name}: {proc_sw.stderr}")

    temp_target.replace(target_path)
    return target_kbps


def split_service_audio(
    filepath: str,
    worship_start: int,
    worship_end: int,
    sermon_start: int,
    sermon_end: int,
    delete_original: bool = True,
    worship_leader: Optional[str] = None,
    is_hip: bool = False,
    raw_title: Optional[str] = None,
) -> SplitResult:
    """
    Разрезает общий аудиофайл служения на два отдельных файла:
    1. Прославление (например: Прославление_Ведущий_дата.m4a или ХиП прославление_Ведущий_дата.m4a)
    2. Проповедь (например: Имя проповедника — "Название" (дата).m4a или ХиП проповедь — ...)
    При превышении 47 МБ автоматически убавляет битрейт для отправки в Telegram.
    Удаляет исходный полный файл, если delete_original=True.
    """
    orig = Path(filepath).resolve()
    if not orig.is_file():
        raise FileNotFoundError(f"Исходный файл не найден: {orig}")

    w_title, s_title = format_clean_audio_titles(
        raw_title or orig.stem,
        worship_leader=worship_leader,
        is_hip=is_hip,
    )
    worship_path = orig.parent / f"{sanitize_filename(w_title)}{orig.suffix}"
    sermon_path = orig.parent / f"{sanitize_filename(s_title)}{orig.suffix}"

    logger.info(
        "Splitting %s -> Worship (%s..%s), Sermon (%s..%s)",
        orig.name,
        format_timestamp(worship_start),
        format_timestamp(worship_end),
        format_timestamp(sermon_start),
        format_timestamp(sermon_end),
    )

    w_reenc_kbps = cut_audio_segment(orig, worship_path, worship_start, worship_end)
    s_reenc_kbps = cut_audio_segment(orig, sermon_path, sermon_start, sermon_end)

    w_size_mb = round(worship_path.stat().st_size / (1024 * 1024), 2)
    s_size_mb = round(sermon_path.stat().st_size / (1024 * 1024), 2)

    w_dur_sec = max(1, worship_end - worship_start)
    s_dur_sec = max(1, sermon_end - sermon_start)
    w_dur_str = format_duration(w_dur_sec)
    s_dur_str = format_duration(s_dur_sec)

    original_deleted = False
    if delete_original and worship_path.is_file() and sermon_path.is_file():
        try:
            orig.unlink()
            original_deleted = True
            logger.info("Deleted original full file: %s", orig)
        except OSError as e:
            logger.warning("Could not delete original file %s: %s", orig, e)

    return SplitResult(
        worship_path=str(worship_path),
        worship_size_mb=w_size_mb,
        worship_duration_str=w_dur_str,
        worship_duration_sec=w_dur_sec,
        sermon_path=str(sermon_path),
        sermon_size_mb=s_size_mb,
        sermon_duration_str=s_dur_str,
        sermon_duration_sec=s_dur_sec,
        original_deleted=original_deleted,
        worship_reencoded_kbps=w_reenc_kbps,
        sermon_reencoded_kbps=s_reenc_kbps,
    )


def extract_timestamp_from_ocr_lines(lines: list[tuple[float, str]]) -> Optional[int]:
    """
    Извлекает таймкод воспроизведения (в секундах) из строк OCR вида (y, text),
    где y — нормализованная координата Vision (0 — низ кадра, 1 — верх кадра).
    """
    # 1. Приоритет: стандартный вид YouTube-плеера "CURRENT / TOTAL" (например "3:37 / 2:10:05")
    for y, text in lines:
        if y > 0.92:
            continue
        m = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)\s*/\s*(\d{1,2}:\d{2}(?::\d{2})?)", text)
        if m:
            sec = parse_time_str(m.group(1))
            if sec is not None:
                return sec

    # 2. Запасной вариант: одиночный таймкод без "/ TOTAL" (игнорируем верхний статус-бар и часы AM/PM)
    candidates: list[tuple[int, float, int]] = []
    for y, text in lines:
        if y > 0.90:
            continue
        cleaned = re.sub(r"\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\b", " ", text)
        for token in re.findall(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", cleaned):
            sec = parse_time_str(token)
            if sec is not None:
                has_hours = 1 if token.count(":") == 2 else 0
                # Предпочитаем формат HH:MM:SS, а среди равных — нижнюю часть экрана (меньший y)
                candidates.append((has_hours, -y, sec))

    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]

    return None


def extract_timestamp_from_image(image_path: str) -> Optional[int]:
    """Запускает локальный Apple Vision OCR (./ocr_tool) для извлечения таймкода со скриншота."""
    if not Path(OCR_TOOL_BIN).is_file() or not Path(image_path).is_file():
        logger.warning("OCR tool or image missing: tool=%s image=%s", OCR_TOOL_BIN, image_path)
        return None

    try:
        proc = subprocess.run(
            [OCR_TOOL_BIN, image_path],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as e:
        logger.warning("OCR subprocess failed for %s: %s", image_path, e)
        return None

    if proc.returncode != 0:
        logger.warning("ocr_tool exited with %s: %s", proc.returncode, proc.stderr.strip())
        return None

    lines: list[tuple[float, str]] = []
    for raw_line in proc.stdout.splitlines():
        if "|" not in raw_line:
            continue
        y_str, text = raw_line.split("|", 1)
        try:
            y_val = float(y_str.strip())
        except ValueError:
            continue
        lines.append((y_val, text.strip()))

    return extract_timestamp_from_ocr_lines(lines)
