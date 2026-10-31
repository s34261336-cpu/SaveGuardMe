"""SaveMod — уведомления об изменении и удалении Business-сообщений."""

import asyncio
import html
import logging
import os
from collections import OrderedDict
from typing import TypedDict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

# Добавьте BOT_TOKEN и OWNER_ID в Replit Secrets.
# Не вставляйте реальные значения токена прямо в этот файл.
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID_VALUE = os.getenv("OWNER_ID")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Добавьте его в Replit Secrets.")

if not OWNER_ID_VALUE:
    raise RuntimeError("Не задан OWNER_ID. Добавьте его в Replit Secrets.")

try:
    OWNER_ID = int(OWNER_ID_VALUE)
except ValueError as exc:
    raise RuntimeError("OWNER_ID должен быть числовым Telegram ID.") from exc

if OWNER_ID <= 0:
    raise RuntimeError("OWNER_ID должен быть положительным числовым Telegram ID.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("savemod")

# Telegram присваивает message_id отдельно в каждом чате, поэтому ключ
# содержит и chat_id — так сообщения разных личных чатов не перезапишут друг друга.
class MessageSnapshot(TypedDict):
    type: str
    text: str | None
    caption: str | None
    sender_name: str | None
    sender_username: str | None
    sender_id: int | None


message_cache: OrderedDict[tuple[int, int], MessageSnapshot] = OrderedDict()
business_connections: dict[str, bool] = {}

# Кэш остается только в RAM и ограничен, чтобы процесс не накапливал данные
# бесконечно. При переполнении удаляются самые старые записи.
MAX_CACHED_MESSAGES = 50_000
MAX_CONTENT_LENGTH = 3_000

SUPPORTED_TYPES = {
    "text",
    "photo",
    "video",
    "voice",
    "document",
    "sticker",
    "audio",
    "video_note",
}

TYPE_LABELS = {
    "photo": "фото",
    "video": "видео",
    "voice": "голосовое сообщение",
    "document": "документ",
    "sticker": "стикер",
    "audio": "аудио",
    "video_note": "видеосообщение",
}

router = Router()


def clip_content(value: str) -> str:
    """Ограничивает размер текста уведомления, сохраняя его начало."""
    if len(value) <= MAX_CONTENT_LENGTH:
        return value
    return value[: MAX_CONTENT_LENGTH - 1] + "…"


def message_key(message: Message) -> tuple[int, int]:
    """Возвращает уникальный для кэша ключ сообщения."""
    return message.chat.id, message.message_id


def make_snapshot(message: Message) -> MessageSnapshot | None:
    """Сохраняет тип, текст, подпись и автора; медиафайлы не скачиваются."""
    message_type = str(getattr(message.content_type, "value", message.content_type))
    if message_type not in SUPPORTED_TYPES:
        return None

    # Для обычных сообщений используем автора, а для сообщений от другого
    # Business-бота — sender_business_bot, если он присутствует.
    sender = message.from_user or getattr(message, "sender_business_bot", None)

    return {
        "type": message_type,
        "text": message.text if message_type == "text" else None,
        "caption": message.caption,
        "sender_name": sender.full_name if sender else None,
        "sender_username": sender.username if sender else None,
        "sender_id": sender.id if sender else None,
    }


def cache_message(message: Message) -> MessageSnapshot | None:
    """Обновляет RAM-кэш и удаляет наиболее старые записи при его переполнении."""
    snapshot = make_snapshot(message)
    if snapshot is None:
        return None

    key = message_key(message)
    message_cache[key] = snapshot
    message_cache.move_to_end(key)

    while len(message_cache) > MAX_CACHED_MESSAGES:
        oldest_key = next(iter(message_cache))
        del message_cache[oldest_key]

    return snapshot


def describe_snapshot(snapshot: MessageSnapshot) -> str:
    """Готовит безопасное текстовое описание текста или типа медиа."""
    if snapshot["type"] == "text":
        return clip_content(snapshot["text"] or "")

    label = TYPE_LABELS.get(snapshot["type"], snapshot["type"])
    caption = snapshot["caption"]
    if caption:
        return f"{label} (подпись: {clip_content(caption)})"
    return label


async def notify_owner(
    bot: Bot, text: str, *, parse_mode: str | None = None
) -> None:
    """Отправляет уведомление владельцу и оставляет понятную запись в логе при ошибке."""
    try:
        await bot.send_message(
            chat_id=OWNER_ID,
            text=text,
            parse_mode=parse_mode,
        )
    except TelegramAPIError:
        logger.exception(
            "Не удалось отправить уведомление владельцу. "
            "Убедитесь, что владелец открыл бота и нажал /start."
        )


def format_deleted_notification(snapshot: MessageSnapshot | None) -> str:
    """Собирает компактное HTML-уведомление с автором и содержимым."""
    if snapshot is None:
        return (
            "🗑 <b>Сообщение удалено</b>\n"
            "👤 <b>Отправитель:</b> неизвестен\n"
            "📝 <i>Содержимое неизвестно</i>"
        )

    sender_name = snapshot["sender_name"] or "неизвестен"
    escaped_name = html.escape(sender_name)
    sender_id = snapshot["sender_id"]
    if sender_id:
        sender_label = f'<a href="tg://user?id={sender_id}">{escaped_name}</a>'
    else:
        sender_label = escaped_name

    username = snapshot["sender_username"]
    if username:
        sender_label += f" · @{html.escape(username)}"

    content = html.escape(describe_snapshot(snapshot))
    return (
        "🗑 <b>Сообщение удалено</b>\n"
        f"👤 <b>Отправитель:</b> {sender_label}\n"
        f"<blockquote>{content}</blockquote>"
    )


async def is_owner_connection(bot: Bot, connection_id: str) -> bool:
    """Проверяет, что Business-подключение принадлежит заданному владельцу."""
    if connection_id in business_connections:
        return business_connections[connection_id]

    # После перезапуска словарь RAM пуст. Проверяем неизвестное подключение
    # через Telegram API, а не доверяем ему без подтверждения владельца.
    try:
        connection = await bot.get_business_connection(
            business_connection_id=connection_id
        )
    except TelegramAPIError:
        logger.exception("Не удалось проверить Business-подключение.")
        return False

    is_owner = connection.user.id == OWNER_ID and connection.is_enabled
    business_connections[connection_id] = is_owner
    return is_owner


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start_handler(message: Message) -> None:
    """Показывает короткую инструкцию по подключению."""
    await message.answer(
        "🤖 SaveMod готов к подключению.\n\n"
        "Откройте Settings → Automatization и подключите этого бота "
        "к своему Telegram Business-аккаунту.\n\n"
        "После подключения я буду присылать уведомления об изменённых "
        "и удалённых сообщениях в личных чатах.\n\n"
        "Кэш временный: он хранится только в памяти и очищается "
        "при перезапуске бота."
    )


@router.business_connection()
async def business_connection_handler(
    connection: BusinessConnection, bot: Bot
) -> None:
    """Запоминает ID только Business-подключения заданного владельца."""
    if connection.user.id != OWNER_ID:
        logger.info("Пропущено Business-подключение другого аккаунта.")
        return

    was_enabled = business_connections.get(connection.id, False)
    business_connections[connection.id] = connection.is_enabled

    if connection.is_enabled and not was_enabled:
        await notify_owner(bot, "✅ Бот подключён")
    elif not connection.is_enabled:
        logger.info("Business-подключение владельца отключено.")


@router.business_message()
async def business_message_handler(message: Message, bot: Bot) -> None:
    """Кэширует поддерживаемые сообщения только из личных чатов владельца."""
    if message.chat.type != ChatType.PRIVATE or not message.business_connection_id:
        return

    if not await is_owner_connection(bot, message.business_connection_id):
        return

    cache_message(message)


@router.edited_business_message()
async def edited_business_message_handler(message: Message, bot: Bot) -> None:
    """Сообщает владельцу прежнее и текущее содержимое отредактированного сообщения."""
    if message.chat.type != ChatType.PRIVATE or not message.business_connection_id:
        return

    if not await is_owner_connection(bot, message.business_connection_id):
        return

    key = message_key(message)
    previous = message_cache.get(key)
    current = cache_message(message)
    if current is None:
        return

    previous_text = (
        describe_snapshot(previous) if previous else "содержимое неизвестно"
    )
    current_text = describe_snapshot(current)
    await notify_owner(
        bot,
        f"✏️ Отредактировано: было «{previous_text}», стало «{current_text}»",
    )


@router.deleted_business_messages()
async def deleted_business_messages_handler(
    update: BusinessMessagesDeleted, bot: Bot
) -> None:
    """Отправляет уведомление для каждого удалённого сообщения из личного чата."""
    if update.chat.type != ChatType.PRIVATE:
        return

    if not await is_owner_connection(bot, update.business_connection_id):
        return

    for message_id in update.message_ids:
        snapshot = message_cache.pop((update.chat.id, message_id), None)
        await notify_owner(
            bot,
            format_deleted_notification(snapshot),
            parse_mode="HTML",
        )


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    logger.info("SaveMod запущен. Ожидание Telegram Business-событий.")
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
