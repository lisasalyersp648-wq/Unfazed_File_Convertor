import logging

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_ACTIVE = {"administrator", "creator"}


def build_channel_id_text(chat_id: int, title: str) -> str:
    return (
        "\U0001f4e1 <b>Channel connected</b>\n\n"
        f"<b>{title}</b>\n"
        f"<code>{chat_id}</code>\n\n"
        "Use this as <code>STORAGE_CHANNEL_ID</code> in both the Library and "
        "Converter bots. Both bots must stay administrators of this channel."
    )


async def channel_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report the chat id when the bot is promoted to channel admin.

    The Library bot needs the same numeric id for STORAGE_CHANNEL_ID, so this
    saves the operator from digging it out of Telegram links.
    """
    change = update.my_chat_member
    if not change or change.chat.type not in ("channel", "supergroup"):
        return
    if change.new_chat_member.status not in _ACTIVE:
        return

    actor = change.from_user
    text = build_channel_id_text(change.chat.id, change.chat.title or "Untitled chat")

    if actor and not actor.is_bot:
        try:
            await context.bot.send_message(actor.id, text)
            return
        except Exception as exc:
            logger.debug("Could not DM channel id to %s: %s", actor.id, exc)

    try:
        await context.bot.send_message(change.chat.id, text)
    except Exception as exc:
        logger.warning("Could not report chat id for %s: %s", change.chat.id, exc)
