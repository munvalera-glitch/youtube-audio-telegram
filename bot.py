"""
Telegram-бот для автоматического скачивания аудиодорожек с YouTube,
выбора типа служения (обычное / ХиП), выбора ведущего прославления,
нарезки записи на «Прославление» и «Проповедь» по ручным таймкодам,
а также системой регистрации пользователей и административной панелью.
"""

import asyncio
import html
import json
import logging
import os
import platform
import sys
import uuid
from pathlib import Path
from typing import Optional

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageOriginChannel, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from database import (
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_PENDING,
    STATUS_REJECTED,
    Database,
    UserRecord,
)
from downloader import (
    YouTubeAudioDownloader,
    DownloadResult,
    DownloaderError,
    InvalidURLError,
    LivestreamActiveError,
    VideoUnavailableError,
    NetworkDownloadError,
    PermissionOrDiskError,
    extract_youtube_url,
    update_downloader_engine,
)
from segmenter import (
    WORSHIP_LEADERS,
    SplitResult,
    extract_timestamp_from_image,
    format_clean_audio_titles,
    format_timestamp,
    parse_manual_timestamps,
    split_service_audio,
)

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# Инициализация базы данных и загрузка первоначальных администраторов из .env
db = Database(config.DB_PATH)
db.bootstrap_admins(config.ALLOWED_USER_IDS)

# Инициализация загрузчика
downloader = YouTubeAudioDownloader(download_dir=config.DOWNLOAD_DIR_PATH)

# Состояние активных задач скачивания (user_id -> info_str)
active_tasks: dict[int, str] = {}

# Состояние заполнения анкеты регистрации: user_id -> {"step": str, "full_name": str, "ministry": str}
registration_states: dict[int, dict] = {}

# Состояние редактирования пользователя админом: admin_id -> {"target_id": int, "field": str, "filter_mode": str, "page": int}
admin_edit_states: dict[int, dict] = {}

# Блокировки для последовательной обработки альбомов скриншотов OCR по user_id
ocr_locks: dict[int, asyncio.Lock] = {}

# Флаг для чистого самоперезапуска бота после обновления yt-dlp
restart_requested: bool = False

USERS_PER_PAGE = 6

STATUS_LABELS = {
    STATUS_APPROVED: "✅ Доступ разрешён",
    STATUS_PENDING: "⏳ Ожидает одобрения",
    STATUS_REJECTED: "❌ Заявка отклонена",
    STATUS_BLOCKED: "🚫 Заблокирован",
}

STATUS_ICONS = {
    STATUS_APPROVED: "✅",
    STATUS_PENDING: "⏳",
    STATUS_REJECTED: "❌",
    STATUS_BLOCKED: "🚫",
}


# ==============================================================================
# Форматирование сообщений и клавиатур для аудио/нарезки
# ==============================================================================

def format_report_message(res: DownloadResult) -> str:
    """Формирует отчет о скачивании."""
    bitrate_str = f"{res.bitrate_kbps} кбит/с" if res.bitrate_kbps else "Не указан"

    lines = [
        "✅ <b>Аудиодорожка успешно скачана.</b>",
        "",
        f"<b>Название:</b> {html.escape(res.title)}",
        f"<b>Продолжительность:</b> {res.duration_str}",
        f"<b>Формат:</b> {res.audio_format}",
        f"<b>Битрейт:</b> {bitrate_str}",
        f"<b>Размер аудиофайла:</b> {res.file_size_mb} МБ",
        f"<b>Время скачивания:</b> {res.download_time_seconds} сек",
        f"<b>Средняя скорость скачивания:</b> {res.speed_mb_s} МБ/с",
        f"<b>Путь к файлу:</b> <code>{html.escape(res.file_path)}</code>",
    ]

    if res.chapters:
        lines.append("")
        lines.append(f"📌 <b>Временные метки (главы):</b> найдено {len(res.chapters)} шт.")
        for idx, ch in enumerate(res.chapters, 1):
            lines.append(f"  {idx}. [{ch['start_time_str']}] {html.escape(ch['title'])}")
    else:
        lines.append("")
        lines.append("ℹ️ <i>В метаданных видео временные метки (главы) не найдены.</i>")

    return "\n".join(lines)


def build_service_type_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора типа служения: Воскресное или ХиП."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⛪ Воскресное служение", callback_data=f"stype:{session_id}:regular"),
            InlineKeyboardButton("🔥 Служение ХиП", callback_data=f"stype:{session_id}:hip"),
        ]
    ])


def build_leader_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора ведущего прославления (по 2 кнопки в ряд)."""
    rows = []
    for i in range(0, len(WORSHIP_LEADERS), 2):
        row = []
        for idx in range(i, min(i + 2, len(WORSHIP_LEADERS))):
            leader_name = WORSHIP_LEADERS[idx]
            row.append(
                InlineKeyboardButton(
                    f"🎤 {leader_name}",
                    callback_data=f"sleader:{session_id}:{idx}",
                )
            )
        rows.append(row)

    rows.append([
        InlineKeyboardButton("⬅️ Назад к выбору типа служения", callback_data=f"sback:{session_id}"),
    ])
    return InlineKeyboardMarkup(rows)


def format_manual_prompt_message(title: str, worship_leader: str, is_hip: bool) -> str:
    """Формирует сообщение с шаблоном для ручного ввода таймкодов или отправки 4 скриншотов."""
    service_label = "ХиП" if is_hip else "Воскресное служение"
    w_title, s_title = format_clean_audio_titles(title, worship_leader=worship_leader, is_hip=is_hip)
    return (
        f"✏️ <b>Ввод времени для нарезки:</b>\n"
        f"«{html.escape(title)}»\n\n"
        f"• <b>Служение:</b> {service_label}\n"
        f"• <b>Ведущий прославления:</b> {html.escape(worship_leader)}\n"
        f"• <b>Имя файла прославления:</b> <code>{html.escape(w_title)}</code>\n"
        f"• <b>Имя файла проповеди:</b> <code>{html.escape(s_title)}</code>\n\n"
        f"<b>Способ 1 (скриншоты):</b> отправьте <b>4 скриншота</b> плеера YouTube по порядку (по одному или сразу альбомом):\n"
        f"1️⃣ Начало прославления\n"
        f"2️⃣ Конец прославления\n"
        f"3️⃣ Начало проповеди\n"
        f"4️⃣ Конец проповеди\n\n"
        f"<b>Способ 2 (текстом):</b> скопируйте шаблон ниже, исправьте время и отправьте ответным сообщением:\n"
        f"<code>00:00:00 - 00:30:00\n00:40:00 - 01:40:00</code>"
    )


def format_split_result_message(
    title: str,
    w_start: int,
    w_end: int,
    s_start: int,
    s_end: int,
    split_res: SplitResult,
) -> str:
    """Формирует итоговый отчет после разрезания файла."""
    w_range = f"{format_timestamp(w_start)} — {format_timestamp(w_end)}"
    s_range = f"{format_timestamp(s_start)} — {format_timestamp(s_end)}"

    w_br = (
        f" (битрейт снижен до {split_res.worship_reencoded_kbps} кбит/с под лимит Telegram 50 МБ)"
        if split_res.worship_reencoded_kbps
        else " (оригинальный битрейт 129 кбит/с)"
    )
    s_br = (
        f" (битрейт снижен до {split_res.sermon_reencoded_kbps} кбит/с под лимит Telegram 50 МБ)"
        if split_res.sermon_reencoded_kbps
        else " (оригинальный битрейт 129 кбит/с)"
    )

    deleted_note = (
        "🗑 <i>Исходный общий файл удалён.</i>"
        if split_res.original_deleted
        else "ℹ️ <i>Исходный файл сохранён.</i>"
    )

    return (
        f"✅ <b>Служение успешно разделено на 2 файла!</b>\n"
        f"«{html.escape(title)}»\n\n"
        f"🎵 <b>1. Прославление</b> (<code>{w_range}</code>)\n"
        f"• Длительность: {split_res.worship_duration_str}\n"
        f"• Размер: {split_res.worship_size_mb} МБ{w_br}\n"
        f"• Файл: <code>{html.escape(split_res.worship_path)}</code>\n\n"
        f"🎙 <b>2. Проповедь</b> (<code>{s_range}</code>)\n"
        f"• Длительность: {split_res.sermon_duration_str}\n"
        f"• Размер: {split_res.sermon_size_mb} МБ{s_br}\n"
        f"• Файл: <code>{html.escape(split_res.sermon_path)}</code>\n\n"
        f"{deleted_note}"
    )


async def send_split_files_to_chat(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    title: str,
    worship_leader: Optional[str],
    is_hip: bool,
    split_res: SplitResult,
) -> None:
    """
    Отправляет оба нарезанных аудиофайла в Telegram-чат без технической информации
    и удаляет их с диска после отправки.
    """
    worship_title, sermon_title = format_clean_audio_titles(
        title,
        worship_leader=worship_leader,
        is_hip=is_hip,
    )

    w_path = Path(split_res.worship_path)
    s_path = Path(split_res.sermon_path)

    if w_path.is_file():
        with open(w_path, "rb") as wf:
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=wf,
                title=worship_title,
                filename=f"{worship_title}.m4a",
                caption=worship_title,
                duration=split_res.worship_duration_sec,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
            )

        # Автоматическая отправка Прославления в назначенный Telegram-канал
        worship_channel_id = db.get_setting("worship_channel_id")
        if worship_channel_id:
            try:
                target_ch = int(worship_channel_id) if worship_channel_id.lstrip("-").isdigit() else worship_channel_id
                with open(w_path, "rb") as wf_ch:
                    sent_msg = await context.bot.send_audio(
                        chat_id=target_ch,
                        audio=wf_ch,
                        title=worship_title,
                        filename=f"{worship_title}.m4a",
                        caption=worship_title,
                        duration=split_res.worship_duration_sec,
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=60,
                    )
                if sent_msg and sent_msg.audio:
                    db.add_worship_post(
                        channel_id=str(worship_channel_id),
                        message_id=sent_msg.message_id,
                        file_id=sent_msg.audio.file_id,
                        title=worship_title,
                        duration=split_res.worship_duration_sec,
                    )
                logger.info("Sent worship audio '%s' to channel %s", worship_title, worship_channel_id)
            except Exception as e:
                logger.error("Failed to send worship audio to channel %s: %s", worship_channel_id, e)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Не удалось отправить прославление в канал (<code>{html.escape(str(e))}</code>).",
                    parse_mode=ParseMode.HTML,
                )

        try:
            w_path.unlink()
            logger.info("Deleted sent worship file: %s", w_path)
        except OSError as e:
            logger.warning("Failed to delete worship file %s: %s", w_path, e)

    if s_path.is_file():
        with open(s_path, "rb") as sf:
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=sf,
                title=sermon_title,
                filename=f"{sermon_title}.m4a",
                caption=sermon_title,
                duration=split_res.sermon_duration_sec,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
            )

        # Автоматическая отправка Проповеди в назначенный Telegram-канал
        sermon_channel_id = db.get_setting("sermon_channel_id")
        if sermon_channel_id:
            try:
                target_ch = int(sermon_channel_id) if sermon_channel_id.lstrip("-").isdigit() else sermon_channel_id
                with open(s_path, "rb") as sf_ch:
                    sent_msg = await context.bot.send_audio(
                        chat_id=target_ch,
                        audio=sf_ch,
                        title=sermon_title,
                        filename=f"{sermon_title}.m4a",
                        caption=sermon_title,
                        duration=split_res.sermon_duration_sec,
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=60,
                    )
                if sent_msg and sent_msg.audio:
                    db.add_worship_post(
                        channel_id=str(sermon_channel_id),
                        message_id=sent_msg.message_id,
                        file_id=sent_msg.audio.file_id,
                        title=sermon_title,
                        duration=split_res.sermon_duration_sec,
                    )
                logger.info("Sent sermon audio '%s' to channel %s", sermon_title, sermon_channel_id)
            except Exception as e:
                logger.error("Failed to send sermon audio to channel %s: %s", sermon_channel_id, e)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Не удалось отправить проповедь в канал (<code>{html.escape(str(e))}</code>).",
                    parse_mode=ParseMode.HTML,
                )

        try:
            s_path.unlink()
            logger.info("Deleted sent sermon file: %s", s_path)
        except OSError as e:
            logger.warning("Failed to delete sermon file %s: %s", s_path, e)


async def prompt_service_setup(message, user_id: int, file_path: str, title: str) -> None:
    """Отправляет меню выбора типа служения (Обычное / ХиП) и ведущего прославления."""
    session_id = uuid.uuid4().hex[:8]
    is_hip_default = "хип" in title.lower()
    db.save_split_session(
        user_id=user_id,
        session_id=session_id,
        file_path=file_path,
        title=title,
        is_hip=is_hip_default,
        leader=None,
        awaiting_timestamps=False,
    )
    await message.reply_text(
        f"🎛 <b>Настройка перед нарезкой:</b>\n"
        f"«{html.escape(title)}»\n\n"
        f"<b>Шаг 1:</b> Выберите тип служения:",
        parse_mode=ParseMode.HTML,
        reply_markup=build_service_type_keyboard(session_id),
    )


# ==============================================================================
# Регистрация новых пользователей и уведомление администратора
# ==============================================================================

async def send_no_access_message(message, user_id: int) -> None:
    """Отправляет сообщение пользователю без доступа и кнопку «Запросить доступ»."""
    user_rec = db.get_user(user_id)
    if user_rec and user_rec.status == STATUS_PENDING:
        await message.reply_text(
            "⏳ <b>Ваша заявка на доступ уже отправлена и ожидает рассмотрения администратором.</b>\n"
            "Как только решение будет принято, бот пришлет вам уведомление.",
            parse_mode=ParseMode.HTML,
        )
        return

    if user_rec and user_rec.status == STATUS_BLOCKED:
        await message.reply_text(
            "🚫 <b>Доступ к этому боту для вашего аккаунта заблокирован администратором.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Запросить доступ", callback_data="req_access")]
    ])
    await message.reply_text(
        "У вас пока нет доступа к этому боту. Чтобы получить разрешение, отправьте заявку администратору.",
        reply_markup=kb,
    )


async def handle_registration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатий на кнопки регистрации («Запросить доступ» / «Отмена»)."""
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user:
        return
    user_id = user.id

    if query.data == "req_cancel":
        registration_states.pop(user_id, None)
        await query.answer("Заполнение заявки отменено.")
        await query.edit_message_text("❌ Заполнение заявки отменено.")
        return

    if query.data == "req_access":
        if db.is_user_allowed(user_id):
            await query.answer("У вас уже есть доступ!")
            await query.edit_message_text("✅ У вас уже есть доступ к боту. Отправьте /start.")
            return

        existing = db.get_user(user_id)
        if existing and existing.status == STATUS_PENDING:
            await query.answer("Заявка уже на рассмотрении.")
            await query.edit_message_text("⏳ Ваша заявка уже отправлена и ожидает рассмотрения администратором.")
            return
        if existing and existing.status == STATUS_BLOCKED:
            await query.answer("Ваш аккаунт заблокирован.", show_alert=True)
            await query.edit_message_text("🚫 Доступ к боту для вашего аккаунта заблокирован.")
            return

        registration_states[user_id] = {"step": "full_name"}
        await query.answer()
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="req_cancel")]])
        await query.edit_message_text(
            "📝 <b>Заявка на получение доступа (Шаг 1 из 2)</b>\n\n"
            "Введите ваши <b>Имя и Фамилию</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_kb,
        )


async def process_registration_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Обрабатывает очередной шаг анкеты регистрации.
    Возвращает True, если сообщение было обработано как шаг анкеты.
    """
    message = update.message
    user = update.effective_user
    if not message or not message.text or not user:
        return False

    user_id = user.id
    state = registration_states.get(user_id)
    if not state:
        return False

    text = message.text.strip()
    if text.lower() in ("/cancel", "отмена"):
        registration_states.pop(user_id, None)
        await message.reply_text("❌ Заполнение заявки отменено.")
        return True

    if text.startswith("/"):
        await message.reply_text("Пожалуйста, завершите заполнение анкеты или нажмите /cancel для отмены.")
        return True

    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="req_cancel")]])
    step = state["step"]

    if step == "full_name":
        if len(text) < 2:
            await message.reply_text("Пожалуйста, введите корректные Имя и Фамилию:")
            return True
        state["full_name"] = text
        state["step"] = "ministry"
        await message.reply_text(
            "📝 <b>Заявка на получение доступа (Шаг 2 из 2)</b>\n\n"
            "Какое <b>служение</b> вы представляете?",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_kb,
        )
        return True

    if step == "ministry":
        if len(text) < 2:
            await message.reply_text("Пожалуйста, укажите название служения:")
            return True

        full_name = state["full_name"]
        ministry = text
        registration_states.pop(user_id, None)

        record = db.upsert_application(
            user_id=user_id,
            username=user.username,
            tg_name=user.full_name,
            full_name=full_name,
            ministry=ministry,
            reason="",
        )

        await message.reply_text(
            "✅ <b>Заявка успешно отправлена администратору!</b>\n\n"
            f"• <b>Имя и фамилия:</b> {html.escape(record.full_name)}\n"
            f"• <b>Служение:</b> {html.escape(record.ministry)}\n\n"
            "Как только администратор рассмотрит заявку, вы получите уведомление.",
            parse_mode=ParseMode.HTML,
        )

        # Отправляем уведомление всем администраторам
        username_str = f"@{record.username}" if record.username else "отсутствует"
        admin_text = (
            "📩 <b>Новая заявка на получение доступа к боту!</b>\n\n"
            f"• <b>Имя и фамилия:</b> {html.escape(record.full_name)}\n"
            f"• <b>Telegram ID:</b> <code>{record.user_id}</code>\n"
            f"• <b>Username:</b> {html.escape(username_str)}\n"
            f"• <b>Служение:</b> {html.escape(record.ministry)}"
        )
        admin_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Разрешить доступ", callback_data=f"adm:approve:{record.user_id}:notif:0"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"adm:reject:{record.user_id}:notif:0"),
            ]
        ])

        for adm in db.get_admins():
            try:
                await context.bot.send_message(
                    chat_id=adm.user_id,
                    text=admin_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=admin_kb,
                )
            except Exception as e:
                logger.warning("Could not notify admin %s: %s", adm.user_id, e)

        return True

    return False


# ==============================================================================
# Административная панель (/admin и callbacks adm:*)
# ==============================================================================

def _format_channel_label(id_key: str, title_key: str) -> str:
    ch_id = db.get_setting(id_key)
    ch_title = db.get_setting(title_key) or ""
    if ch_id:
        return f"✅ {html.escape(ch_title)} (<code>{html.escape(ch_id)}</code>)" if ch_title else f"✅ <code>{html.escape(ch_id)}</code>"
    return "не назначен"


def build_admin_home_view() -> tuple[str, InlineKeyboardMarkup]:
    """Формирует текст и клавиатуру главной страницы админ-панели."""
    stats = db.get_stats()
    w_chan = db.get_setting("worship_channel_id")
    s_chan = db.get_setting("sermon_channel_id")

    text = (
        "🛠 <b>Административная панель</b>\n\n"
        f"• <b>Всего пользователей в базе:</b> {stats['total']}\n"
        f"• <b>✅ С доступом:</b> {stats['approved']} (админов: {stats['admins']})\n"
        f"• <b>⏳ Новые заявки:</b> {stats['pending']}\n"
        f"• <b>🚫 Заблокированы / ❌ Отклонены:</b> {stats['blocked'] + stats['rejected']}\n"
        f"• <b>🎵 Канал прославления:</b> {_format_channel_label('worship_channel_id', 'worship_channel_title')}\n"
        f"• <b>🎙 Канал проповедей:</b> {_format_channel_label('sermon_channel_id', 'sermon_channel_title')}\n\n"
        "Выберите раздел:"
    )
    rows = [
        [
            InlineKeyboardButton(
                f"⏳ Новые заявки ({stats['pending']})",
                callback_data="adm:list:pending:0",
            ),
            InlineKeyboardButton(
                f"👥 Все пользователи ({stats['total']})",
                callback_data="adm:list:all:0",
            ),
        ],
        [
            InlineKeyboardButton(
                f"✅ С доступом ({stats['approved']})",
                callback_data="adm:list:approved:0",
            ),
            InlineKeyboardButton(
                f"🚫 Без доступа ({stats['pending'] + stats['blocked'] + stats['rejected']})",
                callback_data="adm:list:no_access:0",
            ),
        ],
    ]
    if w_chan:
        rows.append([
            InlineKeyboardButton("🔄 Сорт. прославление по датам", callback_data="adm:sort_wchan"),
            InlineKeyboardButton("🔕 Откл. прославление", callback_data="adm:clear_wchan"),
        ])
    if s_chan:
        rows.append([
            InlineKeyboardButton("🔄 Сорт. проповеди по датам", callback_data="adm:sort_schan"),
            InlineKeyboardButton("🔕 Откл. проповеди", callback_data="adm:clear_schan"),
        ])
    return text, InlineKeyboardMarkup(rows)


def build_user_list_view(filter_mode: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Формирует список пользователей с фильтрацией и пагинацией."""
    users = db.list_users(filter_mode)
    titles = {
        "all": "👥 Все пользователи",
        "approved": "✅ Пользователи с доступом",
        "pending": "⏳ Новые заявки на доступ",
        "no_access": "🚫 Пользователи без доступа",
    }
    header = titles.get(filter_mode, "👥 Пользователи")

    total = len(users)
    total_pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = users[page * USERS_PER_PAGE : (page + 1) * USERS_PER_PAGE]

    if not chunk:
        text = f"<b>{header}</b>\n\n<i>В этом разделе пока нет пользователей.</i>"
    else:
        text = (
            f"<b>{header}</b> (всего: {total}, стр. {page + 1}/{total_pages})\n\n"
            "Нажмите на пользователя для просмотра и управления:"
        )

    rows = []
    for u in chunk:
        icon = STATUS_ICONS.get(u.status, "❓")
        crown = "👑 " if u.is_admin else ""
        display_name = u.full_name or u.tg_name or str(u.user_id)
        ministry_part = f" • {u.ministry}" if u.ministry else ""
        btn_label = f"{icon} {crown}{display_name}{ministry_part}"
        if len(btn_label) > 55:
            btn_label = btn_label[:52] + "..."
        rows.append([
            InlineKeyboardButton(
                btn_label,
                callback_data=f"adm:user:{u.user_id}:{filter_mode}:{page}",
            )
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"adm:list:{filter_mode}:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"adm:list:{filter_mode}:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("🏠 Главное меню админки", callback_data="adm:home")])
    return text, InlineKeyboardMarkup(rows)


def build_user_card_view(
    u: UserRecord,
    current_admin_id: int,
    filter_mode: str = "all",
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """Формирует карточку конкретного пользователя с кнопками управления."""
    status_str = STATUS_LABELS.get(u.status, u.status)
    role_str = "👑 Администратор" if u.is_admin else "👤 Обычный пользователь"
    username_str = f"@{u.username}" if u.username else "отсутствует"

    text = (
        f"👤 <b>Карточка пользователя</b>\n\n"
        f"• <b>Имя и фамилия:</b> {html.escape(u.full_name or 'Не указано')}\n"
        f"• <b>Telegram ID:</b> <code>{u.user_id}</code>\n"
        f"• <b>Username:</b> {html.escape(username_str)}\n"
        f"• <b>Служение:</b> {html.escape(u.ministry or 'Не указано')}\n"
        f"• <b>Причина запроса:</b> {html.escape(u.reason or 'Не указана')}\n\n"
        f"• <b>Статус доступа:</b> {status_str}\n"
        f"• <b>Роль:</b> {role_str}\n"
        f"• <b>Создан:</b> <code>{u.created_at}</code>\n"
        f"• <b>Обновлён:</b> <code>{u.updated_at}</code>"
    )

    rows = []

    # Управление доступом
    if u.status == STATUS_PENDING:
        rows.append([
            InlineKeyboardButton("✅ Разрешить доступ", callback_data=f"adm:approve:{u.user_id}:{filter_mode}:{page}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"adm:reject:{u.user_id}:{filter_mode}:{page}"),
        ])
    elif u.status == STATUS_APPROVED:
        if u.user_id != current_admin_id:
            rows.append([
                InlineKeyboardButton("🚫 Заблокировать доступ", callback_data=f"adm:block:{u.user_id}:{filter_mode}:{page}")
            ])
    else:  # blocked или rejected
        rows.append([
            InlineKeyboardButton("✅ Восстановить доступ", callback_data=f"adm:approve:{u.user_id}:{filter_mode}:{page}")
        ])

    # Управление правами администратора (нельзя снять права с самого себя)
    if u.user_id != current_admin_id:
        if u.is_admin:
            rows.append([
                InlineKeyboardButton("👤 Снять права админа", callback_data=f"adm:demote:{u.user_id}:{filter_mode}:{page}")
            ])
        else:
            rows.append([
                InlineKeyboardButton("👑 Назначить администратором", callback_data=f"adm:promote:{u.user_id}:{filter_mode}:{page}")
            ])

    # Редактирование анкетных данных
    rows.append([
        InlineKeyboardButton("✏️ Имя", callback_data=f"adm:edit:{u.user_id}:full_name:{filter_mode}:{page}"),
        InlineKeyboardButton("✏️ Служение", callback_data=f"adm:edit:{u.user_id}:ministry:{filter_mode}:{page}"),
        InlineKeyboardButton("✏️ Причину", callback_data=f"adm:edit:{u.user_id}:reason:{filter_mode}:{page}"),
    ])

    if filter_mode == "notif":
        rows.append([InlineKeyboardButton("🛠 Открыть админ-панель", callback_data="adm:home")])
    else:
        rows.append([
            InlineKeyboardButton("⬅️ К списку", callback_data=f"adm:list:{filter_mode}:{page}"),
            InlineKeyboardButton("🏠 В админку", callback_data="adm:home"),
        ])

    return text, InlineKeyboardMarkup(rows)


async def execute_sort_channel(bot, ch_id: Optional[str] = None, label: str = "Канал") -> str:
    """
    Удаляет опубликованные посты в канале (в пределах 48 ч)
    и мгновенно переотправляет их по сохранённым file_id строго по возрастанию дат.
    """
    if not ch_id:
        ch_id = db.get_setting("worship_channel_id")
    if not ch_id:
        return f"⚠️ {label} ещё не назначен."

    posts = db.get_worship_posts_sorted(ch_id)
    if not posts:
        return (
            f"ℹ️ <b>В базе пока нет сохранённых публикаций для канала «{html.escape(label)}».</b>\n"
            "Нарежьте и отправьте несколько записей, и они автоматически сохранятся для сортировки."
        )

    target_ch = int(ch_id) if str(ch_id).lstrip("-").isdigit() else ch_id
    failed_deletes = 0

    for p in posts:
        try:
            await bot.delete_message(chat_id=target_ch, message_id=p["message_id"])
        except Exception as e:
            failed_deletes += 1
            logger.warning("Could not delete old channel message %s: %s", p["message_id"], e)

    reposted = []
    for p in posts:
        sent = await bot.send_audio(
            chat_id=target_ch,
            audio=p["file_id"],
            title=p["title"],
            caption=p["title"],
            duration=p["duration"] or None,
            read_timeout=120,
            write_timeout=120,
        )
        db.update_worship_post_message_id(p["id"], sent.message_id)
        reposted.append(p["title"])
        await asyncio.sleep(0.4)

    lines = [
        f"✅ <b>{html.escape(label)} отсортирован по возрастанию дат!</b>",
        f"Опубликовано записей: <b>{len(reposted)}</b>",
        "",
    ]
    for idx, t in enumerate(reposted, 1):
        lines.append(f"{idx}. {html.escape(t)}")

    if failed_deletes > 0:
        lines.append("")
        lines.append(
            f"⚠️ <i>Не удалось автоматически удалить {failed_deletes} старых постов "
            f"(Telegram запрещает ботам удалять посты старше 48 часов — удалите их в канале вручную).</i>"
        )
    return "\n".join(lines)


async def sort_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /sort_channel — сортирует посты в подключенных каналах по возрастанию дат."""
    user = update.effective_user
    if not user or not update.message:
        return

    if not db.is_user_allowed(user.id):
        await send_no_access_message(update.message, user.id)
        return

    if not db.is_user_admin(user.id):
        await update.message.reply_text("⛔ Эта команда доступна только администраторам.")
        return

    msg = await update.message.reply_text(
        "🔄 <b>Сортирую посты в каналах по возрастанию дат...</b>",
        parse_mode=ParseMode.HTML,
    )

    reports = []
    w_chan = db.get_setting("worship_channel_id")
    s_chan = db.get_setting("sermon_channel_id")
    if w_chan and db.get_worship_posts_sorted(w_chan):
        reports.append(await execute_sort_channel(context.bot, w_chan, "Канал прославления"))
    if s_chan and db.get_worship_posts_sorted(s_chan):
        reports.append(await execute_sort_channel(context.bot, s_chan, "Канал проповедей"))

    if not reports:
        final_text = (
            "ℹ️ <b>В базе пока нет сохранённых публикаций для сортировки.</b>\n"
            "Нарежьте и отправьте записи (или перешлите их в канал), и они автоматически сохранятся."
        )
    else:
        final_text = "\n\n".join(reports)

    await msg.edit_text(final_text, parse_mode=ParseMode.HTML)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /admin — открывает административную панель (только для администраторов)."""
    user = update.effective_user
    if not user or not update.message:
        return

    if not db.is_user_allowed(user.id):
        await send_no_access_message(update.message, user.id)
        return

    if not db.is_user_admin(user.id):
        await update.message.reply_text("⛔ Эта команда доступна только администраторам.")
        return

    text, kb = build_admin_home_view()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик всех кнопок административной панели (adm:*)."""
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not db.is_user_admin(user.id):
        await query.answer("⛔ Доступ только для администраторов.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else "home"

    if action == "home":
        admin_edit_states.pop(user.id, None)
        text, kb = build_admin_home_view()
        await query.answer()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action in ("set_wchan", "set_schan"):
        ch_id = parts[2] if len(parts) > 2 else ""
        if not ch_id:
            await query.answer("Некорректный ID канала.", show_alert=True)
            return
        ch_title = ""
        try:
            chat_obj = await context.bot.get_chat(int(ch_id))
            ch_title = chat_obj.title or ""
        except Exception:
            pass

        is_worship = (action == "set_wchan")
        id_key = "worship_channel_id" if is_worship else "sermon_channel_id"
        title_key = "worship_channel_title" if is_worship else "sermon_channel_title"
        kind_ru = "Прославления" if is_worship else "Проповедей"

        db.set_setting(id_key, ch_id)
        if ch_title:
            db.set_setting(title_key, ch_title)
        await query.answer(f"✅ Канал {kind_ru} назначен!")
        label = f"«{html.escape(ch_title)}» (<code>{html.escape(ch_id)}</code>)" if ch_title else f"<code>{html.escape(ch_id)}</code>"
        await query.edit_message_text(
            f"✅ <b>Канал для автоматической отправки {kind_ru} настроен!</b>\n\n"
            f"• Канал: {label}",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "clear_wchan":
        db.delete_setting("worship_channel_id")
        db.delete_setting("worship_channel_title")
        text, kb = build_admin_home_view()
        await query.answer("🔕 Автоотправка прославления отключена.")
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == "clear_schan":
        db.delete_setting("sermon_channel_id")
        db.delete_setting("sermon_channel_title")
        text, kb = build_admin_home_view()
        await query.answer("🔕 Автоотправка проповедей отключена.")
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == "sort_wchan":
        await query.answer("Сортирую канал прославления...")
        await query.edit_message_text("🔄 <b>Сортирую посты в канале прославления по возрастанию дат...</b>", parse_mode=ParseMode.HTML)
        report = await execute_sort_channel(context.bot, db.get_setting("worship_channel_id"), "Канал прославления")
        await query.edit_message_text(report, parse_mode=ParseMode.HTML)
        return

    if action == "sort_schan":
        await query.answer("Сортирую канал проповедей...")
        await query.edit_message_text("🔄 <b>Сортирую посты в канале проповедей по возрастанию дат...</b>", parse_mode=ParseMode.HTML)
        report = await execute_sort_channel(context.bot, db.get_setting("sermon_channel_id"), "Канал проповедей")
        await query.edit_message_text(report, parse_mode=ParseMode.HTML)
        return

    if action == "edit_cancel":
        state = admin_edit_states.pop(user.id, None)
        await query.answer("Редактирование отменено.")
        if state:
            target = db.get_user(state["target_id"])
            if target:
                text, kb = build_user_card_view(target, user.id, state["filter_mode"], state["page"])
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
                return
        text, kb = build_admin_home_view()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == "list":
        filter_mode = parts[2] if len(parts) > 2 else "all"
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        text, kb = build_user_list_view(filter_mode, page)
        await query.answer()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == "user":
        target_id = int(parts[2])
        filter_mode = parts[3] if len(parts) > 3 else "all"
        page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        target = db.get_user(target_id)
        if not target:
            await query.answer("Пользователь не найден.", show_alert=True)
            return
        text, kb = build_user_card_view(target, user.id, filter_mode, page)
        await query.answer()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == "edit":
        target_id = int(parts[2])
        field = parts[3]
        filter_mode = parts[4] if len(parts) > 4 else "all"
        page = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
        target = db.get_user(target_id)
        if not target:
            await query.answer("Пользователь не найден.", show_alert=True)
            return

        field_names = {
            "full_name": "Имя и фамилию",
            "ministry": "Служение",
            "reason": "Причину / заметку",
        }
        admin_edit_states[user.id] = {
            "target_id": target_id,
            "field": field,
            "filter_mode": filter_mode,
            "page": page,
        }
        await query.answer()
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="adm:edit_cancel")]])
        await query.edit_message_text(
            f"✏️ <b>Редактирование пользователя:</b> {html.escape(target.full_name or str(target_id))}\n\n"
            f"Отправьте в чат новое значение для поля <b>«{field_names.get(field, field)}»</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_kb,
        )
        return

    if action in ("approve", "reject", "block", "promote", "demote"):
        target_id = int(parts[2])
        filter_mode = parts[3] if len(parts) > 3 else "all"
        page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        target = db.get_user(target_id)
        if not target:
            await query.answer("Пользователь не найден.", show_alert=True)
            return

        notify_user_msg = None

        if action == "approve":
            was_pending = target.status == STATUS_PENDING
            target = db.set_status(target_id, STATUS_APPROVED)
            await query.answer("✅ Доступ разрешён!")
            if was_pending:
                notify_user_msg = (
                    "🎉 <b>Ваша заявка одобрена!</b>\n\n"
                    "Администратор предоставил вам доступ к боту. "
                    "Отправьте /start, чтобы начать работу."
                )
            else:
                notify_user_msg = (
                    "✅ <b>Ваш доступ к боту восстановлен администратором.</b>\n"
                    "Отправьте /start для продолжения работы."
                )

        elif action == "reject":
            target = db.set_status(target_id, STATUS_REJECTED)
            await query.answer("❌ Заявка отклонена.")
            notify_user_msg = "❌ <b>Ваша заявка на доступ к боту была отклонена администратором.</b>"

        elif action == "block":
            if target_id == user.id:
                await query.answer("Нельзя заблокировать самого себя.", show_alert=True)
                return
            target = db.set_status(target_id, STATUS_BLOCKED)
            await query.answer("🚫 Пользователь заблокирован.")
            notify_user_msg = "🚫 <b>Ваш доступ к боту был заблокирован администратором.</b>"

        elif action == "promote":
            target = db.set_admin(target_id, True)
            await query.answer("👑 Пользователь назначен администратором!")
            notify_user_msg = (
                "👑 <b>Вам предоставлены права администратора бота!</b>\n"
                "Используйте команду /admin для управления пользователями и заявками."
            )

        elif action == "demote":
            if target_id == user.id:
                await query.answer("Нельзя снять права администратора с самого себя.", show_alert=True)
                return
            if target_id in config.ALLOWED_USER_IDS:
                await query.answer("Нельзя снять права с главного администратора из .env.", show_alert=True)
                return
            target = db.set_admin(target_id, False)
            await query.answer("👤 Права администратора сняты.")
            notify_user_msg = "ℹ️ <b>Права администратора для вашего аккаунта были сняты.</b>"

        # Уведомляем пользователя об изменении статуса
        if notify_user_msg:
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=notify_user_msg,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning("Could not notify user %s: %s", target_id, e)

        if target:
            text, kb = build_user_card_view(target, user.id, filter_mode, page)
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return


# ==============================================================================
# Callbacks настройки нарезки служения (stype / sleader / sback)
# ==============================================================================

async def handle_setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает выбор типа служения и выбор ведущего прославления."""
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not db.is_user_allowed(user.id):
        await query.answer("У вас нет доступа к боту.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[0]
    session_id = parts[1] if len(parts) > 1 else ""
    session = db.get_split_session_by_id(session_id)

    if not session:
        await query.answer("Сессия устарела. Отправьте /split для повторного выбора.", show_alert=True)
        return

    title = session["title"]

    if action == "sback":
        db.save_split_session(
            user_id=user.id,
            session_id=session_id,
            file_path=session["file_path"],
            title=title,
            is_hip=bool(session["is_hip"]),
            leader=None,
            awaiting_timestamps=False,
        )
        await query.answer()
        await query.edit_message_text(
            f"🎛 <b>Настройка перед нарезкой:</b>\n"
            f"«{html.escape(title)}»\n\n"
            f"<b>Шаг 1:</b> Выберите тип служения:",
            parse_mode=ParseMode.HTML,
            reply_markup=build_service_type_keyboard(session_id),
        )
        return

    if action == "stype":
        stype_val = parts[2] if len(parts) > 2 else "regular"
        is_hip = (stype_val == "hip")
        db.save_split_session(
            user_id=user.id,
            session_id=session_id,
            file_path=session["file_path"],
            title=title,
            is_hip=is_hip,
            leader=session["leader"],
            awaiting_timestamps=False,
        )
        service_label = "🔥 Служение ХиП" if is_hip else "⛪ Воскресное служение"
        await query.answer(service_label)
        await query.edit_message_text(
            f"🎛 <b>Настройка перед нарезкой:</b>\n"
            f"«{html.escape(title)}»\n"
            f"• Тип: <b>{service_label}</b>\n\n"
            f"<b>Шаг 2:</b> Выберите ведущего прославления:",
            parse_mode=ParseMode.HTML,
            reply_markup=build_leader_keyboard(session_id),
        )
        return

    if action == "sleader":
        leader_idx = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        leader_name = WORSHIP_LEADERS[leader_idx % len(WORSHIP_LEADERS)]
        is_hip = bool(session["is_hip"])
        db.save_split_session(
            user_id=user.id,
            session_id=session_id,
            file_path=session["file_path"],
            title=title,
            is_hip=is_hip,
            leader=leader_name,
            awaiting_timestamps=True,
        )
        await query.answer(f"Ведущий: {leader_name}")

        prompt_text = format_manual_prompt_message(
            title=title,
            worship_leader=leader_name,
            is_hip=is_hip,
        )
        await query.edit_message_text(
            prompt_text,
            parse_mode=ParseMode.HTML,
        )
        return


# ==============================================================================
# Рабочие команды бота (доступны только одобренным пользователям)
# ==============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start — приветствие или экран запроса доступа."""
    user = update.effective_user
    if not user or not update.message:
        return
    user_id = user.id

    if not db.is_user_allowed(user_id):
        await send_no_access_message(update.message, user_id)
        return

    admin_line = "\n• /admin — административная панель (пользователи и заявки)" if db.is_user_admin(user_id) else ""

    text = (
        f"👋 Приветствую, <b>{html.escape(user.first_name)}</b>!\n\n"
        "Я бот для скачивания аудиодорожек служений с YouTube и нарезки на "
        "<b>Прославление</b> и <b>Проповедь</b> с отправкой готовых аудиофайлов прямо в Telegram.\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Отправьте ссылку на завершенную трансляцию с YouTube.\n"
        "2. В меню выберите тип служения (<b>Воскресное служение</b> или <b>ХиП</b>) и <b>ведущего прославления</b>.\n"
        "3. Отправьте таймкоды начала/конца Прославления и Проповеди — я разрежу запись, подгоню размер под лимит 50 МБ и пришлю оба файла в чат!\n\n"
        "<b>Доступные команды:</b>\n"
        "• /split — разделить уже скачанное служение из папки downloads\n"
        "• /status — текущее состояние скачивания\n"
        "• /update_ytdlp — обновить движок yt-dlp прямо из бота\n"
        "• /help — подробная справка\n"
        "• /whoami — информация о вашем профиле"
        f"{admin_line}"
    )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /whoami — показывает профиль пользователя."""
    user = update.effective_user
    if not user or not update.message:
        return

    if not db.is_user_allowed(user.id):
        await send_no_access_message(update.message, user.id)
        return

    rec = db.get_user(user.id)
    role_str = "👑 Администратор" if (rec and rec.is_admin) else "👤 Пользователь"
    reply = (
        f"👤 <b>Ваш Telegram профиль:</b>\n\n"
        f"• <b>User ID:</b> <code>{user.id}</code>\n"
        f"• <b>Имя:</b> {html.escape(rec.full_name if rec and rec.full_name else user.full_name)}\n"
        f"• <b>Служение:</b> {html.escape(rec.ministry if rec and rec.ministry else 'Не указано')}\n"
        f"• <b>Username:</b> @{user.username if user.username else 'отсутствует'}\n"
        f"• <b>Роль:</b> {role_str}\n"
        f"• <b>Статус доступа:</b> ✅ Разрешен"
    )
    await update.message.reply_text(reply, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /help — список команд и справка."""
    user = update.effective_user
    if not user or not update.message:
        return

    if not db.is_user_allowed(user.id):
        await send_no_access_message(update.message, user.id)
        return

    admin_line = "\n  /admin — Панель администратора (заявки и управление доступом)" if db.is_user_admin(user.id) else ""
    text = (
        "📖 <b>Справка по использованию бота:</b>\n\n"
        "• <b>Отправка ссылки:</b> просто пришлите URL вида:\n"
        "  - <code>https://www.youtube.com/watch?v=...</code>\n"
        "  - <code>https://www.youtube.com/live/...</code>\n"
        "  - <code>https://youtu.be/...</code>\n\n"
        "• <b>Порядок нарезки:</b>\n"
        "  1. Выберите кнопкой тип служения (<b>Воскресное служение</b> или <b>ХиП</b>).\n"
        "  2. Выберите кнопкой <b>ведущего прославления</b>.\n"
        "  3. Скопируйте шаблон, укажите время Прославления (1-я строка) и Проповеди (2-я строка) и отправьте в чат:\n"
        "  <code>00:04:34 - 00:32:12\n00:42:20 - 01:39:16</code>\n\n"
        "• <b>Команды:</b>\n"
        "  /start — Краткая инструкция\n"
        "  /split — Разделить уже скачанный файл в папке downloads\n"
        "  /status — Проверить, идет ли скачивание сейчас\n"
        "  /update_ytdlp — Обновить движок yt-dlp до последней версии\n"
        "  /whoami — Профиль и права доступа\n"
        "  /help — Это справочное сообщение"
        f"{admin_line}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def update_ytdlp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /update_ytdlp — обновляет yt-dlp до актуальной версии и перезапускает бота."""
    global restart_requested
    user = update.effective_user
    if not user or not update.message:
        return

    if not db.is_user_allowed(user.id):
        await send_no_access_message(update.message, user.id)
        return

    old_ver = getattr(yt_dlp.version, "__version__", "неизвестна")
    py_ver = platform.python_version()
    msg = await update.message.reply_text(
        f"🔄 <b>Проверяю обновления движка yt-dlp...</b>\n"
        f"• Текущая версия yt-dlp: <code>{old_ver}</code>\n"
        f"• Версия Python: <code>{py_ver}</code>",
        parse_mode=ParseMode.HTML,
    )

    loop = asyncio.get_running_loop()
    success, version_or_err = await loop.run_in_executor(None, update_downloader_engine)

    if success:
        if version_or_err != old_ver:
            await msg.edit_text(
                f"✅ <b>Движок yt-dlp обновлён!</b>\n\n"
                f"• Была версия: <code>{old_ver}</code>\n"
                f"• Новая версия: <code>{version_or_err}</code>\n"
                f"• Python: <code>{py_ver}</code>\n\n"
                f"🔄 <i>Перезапускаю бота для применения обновления (через 3 секунды можно отправлять ссылки)...</i>",
                parse_mode=ParseMode.HTML,
            )
            restart_requested = True
            context.application.stop_running()
        else:
            await msg.edit_text(
                f"✅ <b>У вас уже установлена последняя совместимая версия движка yt-dlp!</b>\n\n"
                f"• Текущая версия: <code>{version_or_err}</code>\n"
                f"• Python: <code>{py_ver}</code>\n"
                f"• Перезапуск не требуется.",
                parse_mode=ParseMode.HTML,
            )
    else:
        hint = ""
        if "Requires-Python" in version_or_err or "python" in version_or_err.lower():
            hint = "\n\n💡 <i>Похоже, новая версия yt-dlp требует более свежий Python. Потребуется обновить окружение .venv на компьютере.</i>"
        await msg.edit_text(
            f"⚠️ <b>Ошибка при обновлении движка:</b>\n<code>{html.escape(version_or_err[:300])}</code>{hint}",
            parse_mode=ParseMode.HTML,
        )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /status — состояние загрузки."""
    user = update.effective_user
    if not user or not update.message:
        return

    if not db.is_user_allowed(user.id):
        await send_no_access_message(update.message, user.id)
        return

    if not active_tasks:
        text = "🟢 <b>Бот свободен.</b> В данный момент активных загрузок нет."
    else:
        text = "⏳ <b>Текущие активные задачи:</b>\n"
        for uid, desc in active_tasks.items():
            text += f"• Пользователь <code>{uid}</code>: {html.escape(desc)}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда /split — выбирает последний неразделенный файл служения в папке downloads
    и открывает меню выбора типа служения и ведущего прославления.
    """
    user = update.effective_user
    if not user or not update.message:
        return

    if not db.is_user_allowed(user.id):
        await send_no_access_message(update.message, user.id)
        return

    download_dir = Path(config.DOWNLOAD_DIR_PATH)
    candidates = [
        f for f in sorted(download_dir.glob("*.m4a"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not f.name.startswith("Прославление_")
        and not f.name.startswith("ХиП прославление_")
        and " - Прославление" not in f.name
        and " - Проповедь" not in f.name
        and f.stat().st_size > 10 * 1024 * 1024
    ]

    if not candidates:
        await update.message.reply_text(
            "📂 В папке <code>downloads</code> сейчас нет неразделенных записей служений (размером > 10 МБ).",
            parse_mode=ParseMode.HTML,
        )
        return

    fpath = candidates[0]
    await prompt_service_setup(update.message, user.id, str(fpath), fpath.stem)


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отслеживает новые аудиопосты в каналах Прославления и Проповедей
    (в том числе добавленные вручную/пересланные) и позволяет запускать /sort_channel.
    """
    post = update.channel_post
    if not post:
        return

    w_chan = db.get_setting("worship_channel_id")
    s_chan = db.get_setting("sermon_channel_id")
    chat_id_str = str(post.chat.id)

    if chat_id_str not in {str(w_chan), str(s_chan)}:
        return

    is_worship = (chat_id_str == str(w_chan))
    chan_label = "Канал прославления" if is_worship else "Канал проповедей"

    if post.audio:
        default_title = "Прославление" if is_worship else "Проповедь"
        title = (post.caption or post.audio.title or post.audio.file_name or default_title).strip()
        db.add_worship_post(
            channel_id=chat_id_str,
            message_id=post.message_id,
            file_id=post.audio.file_id,
            title=title,
            duration=post.audio.duration or 0,
        )
        logger.info("Tracked %s audio post '%s' (msg %s)", chan_label, title, post.message_id)
        return

    if post.text and post.text.strip().startswith("/sort_channel"):
        try:
            await context.bot.delete_message(chat_id=post.chat.id, message_id=post.message_id)
        except Exception:
            pass
        report = await execute_sort_channel(context.bot, chat_id_str, chan_label)
        for adm in db.get_admins():
            try:
                await context.bot.send_message(chat_id=adm.user_id, text=report, parse_mode=ParseMode.HTML)
            except Exception:
                pass


async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отслеживает добавление бота администратором в канал и предлагает назначить его каналом Прославления."""
    my_cm = update.my_chat_member
    if not my_cm:
        return

    chat = my_cm.chat
    new_status = my_cm.new_chat_member.status
    if chat.type == ChatType.CHANNEL and new_status == ChatMemberStatus.ADMINISTRATOR:
        ch_title = chat.title or "Канал"
        logger.info("Bot added as administrator to channel '%s' (%s)", ch_title, chat.id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Назначить каналом для Прославления", callback_data=f"adm:set_wchan:{chat.id}")]
        ])
        for adm in db.get_admins():
            try:
                await context.bot.send_message(
                    chat_id=adm.user_id,
                    text=(
                        f"📢 <b>Бот добавлен администратором в канал!</b>\n\n"
                        f"• <b>Название:</b> {html.escape(ch_title)}\n"
                        f"• <b>ID канала:</b> <code>{chat.id}</code>\n\n"
                        f"Нажмите кнопку ниже, чтобы автоматически отправлять туда <b>Прославление</b>:"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
            except Exception as e:
                logger.warning("Could not notify admin %s about channel %s: %s", adm.user_id, chat.id, e)


async def run_split_and_send(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    file_path: str,
    title: str,
    worship_leader: Optional[str],
    is_hip: bool,
    w_s: int,
    w_e: int,
    s_s: int,
    s_e: int,
) -> None:
    """Выполняет нарезку аудио, отправку файлов в чат и каналы, а также очистку временных файлов."""
    db.clear_split_session(user_id)

    cut_msg = await message.reply_text(
        f"✂️ <b>Разрезаю запись на «Прославление» и «Проповедь»...</b>\n"
        f"«{html.escape(title)}»\n\n"
        f"<i>Если файл превышает 47 МБ, битрейт автоматически снижается под лимит Telegram 50 МБ...</i>",
        parse_mode=ParseMode.HTML,
    )

    try:
        loop = asyncio.get_running_loop()
        split_res: SplitResult = await loop.run_in_executor(
            None,
            split_service_audio,
            file_path,
            w_s,
            w_e,
            s_s,
            s_e,
            True,
            worship_leader,
            is_hip,
            title,
        )

        done_text = format_split_result_message(title, w_s, w_e, s_s, s_e, split_res)
        await cut_msg.edit_text(
            done_text + "\n\n📤 <b>Отправляю аудиофайлы в чат...</b>",
            parse_mode=ParseMode.HTML,
        )

        await send_split_files_to_chat(
            chat_id=message.chat_id,
            context=context,
            title=title,
            worship_leader=worship_leader,
            is_hip=is_hip,
            split_res=split_res,
        )

        await cut_msg.edit_text(
            done_text + "\n\n✅ <b>Оба аудиофайла отправлены в чат, временные файлы удалены с диска!</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.exception("Error splitting/sending audio file %s", file_path)
        await cut_msg.edit_text(
            f"⚠️ <b>Ошибка при нарезке/отправке файла:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode=ParseMode.HTML,
        )


async def handle_ocr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка кнопок подтверждения/сброса распознанных по скриншотам таймкодов."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user:
        return
    user_id = user.id
    if not db.is_user_allowed(user_id):
        return

    session = db.get_awaiting_split_session(user_id)
    if not session:
        await query.edit_message_text("⚠️ Сессия нарезки не найдена или уже завершена.")
        return

    file_path = session["file_path"]
    title = session["title"]
    worship_leader = session.get("leader") or "—"
    is_hip = bool(session.get("is_hip", 0))

    if query.data == "ocr_reset":
        db.update_split_ocr_times(user_id, [])
        await query.edit_message_text(
            "🔄 <b>Скриншоты сброшены.</b>\n\n"
            + format_manual_prompt_message(title, worship_leader, is_hip),
            parse_mode=ParseMode.HTML,
        )
        return

    if query.data == "ocr_confirm":
        try:
            times = json.loads(session.get("ocr_times") or "[]")
        except Exception:
            times = []

        if len(times) != 4:
            await query.edit_message_text("⚠️ Недостаточно распознанных скриншотов (нужно 4). Попробуйте сбросить и отправить заново.")
            return

        w_s, w_e, s_s, s_e = [int(t) for t in times]
        if w_s >= w_e or s_s >= s_e:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Сбросить и отправить заново", callback_data="ocr_reset")]
            ])
            await query.edit_message_text(
                "⚠️ <b>Некорректный порядок таймкодов:</b> время окончания должно быть больше времени начала.\n"
                "Нажмите кнопку ниже и отправьте 4 скриншота заново.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            return

        if not Path(file_path).is_file():
            db.clear_split_session(user_id)
            await query.edit_message_text("⚠️ Исходный файл уже был разделен или удален.")
            return

        await query.edit_message_reply_markup(reply_markup=None)
        await run_split_and_send(
            message=query.message,
            context=context,
            user_id=user_id,
            file_path=file_path,
            title=title,
            worship_leader=session.get("leader"),
            is_hip=is_hip,
            w_s=w_s,
            w_e=w_e,
            s_s=s_s,
            s_e=s_e,
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка сообщений (анкета регистрации, пересланный пост из канала, правки админа, таймкоды или ссылки на YouTube)."""
    message = update.message
    if not message:
        return

    user = update.effective_user
    user_id = user.id if user else 0

    # 0. Если администратор переслал сообщение из канала — определяем ID канала
    if db.is_user_admin(user_id) and isinstance(message.forward_origin, MessageOriginChannel):
        ch = message.forward_origin.chat
        ch_title = ch.title or "Канал"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Назначить каналом для Прославления", callback_data=f"adm:set_wchan:{ch.id}")]
        ])
        await message.reply_text(
            f"📢 <b>Обнаружен канал!</b>\n\n"
            f"• <b>Название:</b> {html.escape(ch_title)}\n"
            f"• <b>ID канала:</b> <code>{ch.id}</code>\n\n"
            f"Нажмите кнопку ниже, чтобы автоматически отправлять туда <b>Прославление</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    # 1. Если пользователь сейчас заполняет анкету регистрации (текст)
    if message.text and user_id in registration_states:
        handled = await process_registration_step(update, context)
        if handled:
            return

    # 2. Проверка прав доступа при каждом обращении
    if not db.is_user_allowed(user_id):
        if message.text or message.photo:
            logger.warning("Unauthorized access attempt by user %s (%s)", user_id, user.full_name if user else "Unknown")
            await send_no_access_message(message, user_id)
        return

    # 2.5. Если пользователь отправил скриншот плеера YouTube для распознавания таймкода (OCR)
    if message.photo:
        awaiting_session = db.get_awaiting_split_session(user_id)
        if not awaiting_session:
            await message.reply_text(
                "ℹ️ Сначала отправьте ссылку на YouTube или выберите файл через /split, а затем присылайте 4 скриншота."
            )
            return

        file_path = awaiting_session["file_path"]
        if not Path(file_path).is_file():
            db.clear_split_session(user_id)
            await message.reply_text("⚠️ Исходный файл уже был разделен или удален.")
            return

        tmp_img = f"/tmp/ocr_{user_id}_{uuid.uuid4().hex}.jpg"
        try:
            tg_file = await message.photo[-1].get_file()
            await tg_file.download_to_drive(custom_path=tmp_img)
            loop = asyncio.get_running_loop()
            sec = await loop.run_in_executor(None, extract_timestamp_from_image, tmp_img)
        finally:
            try:
                Path(tmp_img).unlink(missing_ok=True)
            except OSError:
                pass

        if sec is None:
            await message.reply_text(
                "⚠️ <b>Не удалось распознать таймкод на скриншоте.</b>\n"
                "Убедитесь, что на скриншоте видно время плеера (например, <code>03:37 / 2:10:05</code>), и отправьте его ещё раз.",
                parse_mode=ParseMode.HTML,
            )
            return

        step_labels = [
            "1️⃣ Начало прославления",
            "2️⃣ Конец прославления",
            "3️⃣ Начало проповеди",
            "4️⃣ Конец проповеди",
        ]
        lock = ocr_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            fresh_session = db.get_awaiting_split_session(user_id)
            if not fresh_session:
                return
            try:
                times = json.loads(fresh_session.get("ocr_times") or "[]")
            except Exception:
                times = []
            if len(times) >= 4:
                await message.reply_text(
                    "ℹ️ Уже собраны все 4 скриншота. Нажмите «✂️ Разрезать так» или «🔄 Сбросить» под предыдущим сообщением."
                )
                return
            times.append(int(sec))
            db.update_split_ocr_times(user_id, times)
            times_snapshot = list(times)

        count = len(times_snapshot)
        if count < 4:
            lines = [f"• {step_labels[i]}: <code>{format_timestamp(times_snapshot[i])}</code>" for i in range(count)]
            await message.reply_text(
                f"📸 <b>Скриншот {count}/4 распознан!</b>\n\n"
                + "\n".join(lines)
                + f"\n\n➡️ Отправьте следующий скриншот: <b>{step_labels[count]}</b>",
                parse_mode=ParseMode.HTML,
            )
        else:
            w_s, w_e, s_s, s_e = times_snapshot
            lines = [f"• {step_labels[i]}: <code>{format_timestamp(times_snapshot[i])}</code>" for i in range(4)]
            if w_s >= w_e or s_s >= s_e:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Сбросить и отправить заново", callback_data="ocr_reset")]
                ])
                await message.reply_text(
                    "⚠️ <b>Распознаны все 4 скриншота, но время окончания меньше или равно времени начала:</b>\n\n"
                    + "\n".join(lines)
                    + "\n\nПроверьте порядок скриншотов и нажмите кнопку ниже, чтобы отправить их заново.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
            else:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✂️ Разрезать так", callback_data="ocr_confirm")],
                    [InlineKeyboardButton("🔄 Сбросить", callback_data="ocr_reset")],
                ])
                await message.reply_text(
                    "✅ <b>Все 4 скриншота распознаны!</b>\n\n"
                    + "\n".join(lines)
                    + "\n\nПроверьте таймкоды и нажмите кнопку ниже:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
        return

    if not message.text:
        return

    # 3. Если администратор редактирует данные пользователя в админ-панели
    if user_id in admin_edit_states and db.is_user_admin(user_id):
        edit_state = admin_edit_states.pop(user_id)
        target_id = edit_state["target_id"]
        field = edit_state["field"]
        new_val = message.text.strip()

        kwargs = {field: new_val}
        updated_user = db.update_user_info(target_id, **kwargs)
        if updated_user:
            text, kb = build_user_card_view(
                updated_user,
                user_id,
                edit_state["filter_mode"],
                edit_state["page"],
            )
            await message.reply_text(
                "✅ <b>Данные пользователя обновлены!</b>\n\n" + text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        else:
            await message.reply_text("⚠️ Пользователь не найден.")
        return

    # 4. Проверяем, вводит ли пользователь таймкоды для нарезки (если сообщение не является ссылкой на YouTube)
    url = extract_youtube_url(message.text)
    awaiting_session = db.get_awaiting_split_session(user_id) if not url else None
    if not url and awaiting_session:
        file_path = awaiting_session["file_path"]
        title = awaiting_session["title"]
        worship_leader = awaiting_session.get("leader")
        is_hip = bool(awaiting_session.get("is_hip", 0))

        if not Path(file_path).is_file():
            db.clear_split_session(user_id)
            await message.reply_text("⚠️ Исходный файл уже был разделен или удален.")
            return

        parsed_times = parse_manual_timestamps(message.text)
        if not parsed_times:
            await message.reply_text(
                "⚠️ <b>Не удалось распознать 4 отметки времени.</b>\n"
                "Скопируйте шаблон ниже, исправьте время начала/конца <b>Прославления</b> (первая строка) "
                "и <b>Проповеди</b> (вторая строка) и отправьте ответным сообщением:\n\n"
                "<code>00:00:00 - 00:30:00\n00:40:00 - 01:40:00</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        w_s, w_e, s_s, s_e = parsed_times
        await run_split_and_send(
            message=message,
            context=context,
            user_id=user_id,
            file_path=file_path,
            title=title,
            worship_leader=worship_leader,
            is_hip=is_hip,
            w_s=w_s,
            w_e=w_e,
            s_s=s_s,
            s_e=s_e,
        )
        return

    if not url:
        await message.reply_text(
            "ℹ️ Пожалуйста, отправьте корректную ссылку на видео или трансляцию YouTube.\n"
            "Или используйте команду /split для нарезки уже скачанного файла.",
        )
        return

    if user_id in active_tasks:
        await message.reply_text(
            f"⏳ Вы уже запустили задачу: {active_tasks[user_id]}.\n"
            "Пожалуйста, дождитесь окончания текущей загрузки."
        )
        return

    status_msg = await message.reply_text(
        "🔍 <b>Проверяю видео на YouTube...</b>\n"
        "Получаю метаданные и статус трансляции.",
        parse_mode=ParseMode.HTML,
    )

    active_tasks[user_id] = "Проверка видео..."

    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, downloader.check_video_info, url)

        title = info.get("title", "Без названия")
        active_tasks[user_id] = f"Скачивание: «{title}»"

        await status_msg.edit_text(
            f"⬇️ <b>Начинаю скачивание аудиодорожки:</b>\n"
            f"«{html.escape(title)}»\n\n"
            f"⏳ Пожалуйста, подождите, это может занять от 10 секунд до пары минут...",
            parse_mode=ParseMode.HTML,
        )

        result: DownloadResult = await loop.run_in_executor(
            None, downloader.download_audio, url, 3
        )

        report_text = format_report_message(result)
        await status_msg.edit_text(report_text, parse_mode=ParseMode.HTML)
        logger.info("Successfully downloaded '%s' for user %s", result.title, user_id)

        await prompt_service_setup(message, user_id, result.file_path, result.title)

    except InvalidURLError as e:
        logger.warning("Invalid URL from user %s: %s", user_id, e)
        await status_msg.edit_text(f"❌ <b>Некорректная ссылка:</b>\n{e}", parse_mode=ParseMode.HTML)

    except LivestreamActiveError as e:
        logger.warning("Livestream active: %s", e)
        await status_msg.edit_text(
            f"🔴 <b>Трансляция не завершена:</b>\n{e}",
            parse_mode=ParseMode.HTML,
        )

    except VideoUnavailableError as e:
        logger.warning("Video unavailable: %s", e)
        await status_msg.edit_text(
            f"🚫 <b>Видео недоступно:</b>\n{e}",
            parse_mode=ParseMode.HTML,
        )

    except NetworkDownloadError as e:
        logger.error("Network download error: %s", e)
        await status_msg.edit_text(
            f"🌐 <b>Сетевая ошибка:</b>\n{e}\n\nПовторите попытку через пару минут.",
            parse_mode=ParseMode.HTML,
        )

    except PermissionOrDiskError as e:
        logger.error("Disk/permission error: %s", e)
        await status_msg.edit_text(
            f"💾 <b>Ошибка диска/прав доступа:</b>\n{e}",
            parse_mode=ParseMode.HTML,
        )

    except DownloaderError as e:
        logger.error("Downloader error: %s", e)
        await status_msg.edit_text(
            f"⚠️ <b>Ошибка при скачивании:</b>\n{e}",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.exception("Unexpected error in handle_message")
        await status_msg.edit_text(
            f"💥 <b>Произошла непредвиденная ошибка:</b>\n<code>{html.escape(str(e))}</code>\n\n"
            f"Подробности сохранены в файле <code>bot.log</code>.",
            parse_mode=ParseMode.HTML,
        )

    finally:
        active_tasks.pop(user_id, None)


def main() -> None:
    """Точка входа запуска бота."""
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        print("\n" + "=" * 60)
        print("ОШИБКА: Не задан токен Telegram-бота!")
        print("1. Откройте файл .env (скопируйте из .env.example)")
        print("2. Укажите TELEGRAM_BOT_TOKEN=ваш_токен_от_BotFather")
        print("3. Перезапустите бота: python bot.py")
        print("=" * 60 + "\n")
        sys.exit(1)

    admins = [a.user_id for a in db.get_admins()]
    print("Запуск Telegram-бота (long polling)...")
    print(f"База данных: {config.DB_PATH}")
    print(f"Папка сохранения аудио: {config.DOWNLOAD_DIR_PATH}")
    print(f"Администраторы (ID): {admins}")

    app = (
        ApplicationBuilder()
        .token(token)
        .read_timeout(300)
        .write_timeout(300)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("split", split_command))
    app.add_handler(CommandHandler("update_ytdlp", update_ytdlp_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("sort_channel", sort_channel_command))
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    app.add_handler(CallbackQueryHandler(handle_registration_callback, pattern=r"^req_"))
    app.add_handler(CallbackQueryHandler(handle_admin_callback, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(handle_setup_callback, pattern=r"^s(type|leader|back):"))
    app.add_handler(CallbackQueryHandler(handle_ocr_callback, pattern=r"^ocr_"))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.FORWARDED) & ~filters.COMMAND, handle_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

    if restart_requested:
        logger.info("Restarting bot process to apply updated yt-dlp...")
        os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == "__main__":
    main()
