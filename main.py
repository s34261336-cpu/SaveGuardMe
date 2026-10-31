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

# Добавьте BOT_TOKEN в Secrets на выбранном хостинге.
# Не вставляйте реальные значения токена прямо в этот файл.
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Добавьте его в Secrets выбранного хостинга."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("savemod")

# message_id уникален только внутри чата. Business connection также входит
# в ключ, чтобы данные разных подключивших аккаунтов оставались раздельными.
class MessageSnapshot(TypedDict):
    type: str
    text: str | None
    caption: str | None
    sender_name: str | None
    sender_username: str | None
    sender_id: int | None


message_cache: OrderedDict[tuple[str, int, int], MessageSnapshot] = OrderedDict()
business_connections: dict[str, int | None] = {}

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


def message_key(message: Message) -> tuple[str, int, int]:
    """Возвращает ключ сообщения, изолированный по Business-подключению."""
    return (
        message.business_connection_id or "",
        message.chat.id,
        message.message_id,
    )


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


async def notify_user(
    bot: Bot,
    user_chat_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
) -> None:
    """Отправляет уведомление пользователю своего Business-подключения."""
    try:
        await bot.send_message(
            chat_id=user_chat_id,
            text=text,
            parse_mode=parse_mode,
        )
    except TelegramAPIError:
        logger.exception("Не удалось отправить уведомление владельцу подключения.")


def format_sender(snapshot: MessageSnapshot | None) -> str:
    """Формирует безопасную HTML-ссылку на автора сообщения."""
    if snapshot is None:
        return "неизвестен"
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

    return sender_label


def format_deleted_notification(
    snapshot: MessageSnapshot | None,
    message_id: int,
) -> str:
    """Собирает компактное HTML-уведомление с автором и содержимым."""
    if snapshot is None:
        return (
            "🗑 <b>Сообщение удалено</b>\n"
            "👤 <b>Автор:</b> неизвестен\n"
            f"🆔 <code>{message_id}</code>\n"
            "📝 <i>Содержимое неизвестно</i>"
        )

    content = html.escape(describe_snapshot(snapshot))
    return (
        "🗑 <b>Сообщение удалено</b>\n"
        f"👤 <b>Автор:</b> {format_sender(snapshot)}\n"
        f"🆔 <code>{message_id}</code>\n"
        f"<blockquote>{content}</blockquote>"
    )


def format_edited_notification(
    previous: MessageSnapshot | None,
    current: MessageSnapshot,
    message_id: int,
) -> str:
    """Показывает автора и значения до/после редактирования."""
    sender = format_sender(current)
    if sender == "неизвестен" and previous is not None:
        sender = format_sender(previous)

    old_content = (
        describe_snapshot(previous) if previous else "содержимое неизвестно"
    )
    new_content = describe_snapshot(current)
    return (
        "✏️ <b>Сообщение изменено</b>\n"
        f"👤 <b>Автор:</b> {sender}\n"
        f"🆔 <code>{message_id}</code>\n"
        "<blockquote>"
        f"<b>Было:</b> {html.escape(old_content)}\n"
        f"<b>Стало:</b> {html.escape(new_content)}"
        "</blockquote>"
    )


async def get_connection_user_chat_id(
    bot: Bot, connection_id: str
) -> int | None:
    """Получает чат пользователя, который явно подключил Business-аккаунт."""
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
        return None

    user_chat_id = (
        connection.user_chat_id if connection.is_enabled else None
    )
    business_connections[connection_id] = user_chat_id
    return user_chat_id


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start_handler(message: Message) -> None:
    """Показывает короткую инструкцию по подключению."""
    await message.answer(
        "🤖 SaveMod готов к подключению.\n\n"
        "Откройте Settings → Automatization и подключите этого бота "
        "к своему Telegram Business-аккаунту.\n\n"
        "После подключения я буду присылать уведомления об изменённых "
        "и удалённых сообщениях в личных чатах.\n\n"
        "Уведомления получает только пользователь, подключивший свой аккаунт. "
        "Текст и подписи временно хранятся в RAM; медиа не скачиваются.\n\n"
        "Кэш временный: он хранится только в памяти и очищается "
        "при перезапуске бота."
    )


@router.business_connection()
async def business_connection_handler(
    connection: BusinessConnection, bot: Bot
) -> None:
    """Регистрирует отдельное подключение каждого пользователя."""
    was_enabled = business_connections.get(connection.id) is not None
    if connection.is_enabled:
        business_connections[connection.id] = connection.user_chat_id
        if not was_enabled:
            await notify_user(
                bot,
                connection.user_chat_id,
                "✅ Бот подключён",
            )
    else:
        business_connections[connection.id] = None
        for key in list(message_cache):
            if key[0] == connection.id:
                del message_cache[key]
        logger.info("Пользователь отключил Business-подключение.")


@router.business_message()
async def business_message_handler(message: Message, bot: Bot) -> None:
    """Кэширует поддерживаемые сообщения подключённого пользователя."""
    if message.chat.type != ChatType.PRIVATE or not message.business_connection_id:
        return

    if await get_connection_user_chat_id(bot, message.business_connection_id) is None:
        return

    cache_message(message)


@router.edited_business_message()
async def edited_business_message_handler(message: Message, bot: Bot) -> None:
    """Сообщает подключившемуся пользователю прежнее и текущее содержимое."""
    if message.chat.type != ChatType.PRIVATE or not message.business_connection_id:
        return

    user_chat_id = await get_connection_user_chat_id(
        bot, message.business_connection_id
    )
    if user_chat_id is None:
        return

    key = message_key(message)
    previous = message_cache.get(key)
    current = cache_message(message)
    if current is None:
        return

    await notify_user(
        bot,
        user_chat_id,
        format_edited_notification(previous, current, message.message_id),
        parse_mode="HTML",
    )


@router.deleted_business_messages()
async def deleted_business_messages_handler(
    update: BusinessMessagesDeleted, bot: Bot
) -> None:
    """Отправляет уведомление владельцу подключения для каждого удаления."""
    if update.chat.type != ChatType.PRIVATE:
        return

    user_chat_id = await get_connection_user_chat_id(
        bot, update.business_connection_id
    )
    if user_chat_id is None:
        return

    for message_id in update.message_ids:
        key = (update.business_connection_id, update.chat.id, message_id)
        snapshot = message_cache.pop(key, None)
        await notify_user(
            bot,
            user_chat_id,
            format_deleted_notification(snapshot, message_id),
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
            # События обрабатываются по порядку: исходное сообщение должно
            # попасть в кэш до того, как обработается его редактирование.
            handle_as_tasks=False,
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
