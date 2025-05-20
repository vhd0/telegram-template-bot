from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TEMPLATE_REPLIES = {
    "xin chào": "Chào bạn! Tôi có thể giúp gì?",
    "giúp tôi": "Bạn có thể dùng các lệnh như /start, /help nhé.",
    "tạm biệt": "Hẹn gặp lại bạn!",
}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    reply = TEMPLATE_REPLIES.get(text, "Tôi chưa hiểu ý bạn. Hãy thử lại!")
    await update.message.reply_text(reply)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Chào mừng bạn đến với bot trả lời theo mẫu!")

if __name__ == '__main__':
    import os
    TOKEN = os.getenv("BOT_TOKEN")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running...")
    app.run_polling()
