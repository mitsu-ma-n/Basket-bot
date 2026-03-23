#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
import sys
from logging.handlers import RotatingFileHandler
import asyncpg
from html import escape as html_escape
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    InlineQueryHandler,
    filters,
    ContextTypes,
)
from dotenv import load_dotenv

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL_NUM = getattr(logging, LOG_LEVEL, logging.INFO)

_log_format = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter(_log_format))
console_handler.setLevel(LOG_LEVEL_NUM)

file_handler = RotatingFileHandler(
    "bot_debug.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setFormatter(logging.Formatter(_log_format))
file_handler.setLevel(LOG_LEVEL_NUM)

logging.basicConfig(
    level=LOG_LEVEL_NUM,
    format=_log_format,
    handlers=[console_handler, file_handler],
    force=True,
)

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL_NUM)

# Reduce noise from telegram/httpx internals (can override via env vars).
logging.getLogger("telegram").setLevel(getattr(logging, os.getenv("TELEGRAM_LOG_LEVEL", "WARNING").upper(), logging.WARNING))
logging.getLogger("httpx").setLevel(getattr(logging, os.getenv("HTTPX_LOG_LEVEL", "WARNING").upper(), logging.WARNING))

# Globals
MAIN_PLAYERS_LIMIT = 12

# States
POLL_NAME = range(1)

# ────────────────────────────────────────────
# DATABASE ACCESS LAYER
# ────────────────────────────────────────────

class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        db_url = os.getenv("DATABASE_URL")
        logger.info("Database.connect: DATABASE_URL set=%s", bool(db_url))
        if not db_url:
            raise RuntimeError("DATABASE_URL is not set")
        self.pool = await asyncpg.create_pool(db_url)
        logger.info("Database.connect: pool created")
        await self.create_tables()
        logger.info("Database.connect: tables ensured")

    async def create_tables(self):
        logger.debug("Database.create_tables: start")
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS polls (
                    poll_id TEXT PRIMARY KEY,
                    creator_id BIGINT,
                    chat_id BIGINT,
                    message_id BIGINT,
                    poll_name TEXT
                );
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS poll_votes (
                    poll_id TEXT,
                    user_id BIGINT,
                    user_name TEXT,
                    choice SMALLINT,
                    voted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (poll_id, user_id),
                    FOREIGN KEY (poll_id) REFERENCES polls(poll_id) ON DELETE CASCADE
                );
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_states (
                    chat_id BIGINT,
                    user_id BIGINT,
                    state TEXT,
                    PRIMARY KEY (chat_id, user_id)
                );
            """)
        logger.debug("Database.create_tables: done")

    # USER STATES -----------------------------------------------------------

    async def set_user_state(self, chat_id, user_id, state):
        logger.debug("Database.set_user_state: chat_id=%s user_id=%s state=%s", chat_id, user_id, state)
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                INSERT INTO user_states(chat_id, user_id, state)
                VALUES($1, $2, $3)
                ON CONFLICT (chat_id, user_id)
                DO UPDATE SET state=EXCLUDED.state;
                """, chat_id, user_id, state)
        except Exception:
            logger.exception("Database.set_user_state failed")
            raise

    async def get_user_state(self, chat_id, user_id):
        logger.debug("Database.get_user_state: chat_id=%s user_id=%s", chat_id, user_id)
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                SELECT state FROM user_states
                WHERE chat_id=$1 AND user_id=$2
                """, chat_id, user_id)
                return row["state"] if row else None
        except Exception:
            logger.exception("Database.get_user_state failed")
            raise

    async def clear_user_state(self, chat_id, user_id):
        logger.debug("Database.clear_user_state: chat_id=%s user_id=%s", chat_id, user_id)
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                DELETE FROM user_states
                WHERE chat_id=$1 AND user_id=$2
                """, chat_id, user_id)
        except Exception:
            logger.exception("Database.clear_user_state failed")
            raise

    # POLLS -----------------------------------------------------------------

    async def create_poll(self, poll_id, creator_id, chat_id, message_id, poll_name):
        logger.info(
            "Database.create_poll: poll_id=%s creator_id=%s chat_id=%s message_id=%s",
            poll_id, creator_id, chat_id, message_id
        )
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                INSERT INTO polls(poll_id, creator_id, chat_id, message_id, poll_name)
                VALUES($1, $2, $3, $4, $5)
                """, poll_id, creator_id, chat_id, message_id, poll_name)
        except Exception:
            logger.exception("Database.create_poll failed")
            raise

    async def get_poll(self, poll_id):
        logger.debug("Database.get_poll: poll_id=%s", poll_id)
        try:
            async with self.pool.acquire() as conn:
                poll = await conn.fetchrow("SELECT * FROM polls WHERE poll_id=$1", poll_id)
                if not poll:
                    logger.debug("Database.get_poll: not found poll_id=%s", poll_id)
                    return None

                votes = await conn.fetch("""
                    SELECT user_id, user_name, choice, voted_at
                    FROM poll_votes
                    WHERE poll_id=$1
                    ORDER BY voted_at ASC
                """, poll_id)

                #voters1 = [v["user_name"] for v in votes if v["choice"] == 1]
                voters1 = [{"user_id": v["user_id"], "user_name": v["user_name"]}  for v in votes if v["choice"] == 1]

                #voters2 = [v["user_name"] for v in votes if v["choice"] == 2]
                voters2 = [{"user_id": v["user_id"], "user_name": v["user_name"]}  for v in votes if v["choice"] == 2]

                return {
                    "poll_id": poll["poll_id"],
                    "creator_id": poll["creator_id"],
                    "chat_id": poll["chat_id"],
                    "message_id": poll["message_id"],
                    "poll_name": poll["poll_name"],
                    "voters1": voters1,
                    "voters2": voters2
                }
        except Exception:
            logger.exception("Database.get_poll failed")
            raise

    async def add_vote(self, poll_id, user_id, user_name, choice):
        logger.info(
            "Database.add_vote: poll_id=%s user_id=%s choice=%s user_name_len=%s",
            poll_id, user_id, choice, len(user_name) if user_name else 0
        )
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                INSERT INTO poll_votes(poll_id, user_id, user_name, choice, voted_at)
                VALUES($1, $2, $3, $4, NOW())
                ON CONFLICT (poll_id, user_id)
                DO UPDATE SET
                    user_name = EXCLUDED.user_name,
                    choice = EXCLUDED.choice,
                    voted_at = NOW();
                """, poll_id, user_id, user_name, choice)
        except Exception:
            logger.exception("Database.add_vote failed")
            raise


db = Database()

# ────────────────────────────────────────────
# BUSINESS LOGIC
# ────────────────────────────────────────────

def format_poll_message(poll):
    poll_name = poll["poll_name"]
    voters1 = poll["voters1"]
    voters2 = poll["voters2"]

    main = voters1[:MAIN_PLAYERS_LIMIT]
    reserve = voters1[MAIN_PLAYERS_LIMIT:]

    logger.debug("format_poll_message: start poll_id=%s", poll.get("poll_id"))

    #logger.info(" Main: %s\n Reserve: %s\n Voters2: %s\n", main, reserve, voters2)

    def user_link(user_id: int, user_name: str) -> str:
        # Make the whole displayed name clickable to open the user's Telegram profile/contact card.
        # `tg://user?id=...` works even if the user has hidden their @username.
        safe_name = html_escape(user_name)
        return f'<a href="tg://user?id={user_id}">{safe_name}</a>'

    msg = f"📊 {html_escape(poll_name)}\n\n"

    msg += "✅ Иду\n"
    if main:
        msg += "\n".join(
            f"{i+1}. {user_link(v['user_id'], v['user_name'])}"
            for i, v in enumerate(main)
        ) + "\n"
    else:
        msg += "Нет участников\n"

    if reserve:
        msg += "\n🔄 Запас\n"
        msg += "\n".join(f"• {user_link(v['user_id'], v['user_name'])}" for v in reserve) + "\n"

    msg += "\n❌ Пропущу\n"
    if voters2:
        msg += "\n".join(f"• {user_link(v['user_id'], v['user_name'])}" for v in voters2) + "\n"
    else:
        msg += "Нет участников\n"

    logger.debug("format_poll_message: done (len=%s)", len(msg))

    return msg


# ────────────────────────────────────────────
# COMMAND HANDLERS
# ────────────────────────────────────────────

async def start(update, context):
    try:
        logger.info(
            "Handler start (/start): user_id=%s chat_id=%s",
            update.effective_user.id if update.effective_user else None,
            update.effective_chat.id if update.effective_chat else None,
        )
        await update.message.reply_text(
            "Привет! Я бот для создания голосований.\n\n"
            "/create — создать голосование\n"
            "/help — помощь"
        )
    except Exception:
        logger.exception("Handler start failed")
        raise


async def help_command(update, context):
    try:
        logger.info(
            "Handler help (/help): user_id=%s chat_id=%s",
            update.effective_user.id if update.effective_user else None,
            update.effective_chat.id if update.effective_chat else None,
        )
        await update.message.reply_text(
            "Инструкция по созданию голосования:\n"
            "1) Нажмите /create\n"
            "2) Введите название голосования ответом на сообщение бота"
        )
    except Exception:
        logger.exception("Handler help_command failed")
        raise


async def create_poll(update, context):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    logger.info("Handler create_poll: user_id=%s chat_id=%s", user_id, chat_id)
    try:
        await db.set_user_state(chat_id, user_id, "waiting_name")
        await update.message.reply_text("Введите название голосования:")
        return POLL_NAME
    except Exception:
        logger.exception("Handler create_poll failed")
        raise


async def poll_name(update, context):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    logger.info("Handler poll_name: user_id=%s chat_id=%s", user_id, chat_id)
    state = await db.get_user_state(chat_id, user_id)
    if state != "waiting_name":
        logger.warning(
            "Handler poll_name: state mismatch expected=waiting_name got=%s user_id=%s chat_id=%s",
            state, user_id, chat_id
        )
        return ConversationHandler.END

    title = update.message.text
    poll_id = f"{user_id}_{update.message.message_id}"
    logger.debug("Handler poll_name: poll_id=%s title_len=%s", poll_id, len(title) if title else 0)

    try:
        # Send poll
        keyboard = [[
            InlineKeyboardButton("Иду", callback_data=f"vote_{poll_id}_1"),
            InlineKeyboardButton("Пропущу", callback_data=f"vote_{poll_id}_2"),
        ]]
        markup = InlineKeyboardMarkup(keyboard)

        msg = await update.message.reply_text(
            f"✅ Голосование создано!\n\n{title}",
            reply_markup=markup
        )

        await db.create_poll(
            poll_id=poll_id,
            creator_id=user_id,
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            poll_name=title
        )

        await db.clear_user_state(chat_id, user_id)
        return ConversationHandler.END
    except Exception:
        logger.exception("Handler poll_name failed poll_id=%s", poll_id)
        raise


async def handle_group_message(update, context):
    try:
        if update.message.text.startswith('/'):
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        text = update.message.text or ""
        logger.debug(
            "Handler handle_group_message: user_id=%s chat_id=%s text_len=%s text_preview=%r",
            user_id, chat_id, len(text), text[:40],
        )

        state = await db.get_user_state(chat_id, user_id)
        if state == "waiting_name":
            logger.info("Handler handle_group_message: forwarding to poll_name user_id=%s chat_id=%s", user_id, chat_id)
            await poll_name(update, context)
    except Exception:
        logger.exception("Handler handle_group_message failed")
        raise


async def cancel(update, context):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    logger.info("Handler cancel: user_id=%s chat_id=%s", user_id, chat_id)
    try:
        await db.clear_user_state(chat_id, user_id)
        await update.message.reply_text("❌ Создание голосования отменено.")
        return ConversationHandler.END
    except Exception:
        logger.exception("Handler cancel failed")
        raise


# ────────────────────────────────────────────
# VOTE HANDLING
# ────────────────────────────────────────────
async def notify_promoted_users(poll_before, poll_after, context):
    if not poll_before or not poll_after:
        return

    logger.debug(
        "notify_promoted_users: poll_name=%r main_before=%s voters2=%s",
        poll_after.get("poll_name") if poll_after else None,
        len(poll_before.get("voters1", [])) if poll_before else None,
        len(poll_after.get("voters2", [])) if poll_after else None,
    )

    main_before = poll_before["voters1"][:MAIN_PLAYERS_LIMIT]
    reserve_before = poll_before["voters1"][MAIN_PLAYERS_LIMIT:]

    main_after = poll_after["voters1"][:MAIN_PLAYERS_LIMIT]

    main_before_ids = {u["user_id"] for u in main_before}
    reserve_before_map = {
        u["user_id"]: u["user_name"] for u in reserve_before
    }

    poll_name = poll_after["poll_name"]  # название голосования

    for user in main_after:
        uid = user["user_id"]
        if uid not in main_before_ids and uid in reserve_before_map:
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=(
                        "✅ Вы перемещены из списка запаса в основной состав.\n\n"
                        f"Тренировка: «{poll_name}»\n\n"
                        "Теперь вы находитесь в основном списке и можете посетить тренировку."
                    )
                )
            except Exception as e:
                logger.warning(
                    "Cannot notify user %s: %s",
                    uid,
                    e
                )


async def vote_callback(update, context):
    q = update.callback_query
    try:
        logger.info(
            "vote_callback: start q_id=%s from_user_id=%s data=%s chat_id=%s message_id=%s",
            getattr(q, "id", None),
            q.from_user.id if q.from_user else None,
            q.data,
            q.message.chat_id if q.message else None,
            q.message.message_id if q.message else None,
        )

        await q.answer()

        parts = q.data.split('_')
        if len(parts) != 4 or parts[0] != "vote":
            logger.error("vote_callback: bad callback_data=%s parts=%s", q.data, parts)
            await q.answer("Некорректный формат данных кнопки.", show_alert=True)
            return

        _, poll_id_part1, poll_id_part2, choice = parts
        poll_id = f"{poll_id_part1}_{poll_id_part2}"
        logger.debug("vote_callback: parsed poll_id=%s choice=%s", poll_id, choice)

        poll = await db.get_poll(poll_id)
        if not poll:
            logger.warning("vote_callback: poll not found poll_id=%s", poll_id)
            await q.answer("Голосование не найдено!", show_alert=True)
            return

        user_name = (q.from_user.first_name or "").strip()
        if q.from_user.last_name:
            user_name += " " + q.from_user.last_name
        if q.from_user.username:
            user_name += " (@" + q.from_user.username + ")"

        poll_before = await db.get_poll(poll_id)
        await db.add_vote(poll_id, q.from_user.id, user_name, int(choice))
        poll_after = await db.get_poll(poll_id)

        await notify_promoted_users(poll_before, poll_after, context)
        await update_poll_message(poll_id, context)

        logger.info(
            "vote_callback: done poll_id=%s user_id=%s choice=%s",
            poll_id, q.from_user.id, choice
        )
    except Exception:
        logger.exception("vote_callback: failed")
        try:
            await q.answer("Ошибка при обработке голоса.", show_alert=True)
        except Exception:
            pass



async def update_poll_message(poll_id, context):
    poll = await db.get_poll(poll_id)
    if not poll:
        logger.warning("update_poll_message: poll not found poll_id=%s", poll_id)
        return

    keyboard = [[
        InlineKeyboardButton("Иду", callback_data=f"vote_{poll_id}_1"),
        InlineKeyboardButton("Пропущу", callback_data=f"vote_{poll_id}_2"),
    ]]

    markup = InlineKeyboardMarkup(keyboard)
    text = format_poll_message(poll)

    try:
        logger.debug(
            "update_poll_message: edit chat_id=%s message_id=%s poll_id=%s text_len=%s",
            poll["chat_id"], poll["message_id"], poll_id, len(text),
        )
        await context.bot.edit_message_text(
            chat_id=poll["chat_id"],
            message_id=poll["message_id"],
            text=text,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception as e:
        # HTML entity issues are a common reason edit fails; keep traceback for diagnosis.
        logger.exception(
            "update_poll_message: failed poll_id=%s error=%r text_preview=%r",
            poll_id,
            e,
            text[:300] if text else None,
        )


# ────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────

async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logger.exception(
        "Global error handler: update_type=%s error=%r",
        type(update).__name__,
        context.error,
    )


async def on_startup(app):
    logger.info("on_startup: connecting to database")
    await db.connect()
    logger.info("on_startup: Database connected")

def main():
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    logger.info("main: TELEGRAM_BOT_TOKEN set=%s", bool(token))
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    global MAIN_PLAYERS_LIMIT
    MAIN_PLAYERS_LIMIT = int(os.getenv("MAIN_PLAYERS_LIMIT", 12))
    logger.info("MAIN_PLAYERS_LIMIT = %s", MAIN_PLAYERS_LIMIT)

    app = Application.builder().token(token).post_init(on_startup).build()
    app.add_error_handler(on_error)

    conv = ConversationHandler(
        entry_points=[CommandHandler("create", create_poll)],
        states={POLL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, poll_name)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(vote_callback, pattern="^vote_"))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, handle_group_message))

    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
