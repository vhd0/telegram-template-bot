import os
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from flask import Flask, request, make_response

# Khởi tạo ứng dụng Flask để phục vụ endpoint /health
app = Flask(__name__)

# Cấu hình logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Dữ liệu trả lời mẫu (nên được lưu trữ bên ngoài nếu cần mở rộng)
TEMPLATE_REPLIES = {
    "東京都": "江東区\n江戸川区\n足立区",
    "江東区": "亀戸6-12-7 第2伸光マンション\n亀戸6丁目47-2 ウィンベル亀戸(MONTHLY亀戸1)",
    "江戸川区": "西小岩1丁目30-11",
}

# Xử lý tin nhắn từ người dùng
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý tin nhắn văn bản từ người dùng."""
    text = update.message.text.lower()
    reply = TEMPLATE_REPLIES.get(text, "Tôi không hiểu bạn đang nói gì. Vui lòng thử lại.")
    await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
    logger.info(f"Sent reply: {reply} to chat_id: {update.effective_chat.id}")


# Xử lý lệnh /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý lệnh /start."""
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text="Chào mừng đến với 三上はじめに!"
    )
    logger.info(f"Sent start message to chat_id: {update.effective_chat.id}")


# Endpoint /health cho Flask
@app.route("/", methods=["HEAD"])
def health_check():
    """Endpoint kiểm tra sức khỏe cho UptimeRobot."""
    return make_response("", 200)


def run_telegram_bot(token: str, webhook_url: str, port: int):
    """Khởi động bot Telegram."""
    telegram_app = ApplicationBuilder().token(token).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # Thêm error handler để log các lỗi không mong muốn.
    telegram_app.add_error_handler(error_handler)
    try:
        logger.info(f"Setting webhook to {webhook_url}")
        telegram_app.run_webhook(listen="0.0.0.0", port=port, webhook_url=webhook_url)
    except Exception as e:
        logger.error(f"Error setting up webhook: {e}")
        raise  # Re-raise the exception để Flask biết và có thể log hoặc xử lý nếu cần.
    return telegram_app  # Return ứng dụng để có thể dừng nó một cách rõ ràng.


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a broadcast message to the developer(s)."""
    # Log the error before we do anything else, so we have it even if
    # sending the message fails.
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    # Try to send an alert to the user!
    if update and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Rất tiếc, đã có lỗi xảy ra. Vui lòng thử lại sau.",
            )
        except Exception as e:
            logger.warning("Failed to send error message to user", exc_info=e)


def main():
    """Khởi động bot Telegram và Flask."""
    # Lấy token và URL webhook từ biến môi trường
    TOKEN = os.getenv("BOT_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    PORT = int(os.getenv("PORT", 8443))  # Sử dụng 8443 làm mặc định

    # Kiểm tra xem các biến môi trường đã được thiết lập chưa
    if not TOKEN:
        logger.critical("BOT_TOKEN environment variable is missing.")
        return
    if not WEBHOOK_URL:
        logger.critical("WEBHOOK_URL environment variable is missing.")
        return

    # Chạy ứng dụng Flask (trong một thread riêng)
    def run_flask_app():
        logger.info(f"Starting Flask app on port {PORT}")
        app.run(host="0.0.0.0", port=PORT)  # Flask chạy trên cùng một cổng với ứng dụng Telegram

    import threading
    flask_thread = threading.Thread(target=run_flask_app)
    flask_thread.daemon = True  # Flask sẽ tắt khi ứng dụng chính tắt
    flask_thread.start()

    # Khởi động bot Telegram
    telegram_app = run_telegram_bot(TOKEN, WEBHOOK_URL, PORT)

    # Giữ ứng dụng chính chạy để các luồng hoạt động
    import time
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping bot and Flask app...")
        # Dừng bot Telegram một cách rõ ràng
        telegram_app.stop()
        telegram_app.shutdown()  # Shutdown app
        # Không cần dừng явно Flask app, nó sẽ thoát khi luồng chính thoát.
    except Exception as e:
        logger.error(f"An error occurred: {e}")



if __name__ == "__main__":
    main()

