#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
import asyncpg
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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
        self.pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"))
        await self.create_tables()

    async def create_tables(self):
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

    # USER STATES -----------------------------------------------------------

    async def set_user_state(self, chat_id, user_id, state):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_states(chat_id, user_id, state)
                VALUES($1, $2, $3)
                ON CONFLICT (chat_id, user_id)
                DO UPDATE SET state=EXCLUDED.state;
            """, chat_id, user_id, state)

    async def get_user_state(self, chat_id, user_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT state FROM user_states
                WHERE chat_id=$1 AND user_id=$2
            """, chat_id, user_id)
            return row["state"] if row else None

    async def clear_user_state(self, chat_id, user_id):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM user_states
                WHERE chat_id=$1 AND user_id=$2
            """, chat_id, user_id)

    # POLLS -----------------------------------------------------------------

    async def create_poll(self, poll_id, creator_id, chat_id, message_id, poll_name):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO polls(poll_id, creator_id, chat_id, message_id, poll_name)
                VALUES($1, $2, $3, $4, $5)
            """, poll_id, creator_id, chat_id, message_id, poll_name)

    async def get_poll(self, poll_id):
        async with self.pool.acquire() as conn:
            poll = await conn.fetchrow("SELECT * FROM polls WHERE poll_id=$1", poll_id)
            if not poll:
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

    async def add_vote(self, poll_id, user_id, user_name, choice):
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

    logger.info(" Main: %s\n Reserve: %s\n Voters2: %s\n", main, reserve, voters2)

    msg = f"📊 {poll_name}\n\n"

    msg += "✅ Иду\n"
    if main:
        msg += "\n".join(f"{i+1}. {v['user_name']}" for i, v in enumerate(main)) + "\n"
    else:
        msg += "Нет участников\n"

    if reserve:
        msg += "\n🔄 Запас\n"
        msg += "\n".join(f"• {v['user_name']}" for v in reserve) + "\n"

    msg += "\n❌ Пропущу\n"
    if voters2:
        msg += "\n".join(f"• {v['user_name']}" for v in voters2) + "\n"
    else:
        msg += "Нет участников\n"

    return msg


# ────────────────────────────────────────────
# COMMAND HANDLERS
# ────────────────────────────────────────────

async def start(update, context):
    await update.message.reply_text(
        "Привет! Я бот для создания голосований.\n\n"
        "/create — создать голосование\n"
        "/help — помощь"
    )


async def help_command(update, context):
    await update.message.reply_text(
        "Инструкция по созданию голосования:\n"
        "1) Нажмите /create\n"
        "2) Введите название голосования ответом на сообщение бота"
    )


async def create_poll(update, context):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    await db.set_user_state(chat_id, user_id, "waiting_name")

    await update.message.reply_text(
        "Введите название голосования:"
    )
    return POLL_NAME


async def poll_name(update, context):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    state = await db.get_user_state(chat_id, user_id)
    if state != "waiting_name":
        return ConversationHandler.END

    title = update.message.text
    poll_id = f"{user_id}_{update.message.message_id}"

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


async def handle_group_message(update, context):
    if update.message.text.startswith('/'):
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    state = await db.get_user_state(chat_id, user_id)
    if state == "waiting_name":
        await poll_name(update, context)


async def cancel(update, context):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    await db.clear_user_state(chat_id, user_id)
    await update.message.reply_text("❌ Создание голосования отменено.")
    return ConversationHandler.END


# ────────────────────────────────────────────
# VOTE HANDLING
# ────────────────────────────────────────────
async def notify_promoted_users(poll_before, poll_after, context):
    if not poll_before or not poll_after:
        return

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
    await q.answer()

    _, poll_id_part1, poll_id_part2, choice = q.data.split('_')
    poll_id = f"{poll_id_part1}_{poll_id_part2}"

    poll = await db.get_poll(poll_id)
    if not poll:
        await q.answer("Голосование не найдено!", show_alert=True)
        return

    user_name = q.from_user.first_name
    if q.from_user.last_name:
        user_name += " " + q.from_user.last_name
    if q.from_user.username:
        user_name += " (@" + q.from_user.username + ")"

    poll_before = await db.get_poll(poll_id)

    await db.add_vote(
        poll_id,
        q.from_user.id,
        user_name,
        int(choice)
    )

    poll_after = await db.get_poll(poll_id)

    await notify_promoted_users(
        poll_before,
        poll_after,
        context
    )

    await update_poll_message(poll_id, context)


async def update_poll_message(poll_id, context):
    poll = await db.get_poll(poll_id)
    if not poll:
        return

    keyboard = [[
        InlineKeyboardButton("Иду", callback_data=f"vote_{poll_id}_1"),
        InlineKeyboardButton("Пропущу", callback_data=f"vote_{poll_id}_2"),
    ]]

    markup = InlineKeyboardMarkup(keyboard)
    text = format_poll_message(poll)

    try:
        await context.bot.edit_message_text(
            chat_id=poll["chat_id"],
            message_id=poll["message_id"],
            text=text,
            reply_markup=markup
        )
    except Exception as e:
        logger.warning("Cannot update message: %s", e)


# ────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────

async def on_startup(app):
    await db.connect()
    logger.info("Database connected")

def main():
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    global MAIN_PLAYERS_LIMIT
    MAIN_PLAYERS_LIMIT = int(os.getenv("MAIN_PLAYERS_LIMIT", 12))
    logger.info("MAIN_PLAYERS_LIMIT = %s", MAIN_PLAYERS_LIMIT)

    app = Application.builder().token(token).post_init(on_startup).build()

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
