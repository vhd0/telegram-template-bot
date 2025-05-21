from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler # Import CallbackQueryHandler
import os
from flask import Flask, request, jsonify
import asyncio
from hypercorn.asyncio import serve
from hypercorn.config import Config
import logging

# --- Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)

WEBHOOK_PATH = "/webhook_telegram"

# --- Bot Reply Data ---
TEMPLATE_REPLIES = {
    "東京都": ["江東区", "江戸川区", "足立区"],
    "江東区": ["亀戸6-12-7 第2伸光マンション", "亀戸6丁目47-2 ウィンベル亀戸(MONTHLY亀戸1)"],
    "江戸川区": ["西小岩1丁目30-11"],
}

# --- Telegram Bot Handlers ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responds to user messages based on TEMPLATE_REPLIES with inline buttons."""
    if update.message and update.message.text:
        text = update.message.text
        # Chuyển đổi văn bản thành chữ thường để khớp với khóa,
        # nhưng giữ nguyên chữ hoa ban đầu cho phản hồi nếu cần
        replies_key = text.lower()
        replies = TEMPLATE_REPLIES.get(replies_key)

        if replies:
            keyboard = [[InlineKeyboardButton(reply, callback_data=reply)] for reply in replies]
            reply_markup = InlineKeyboardMarkup(keyboard)
            # Thay đổi câu hỏi để rõ ràng hơn khi hiển thị nút
            await update.message.reply_text("以下の選択肢からお選びください:", reply_markup=reply_markup)
        else:
            await update.message.reply_text("何を言っているのか分かりません。もう一度お試しください。")
    else:
        logger.warning("Received an update without message text: %s", update)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    await update.message.reply_text("三上はじめにへようこそ")

# --- New handler for Callback Queries ---
async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming callback queries from inline buttons."""
    query = update.callback_query
    
    # 1. Bắt buộc phải gọi answer() để báo cho Telegram rằng bạn đã nhận được query.
    # Nếu không, nút sẽ ở trạng thái "đang chờ" và gây ra vấn đề.
    # Bạn có thể truyền text='Một tin nhắn tạm thời' để hiển thị pop-up.
    await query.answer() 

    # Lấy dữ liệu từ nút đã nhấn
    data = query.data

    logger.info("Callback query received: %s", data)

    # Lấy phản hồi từ TEMPLATE_REPLIES dựa trên dữ liệu callback
    replies = TEMPLATE_REPLIES.get(data)

    if replies:
        # Tạo bàn phím inline mới từ các phản hồi
        keyboard = [[InlineKeyboardButton(reply, callback_data=reply)] for reply in replies]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # 2. Sử dụng edit_message_text để cập nhật tin nhắn cũ
        # Điều này giúp cuộc trò chuyện gọn gàng hơn vì nó không tạo ra tin nhắn mới liên tục.
        try:
            await query.edit_message_text(text=f"選択されました: {data}\n次に進んでください:", reply_markup=reply_markup)
        except Exception as e:
            # Xử lý trường hợp tin nhắn đã quá cũ hoặc không thể chỉnh sửa
            logger.warning("Could not edit message: %s - %s", query.message.message_id, e)
            await query.message.reply_text(f"選択されました: {data}\n以下の選択肢からお選びください:", reply_markup=reply_markup)
    else:
        # Nếu không có phản hồi nữa (ví dụ: địa chỉ cuối cùng)
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

# --- Main Application Logic ---
async def main():
    """Main function to initialize and run the Telegram bot and Flask server."""
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
    # Add the CallbackQueryHandler AFTER MessageHandler for more specific processing
    application.add_handler(CallbackQueryHandler(handle_button_press))


    # --- Define Flask Webhook Route Dynamically ---
    @flask_app.route(WEBHOOK_PATH, methods=["POST"])
    async def telegram_webhook():
        """Handles incoming Telegram updates via webhook."""
        if request.method == "POST":
            try:
                json_data = request.get_json(force=True)
                if not json_data:
                    logger.warning("Received empty or invalid JSON payload from webhook.")
                    return "Bad Request", 400

                update = Update.de_json(json_data, application.bot)
                await application.process_update(update)
                return "ok", 200
            except Exception as e:
                logger.error("Error processing Telegram update: %s", e)
                return "ok", 200
        return "Method Not Allowed", 405

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
