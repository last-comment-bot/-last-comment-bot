import asyncio
import logging
import os
import sqlite3
import time
from html import escape

from telegram import Update
from telegram.constants import ChatType, ParseMode
from dotenv import load_dotenv

load_dotenv()

from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

OWNER_ID = 8275854816
TIMER_SECONDS = 5 * 60
DB_PATH = os.getenv("DB_PATH", "giveaways.sqlite3")
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("last-comment-bot")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS contests (
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    last_user_id INTEGER,
    last_user_name TEXT,
    last_message_id INTEGER,
    deadline REAL,
    PRIMARY KEY (chat_id, thread_id)
)
""")
db.commit()

# Tasks are keyed by (chat_id, thread_id). The database keeps deadlines
# so a restart does not erase a running contest.
tasks: dict[tuple[int, int], asyncio.Task] = {}


def owner_only(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == OWNER_ID)


def get_thread_id(message) -> int:
    """Return a discussion/topic thread ID, or 0 for the whole group."""
    if message.is_automatic_forward:
        return message.message_id
    if message.message_thread_id:
        return message.message_thread_id
    replied = message.reply_to_message
    if replied:
        if replied.is_automatic_forward:
            return replied.message_id
        if replied.message_thread_id:
            return replied.message_thread_id
    return 0


def get_contest(chat_id: int, thread_id: int):
    return db.execute(
        """SELECT active, last_user_id, last_user_name, last_message_id, deadline
           FROM contests WHERE chat_id=? AND thread_id=?""",
        (chat_id, thread_id),
    ).fetchone()


async def announce_winner(chat_id: int, thread_id: int, user_name: str):
    safe_name = escape(user_name or "Участник")
    text = (
        "🏆 <b>Розыгрыш завершён!</b>\n\n"
        f"Последним участником стал {safe_name}.\n"
        "Поздравляем с победой! 🎁"
    )
    kwargs = {"chat_id": chat_id, "text": text, "parse_mode": ParseMode.HTML}
    if thread_id:
        kwargs["reply_to_message_id"] = thread_id
    try:
        await application.bot.send_message(**kwargs)
    except Exception:
        log.exception("Не удалось объявить победителя в чате %s", chat_id)


async def countdown(chat_id: int, thread_id: int, deadline: float):
    key = (chat_id, thread_id)
    try:
        await asyncio.sleep(max(0, deadline - time.time()))
        row = get_contest(chat_id, thread_id)
        if not row:
            return
        active, user_id, user_name, message_id, current_deadline = row
        # A newer message has reset the timer; this task is obsolete.
        if not active or current_deadline != deadline:
            return

        db.execute(
            "UPDATE contests SET active=0 WHERE chat_id=? AND thread_id=?",
            (chat_id, thread_id),
        )
        db.commit()
        if user_id is not None:
            await announce_winner(chat_id, thread_id, user_name)
    except asyncio.CancelledError:
        pass
    finally:
        if tasks.get(key) is asyncio.current_task():
            tasks.pop(key, None)


def schedule_countdown(chat_id: int, thread_id: int, deadline: float):
    key = (chat_id, thread_id)
    old = tasks.get(key)
    if old and not old.done():
        old.cancel()
    tasks[key] = asyncio.create_task(countdown(chat_id, thread_id, deadline))


async def startgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        if message:
            await message.reply_text("Запусти команду в нужной группе или в комментариях под постом.")
        return

    thread_id = get_thread_id(message)
    deadline = time.time() + TIMER_SECONDS
    db.execute(
        """INSERT INTO contests
           (chat_id, thread_id, active, last_user_id, last_user_name, last_message_id, deadline)
           VALUES (?, ?, 1, NULL, NULL, NULL, ?)
           ON CONFLICT(chat_id, thread_id) DO UPDATE SET
             active=1, last_user_id=NULL, last_user_name=NULL,
             last_message_id=NULL, deadline=excluded.deadline""",
        (chat.id, thread_id, deadline),
    )
    db.commit()
    schedule_countdown(chat.id, thread_id, deadline)
    await message.reply_text(
        "🎁 Розыгрыш запущен!\n"
        "Таймер — 5 минут после последнего сообщения участника.\n"
        "Если новых сообщений не будет, победит последний написавший."
    )


async def stopgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    thread_id = get_thread_id(message)
    db.execute(
        "UPDATE contests SET active=0 WHERE chat_id=? AND thread_id=?",
        (chat.id, thread_id),
    )
    db.commit()
    task = tasks.pop((chat.id, thread_id), None)
    if task and not task.done():
        task.cancel()
    await message.reply_text("⏹ Розыгрыш остановлен. Победитель не объявлялся.")


async def newgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Starting again clears the previous winner and starts a fresh 5-minute round.
    await startgiveaway(update, context)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    thread_id = get_thread_id(message)
    row = get_contest(chat.id, thread_id)
    if not row or not row[0]:
        await message.reply_text("Сейчас в этом чате/обсуждении нет активного розыгрыша.")
        return
    _, user_id, user_name, _, deadline = row
    remaining = max(0, int(deadline - time.time() + 0.999))
    mins, secs = divmod(remaining, 60)
    last = user_name or "Пока никто не участвовал"
    await message.reply_text(
        f"🎁 Розыгрыш активен.\n"
        f"Последний участник: {last}\n"
        f"До завершения: {mins:02d}:{secs:02d}"
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user or update.edited_message:
        return
    if user.is_bot or user.id == OWNER_ID:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if message.text and message.text.startswith("/"):
        return

    thread_id = get_thread_id(message)
    row = get_contest(chat.id, thread_id)
    if not row or not row[0]:
        return

    display_name = user.full_name or (f"@{user.username}" if user.username else str(user.id))
    deadline = time.time() + TIMER_SECONDS
    db.execute(
        """UPDATE contests
           SET last_user_id=?, last_user_name=?, last_message_id=?, deadline=?
           WHERE chat_id=? AND thread_id=? AND active=1""",
        (user.id, display_name, message.message_id, deadline, chat.id, thread_id),
    )
    db.commit()
    schedule_countdown(chat.id, thread_id, deadline)


async def post_init(app: Application):
    # Restore active contests after a process restart.
    rows = db.execute(
        "SELECT chat_id, thread_id, deadline FROM contests WHERE active=1"
    ).fetchall()
    for chat_id, thread_id, deadline in rows:
        schedule_countdown(chat_id, thread_id, deadline)


async def post_shutdown(app: Application):
    for task in list(tasks.values()):
        task.cancel()
    db.close()


if not BOT_TOKEN:
    raise SystemExit(
        "Не найден BOT_TOKEN. Создай файл .env по примеру .env.example "
        "или задай переменную окружения BOT_TOKEN."
    )

application = (
    Application.builder()
    .token(BOT_TOKEN)
    .post_init(post_init)
    .post_shutdown(post_shutdown)
    .build()
)
application.add_handler(CommandHandler("startgiveaway", startgiveaway))
application.add_handler(CommandHandler("stopgiveaway", stopgiveaway))
application.add_handler(CommandHandler("newgiveaway", newgiveaway))
application.add_handler(CommandHandler("status", status))
application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))

if __name__ == "__main__":
    log.info("Бот запущен. ID владельца: %s", OWNER_ID)
    application.run_polling(allowed_updates=Update.ALL_TYPES)
