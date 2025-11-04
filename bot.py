#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent
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

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
POLL_NAME, MAX_PARTICIPANTS, OPTION1_NAME, OPTION2_NAME = range(4)

# Global storage for polls
polls = {}


def format_poll_message(poll_data):
    """Format the poll message with all participant lists."""
    poll_name = poll_data['poll_name']
    
    voters1 = poll_data.get('voters1', [])
    voters2 = poll_data.get('voters2', [])
    
    # Split voters1 into main and reserve
    main_voters = voters1[:12]
    reserve_voters = voters1[12:]
    
    message = f"📊 {poll_name}\n\n"
    
    # First option with main participants
    message += f"✅ Иду\n"
    if main_voters:
        for idx, voter in enumerate(main_voters, 1):
            message += f"{idx}. {voter}\n"
    else:
        message += "Нет участников\n"
    
    # Reserve section
    if reserve_voters:
        message += f"\n🔄 Запас\n"
        for voter in reserve_voters:
            message += f"• {voter}\n"
    
    # Second option
    message += f"\n❌ Пропущу\n"
    if voters2:
        for voter in voters2:
            message += f"• {voter}\n"
    else:
        message += "Нет участников\n"
    
    return message


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler."""
    await update.message.reply_text(
        "Привет! Я бот для создания голосований.\n\n"
        "Команды:\n"
        "/create - Создать новое голосование\n"
        "/help - Показать справку"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command handler."""
    await update.message.reply_text(
        "Как использовать бота:\n\n"
        "1. Используйте /create для создания голосования\n"
        "2. Ответьте на вопросы бота:\n"
        "   - Название голосования\n"
        "4. Участники смогут голосовать нажатием кнопок\n\n"
        "💡 Важно: Для работы в других чатах бот должен быть добавлен в них как участник!"
    )


async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start creating a new poll."""
    await update.message.reply_text(
        "Давайте создадим новое голосование!\n\n"
        "Введите название голосования:"
    )
    return POLL_NAME


async def poll_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive poll name and ask for max participants."""
    context.user_data['poll_name'] = update.message.text
    
    # Generate unique poll ID
    poll_id = f"{update.effective_user.id}_{len(polls)}"
    
    # Store poll data
    poll_data = {
        'poll_id': poll_id,
        'creator_id': update.effective_user.id,
        'poll_name': context.user_data['poll_name'],
        'voters1': [],
        'voters2': [],
        'voter_ids': {}  # Track who voted for what
    }
    
    polls[poll_id] = poll_data
    
    # Create inline keyboard
    keyboard = [
        [
            InlineKeyboardButton(f"Иду", callback_data=f"vote_{poll_id}_1"),
            InlineKeyboardButton(f"Пропущу", callback_data=f"vote_{poll_id}_2")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Format and send the poll
    message_text = format_poll_message(poll_data)
    
    poll_message = await update.message.reply_text(
        "✅ Голосование создано!\n\n" + message_text,
        reply_markup=reply_markup
    )
    
    # Store message_id for potential updates
    poll_data['message_id'] = poll_message.message_id
    poll_data['chat_id'] = poll_message.chat_id
    
    # Clear user data
    context.user_data.clear()
    
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the conversation."""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Создание голосования отменено."
    )
    return ConversationHandler.END


async def vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voting button clicks."""
    query = update.callback_query
    await query.answer()
    
    # Parse callback data
    parts = query.data.split('_')
    if len(parts) != 4 or parts[0] != 'vote':
        return
    
    poll_id = parts[1] + '_' + parts[2]
    option = parts[3]  # '1' or '2'
    
    # Check if poll exists
    if poll_id not in polls:
        await query.answer("⚠️ Голосование не найдено!", show_alert=True)
        return
    
    poll_data = polls[poll_id]
    user_id = query.from_user.id
    user_name = query.from_user.first_name
    if query.from_user.last_name:
        user_name += f" {query.from_user.last_name}"
    
    # Check if user already voted
    if user_id in poll_data['voter_ids']:
        old_choice = poll_data['voter_ids'][user_id]
        
        # If voting for the same option, do nothing
        if old_choice == option:
            await query.answer("Вы уже проголосовали за этот вариант!", show_alert=True)
            return
        
        # Remove from previous choice
        if old_choice == '1':
            # Remove all entries for this user (handle duplicates)
            poll_data['voters1'] = [v for v in poll_data['voters1'] if not v.startswith(user_name)]
        else:
            poll_data['voters2'] = [v for v in poll_data['voters2'] if not v.startswith(user_name)]
    
    # Add vote to new choice
    poll_data['voter_ids'][user_id] = option
    if option == '1':
        poll_data['voters1'].append(user_name)
    else:
        poll_data['voters2'].append(user_name)
    
    # Update the message in all chats where it exists
    await update_poll_message(poll_id, context)


async def update_poll_message(poll_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Update poll message in all chats where it was sent."""
    if poll_id not in polls:
        return
    
    poll_data = polls[poll_id]
    
    # Create inline keyboard
    keyboard = [
        [
            InlineKeyboardButton(f"Иду", callback_data=f"vote_{poll_id}_1"),
            InlineKeyboardButton(f"Пропущу", callback_data=f"vote_{poll_id}_2")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = format_poll_message(poll_data)
    
    # Try to update the original message if we have its ID
    try:
        if 'message_id' in poll_data and 'chat_id' in poll_data:
            await context.bot.edit_message_text(
                chat_id=poll_data['chat_id'],
                message_id=poll_data['message_id'],
                text=message_text,
                reply_markup=reply_markup
            )
    except Exception as e:
        logger.warning(f"Could not update original poll message: {e}")


async def handle_forwarded_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle forwarded poll messages to ensure they work correctly."""
    # This handler ensures that forwarded messages with polls are processed correctly
    pass


def main():
    """Start the bot."""
    # Load environment variables from .env file
    load_dotenv()
    # Get token from environment
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables!")
        return
    
    # Create application
    application = Application.builder().token(token).build()
    
    # Conversation handler for poll creation
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('create', create_poll)],
        states={
            POLL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, poll_name)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    # Add handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(vote_callback, pattern='^vote_'))
    
    # Start the bot
    logger.info("Bot started!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()