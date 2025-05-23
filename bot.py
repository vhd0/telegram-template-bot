import logging
import os
import asyncio
import pandas as pd
import time
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Set, Optional
from functools import lru_cache
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    ContextTypes, filters, CallbackQueryHandler
)
from flask import Flask, request, jsonify
from hypercorn.asyncio import serve
from hypercorn.config import Config
from asgiref.sync import async_to_sync
import contextvars
import functools

# ... (giữ nguyên phần Settings và Messages) ...

class State:
    def __init__(self):
        self.data: List[dict] = []
        self.string_ids: Dict[str, int] = {}
        self.id_strings: Dict[int, str] = {}
        self.next_id: int = 0
        self.welcomed_users: Set[int] = set()
        self.last_refresh: float = 0
        self._requests: Dict[int, List[float]] = defaultdict(list)
        self.processing: Dict[int, bool] = {}
        self.loop = None

    def set_loop(self, loop):
        self.loop = loop

    async def run_async(self, func, *args, **kwargs):
        """Run async function with proper event loop handling"""
        if self.loop and self.loop.is_running():
            return await func(*args, **kwargs)
        else:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return await func(*args, **kwargs)
            finally:
                loop.close()

    # ... (giữ nguyên các method khác) ...

settings = Settings()
logger = logging.getLogger(__name__)
flask_app = Flask(__name__)
application = None
state = State()

# ... (giữ nguyên các function load_excel_data và refresh_data) ...

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    
    if not state.can_request(user_id) or state.processing.get(user_id):
        await query.answer(MESSAGES["processing"])
        return

    try:
        state.processing[user_id] = True
        await query.answer()
        refresh_data()

        level, *ids = query.data.split(':')
        selected_ids = [int(id_) if id_ else -1 for id_ in ids]
        key_id, rep1_id, rep2_id = selected_ids + [-1] * (3 - len(selected_ids))

        key = state.get_string(key_id)
        rep1 = state.get_string(rep1_id) if rep1_id != -1 else ''
        rep2 = state.get_string(rep2_id) if rep2_id != -1 else ''

        async def process_button():
            if level == "key":
                next_rep1 = {row["Rep1"] for row in state.data if row["Key"] == key and row["Rep1"]}
                if next_rep1:
                    keyboard = [[InlineKeyboardButton(r1, callback_data=f"rep1:{key_id}:{state.get_id(r1)}:")] 
                              for r1 in sorted(next_rep1)]
                    await query.edit_message_text(
                        f"{MESSAGES['selected'].format(key)}\n{MESSAGES['next_step']}",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
            
            elif level == "rep1":
                next_rep2 = {row["Rep2"] for row in state.data 
                            if row["Key"] == key and row["Rep1"] == rep1 and row["Rep2"]}
                if next_rep2:
                    keyboard = [[InlineKeyboardButton(r2, callback_data=f"rep2:{key_id}:{rep1_id}:{state.get_id(r2)}")] 
                              for r2 in sorted(next_rep2)]
                    await query.edit_message_text(
                        f"{MESSAGES['selected'].format(rep1)}\n{MESSAGES['next_step']}",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
            
            elif level == "rep2":
                rep3 = next((row["Rep3"] for row in state.data 
                            if row["Key"] == key and row["Rep1"] == rep1 and row["Rep2"] == rep2),
                           MESSAGES["no_data"])
                await query.edit_message_text(MESSAGES["number"].format(rep3))
                await query.message.reply_text(
                    f"{MESSAGES['instruction']}\n\n{MESSAGES['wait_time']}",
                    parse_mode='HTML'
                )

        await state.run_async(process_button)

    except Exception as e:
        logger.error(f"Button handler error: {e}")
        try:
            await query.message.reply_text(MESSAGES["error"])
        except Exception:
            pass
    finally:
        state.processing[user_id] = False

@flask_app.route(settings.WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    """Handle Telegram webhook updates"""
    if not application:
        return "Bot not ready", 503

    try:
        if not (data := request.get_json(force=True)):
            return "Empty request", 400

        async def process_update():
            update = Update.de_json(data, application.bot)
            await application.process_update(update)

        asyncio.run(process_update())
        return "ok", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "ok", 200

async def init_application():
    """Initialize application"""
    global application
    
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.DEBUG if settings.DEBUG else logging.INFO
    )

    try:
        application = (
            ApplicationBuilder()
            .token(settings.BOT_TOKEN)
            .build()
        )

        application.add_handler(CommandHandler("start", handle_start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                            lambda u, c: u.message.reply_text(MESSAGES["welcome"])))
        application.add_handler(CallbackQueryHandler(handle_button))

        await application.initialize()
        await application.bot.set_webhook(url=f"{settings.WEBHOOK_URL}{settings.WEBHOOK_PATH}")
        
        # Store the event loop
        state.set_loop(asyncio.get_event_loop())
        
        refresh_data()
        return True

    except Exception as e:
        logger.critical(f"Initialization error: {e}")
        return False

async def run_application():
    """Run the application"""
    try:
        if await init_application():
            config = Config()
            config.bind = [f"0.0.0.0:{settings.PORT}"]
            await serve(flask_app, config)
        else:
            raise RuntimeError("Application initialization failed")
    except Exception as e:
        logger.critical(f"Startup error: {e}")
        raise

if __name__ == '__main__':
    try:
        asyncio.run(run_application())
    except KeyboardInterrupt:
        logger.info("Shutdown by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        raise
