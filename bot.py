from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import os
from flask import Flask, request, jsonify
import asyncio

# Khởi tạo Flask app
flask_app = Flask(__name__)

# Định nghĩa đường dẫn webhook cố định
# Đảm bảo rằng WEBHOOK_URL trên Render và khi bạn gọi setWebhook cho Telegram
# sẽ có đường dẫn này ở cuối. Ví dụ: https://your-app.onrender.com/webhook_telegram
WEBHOOK_PATH = "/webhook_telegram"

TEMPLATE_REPLIES = {
    "東京都": "江東区\n江戸川区\n足立区",
    "江東区": "亀戸6-12-7 第2伸光マンション\n亀戸6丁目47-2 ウィンベル亀戸(MONTHLY亀戸1)",
    "江戸川区": "西小岩1丁目30-11",
}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    reply = TEMPLATE_REPLIES.get(text, "何を言っているのか分かりません。もう一度お試しください。")
    await update.message.reply_text(reply)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("三上はじめにへようこそ")

# Endpoint /health cho Render kiểm tra tình trạng ứng dụng
@flask_app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"})

# Hàm chính để khởi động bot và Flask app
async def main():
    TOKEN = os.getenv("BOT_TOKEN")
    BASE_WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    PORT = int(os.getenv("PORT", 8443))

    FULL_WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

    # Khởi tạo Telegram Application
    application = ApplicationBuilder().token(TOKEN).build()

    # KHỞI TẠO ỨNG DỤNG TRƯỚC KHI XỬ LÝ CẬP NHẬT
    # Đây là bước quan trọng để khắc phục lỗi "Application was not initialized"
    await application.initialize()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Setting up webhook for Telegram bot...")
    
    # Hàm xử lý webhook của Telegram thông qua Flask
    @flask_app.route(WEBHOOK_PATH, methods=["POST"])
    async def telegram_webhook():
        if request.method == "POST":
            # Xử lý update từ Telegram
            update = Update.de_json(request.get_json(force=True), application.bot)
            await application.process_update(update)
        return "ok"

    # Đặt webhook cho Telegram bot
    print(f"Setting Telegram webhook to: {FULL_WEBHOOK_URL}")
    
    try:
        # Gọi set_webhook trực tiếp trong ngữ cảnh async của hàm main
        await application.bot.set_webhook(url=FULL_WEBHOOK_URL)
        print("Telegram webhook set successfully.")
    except Exception as e:
        print(f"Error setting Telegram webhook: {e}")

    # Chạy Flask app để lắng nghe các yêu cầu HTTP (bao gồm /health và webhook)
    print(f"Flask app listening on port {PORT}")
    # Sử dụng await để chạy Flask app trong ngữ cảnh async
    # Flask sẽ tự quản lý vòng lặp sự kiện của nó
    # Lưu ý: Flask cần được cài đặt với extra 'async' (Flask[async])
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    await serve(flask_app, config)


if __name__ == '__main__':
    # Chạy hàm main async
    asyncio.run(main())

