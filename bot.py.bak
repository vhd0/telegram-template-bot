from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import os

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

if __name__ == '__main__':
    from telegram.ext import Application

    # Lấy token và URL từ biến môi trường
    TOKEN = os.getenv("BOT_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # ví dụ: "https://your-domain.com/your-path"

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Starting webhook...")
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 8443)),  # Có thể dùng 443 hoặc 8443 tùy platform
        webhook_url=WEBHOOK_URL,
    )
