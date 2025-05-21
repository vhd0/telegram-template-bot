from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import os
from flask import Flask, request, jsonify
import asyncio # Thêm asyncio

# Khởi tạo Flask app
flask_app = Flask(__name__)

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

if __name__ == '__main__':
    TOKEN = os.getenv("BOT_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    PORT = int(os.getenv("PORT", 8443))

    # Khởi tạo Telegram Application
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Setting up webhook for Telegram bot...")
    
    # Hàm xử lý webhook của Telegram thông qua Flask
    @flask_app.route(f"/{WEBHOOK_URL.split('/')[-1]}", methods=["POST"])
    async def telegram_webhook():
        if request.method == "POST":
            # Xử lý update từ Telegram
            update = Update.de_json(request.get_json(force=True), application.bot)
            await application.process_update(update)
        return "ok"

    # Đặt webhook cho Telegram bot
    # Đây là bước quan trọng để Telegram biết gửi update về đâu
    print(f"Setting Telegram webhook to: {WEBHOOK_URL}")
    
    # Sử dụng asyncio.run để chạy hàm async set_webhook() một lần
    try:
        asyncio.run(application.bot.set_webhook(url=WEBHOOK_URL))
        print("Telegram webhook set successfully.")
    except Exception as e:
        print(f"Error setting Telegram webhook: {e}")

    # Chạy Flask app để lắng nghe các yêu cầu HTTP (bao gồm /health và webhook)
    print(f"Flask app listening on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT)
