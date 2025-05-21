from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
import os
from flask import Flask, request, jsonify
import asyncio
from hypercorn.asyncio import serve
from hypercorn.config import Config
import logging
from pykakasi import kakasi

# --- Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)
# Khai báo 'application' là biến global, sẽ được khởi tạo trong main()
application = None 

WEBHOOK_PATH = "/webhook_telegram"

# Initialize Kakasi for text conversion
kks = kakasi()
kks.setMode("H", "K") # Convert Hiragana to Katakana
kks.setMode("J", "K") # Convert Kanji to Katakana
kks.setMode("K", "K") # Keep Katakana as is (redundant but explicit)
converter = kks.getConverter()

# --- Bot Reply Data ---
# All keys in TEMPLATE_REPLIES should be in Katakana for consistent lookup
TEMPLATE_REPLIES = {
    "トウキョウト": ["江東区", "江戸川区", "足立区"], # 東京都
    "コウトウク": ["亀戸6-12-7 第2伸光マンション", "亀戸6丁目47-2 ウィンベル亀戸(MONTHLY亀戸1)"], # 江東区
    "エドガワク": ["西小岩1丁目30-11"], # 江戸川区
    "アダチク": ["〇〇区", "△△区"], # Example: "足立区" in Katakana
}

# Set để lưu trữ user_id đã được chào mừng.
# Lưu ý: Sẽ bị reset khi bot khởi động lại.
# Đối với ứng dụng thực tế, bạn sẽ dùng database.
welcomed_users = set()

# --- Utility Function to Normalize Japanese Input ---
def normalize_japanese_input(text: str) -> str:
    """Converts Japanese text to Katakana for consistent matching."""
    return converter.do(text)

# --- Telegram Bot Handlers ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responds to user messages based on TEMPLATE_REPLIES with inline buttons."""
    user_id = update.message.from_user.id

    # Check if user has been welcomed
    if user_id not in welcomed_users:
        await update.message.reply_text("三上はじめにへようこそ") # Japanese welcome message
        # Tạo nút "東京都"
        keyboard = [[InlineKeyboardButton("東京都", callback_data="東京都")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("以下の選択肢からお選びください:", reply_markup=reply_markup)
        welcomed_users.add(user_id) # Add user to welcomed set
        return # Stop processing this message as it's a welcome
    
    # If user is already welcomed, process their input normally
    if update.message and update.message.text:
        user_input_raw = update.message.text
        user_input_normalized = normalize_japanese_input(user_input_raw) 
        
        logger.info(f"Received text: '{user_input_raw}', Normalized: '{user_input_normalized}'")

        replies = TEMPLATE_REPLIES.get(user_input_normalized)

        if replies:
            keyboard = [[InlineKeyboardButton(reply, callback_data=reply)] for reply in replies]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("以下の選択肢からお選びください:", reply_markup=reply_markup)
        else:
            await update.message.reply_text("何を言っているのか分かりません。もう一度お試しください。")
    else:
        logger.warning("Received an update without message text: %s", update)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command - can just trigger the welcome logic."""
    user_id = update.message.from_user.id
    if user_id not in welcomed_users: # To prevent double welcome if /start is sent after welcome
        await update.message.reply_text("三上はじめにへようこそ")
        keyboard = [[InlineKeyboardButton("東京都", callback_data="東京都")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("以下の選択肢からお選びください:", reply_markup=reply_markup)
        welcomed_users.add(user_id)
    else:
        await update.message.reply_text("すでにようこそ！") # Already welcomed message


# --- New handler for Callback Queries ---
async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming callback queries from inline buttons."""
    query = update.callback_query
    
    await query.answer() 

    data = query.data
    data_normalized = normalize_japanese_input(data) # Normalize callback_data as well

    logger.info("Callback query received: %s, Normalized: %s", data, data_normalized)

    replies = TEMPLATE_REPLIES.get(data_normalized)

    if replies:
        keyboard = [[InlineKeyboardButton(reply, callback_data=reply)] for reply in replies]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.edit_message_text(text=f"選択されました: {data}\n次に進んでください:", reply_markup=reply_markup)
        except Exception as e:
            logger.warning("Could not edit message: %s - %s", query.message.message_id, e)
            await query.message.reply_text(f"選択されました: {data}\n以下の選択肢からお選びください:", reply_markup=reply_markup)
    else:
        # If no more replies, it means we've reached a final address/item
        try:
            await query.edit_message_text(text=f"選択されました: {data}\n詳細情報です。ありがとうございました。")
        except Exception as e:
            logger.warning("Could not edit message (final): %s - %s", query.message.message_id, e)
            await query.message.reply_text(f"選択されました: {data}\n詳細情報です。ありがとうございました。")


# --- Flask Endpoints ---
@flask_app.route("/health", methods=["GET"])
def health_check():
    """Endpoint for Render's health checks."""
    return jsonify({"status": "ok"})

# ĐỊNH NGHĨA ROUTE WEBHOOK BÊN NGOÀI HÀM main()
@flask_app.route(WEBHOOK_PATH, methods=["POST"])
async def telegram_webhook():
    """Handles incoming Telegram updates via webhook."""
    global application # Truy cập biến global 'application'
    if application is None:
        logger.error("Telegram Application object not initialized yet.")
        # Trả về lỗi 500 nếu application chưa sẵn sàng, nhưng Telegram vẫn sẽ thử lại
        return "Internal Server Error: Bot not ready", 500

    if request.method == "POST":
        try:
            json_data = request.get_json(force=True)
            if not json_data:
                logger.warning("Received empty or invalid JSON payload from webhook.")
                return "Bad Request", 400

            update = Update.de_json(json_data, application.bot)
            await application.process_update(update)
            logger.info("Successfully processed Telegram update.") # Thêm log thành công
            return "ok", 200 # Luôn trả về 200 OK để Telegram không thử lại liên tục
        except Exception as e:
            logger.error("Error processing Telegram update: %s", e)
            return "ok", 200 # Trả về 200 OK ngay cả khi có lỗi nội bộ
    return "Method Not Allowed", 405 # For non-POST requests


# --- Main Application Logic ---
async def main():
    """Main function to initialize and run the Telegram bot and Flask server."""
    global application # Gán giá trị cho biến global 'application'

    TOKEN = os.getenv("BOT_TOKEN")
    BASE_WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    PORT = int(os.getenv("PORT", 8443))

    if not TOKEN:
        logger.error("BOT_TOKEN environment variable not set.")
        raise ValueError("BOT_TOKEN environment variable not set.")
    if not BASE_WEBHOOK_URL:
        logger.error("WEBHOOK_URL environment variable not set.")
        raise ValueError("WEBHOOK_URL environment variable not set.")

    FULL_WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

    application = ApplicationBuilder().token(TOKEN).build()

    await application.initialize()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_button_press))

    # --- Set Telegram Webhook ---
    logger.info("Setting Telegram webhook to: %s", FULL_WEBHOOK_URL)
    try:
        await application.bot.set_webhook(url=FULL_WEBHOOK_URL)
        logger.info("Telegram webhook set successfully.")
    except Exception as e:
        logger.error("Error setting Telegram webhook: %s", e)

    # --- Run Hypercorn Server ---
    logger.info("Flask app listening on port %d", PORT)
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]

    server_task = asyncio.create_task(serve(flask_app, config))
    await server_task

# --- Entry Point ---
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical("Application stopped due to an unhandled error: %s", e)
