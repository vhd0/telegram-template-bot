import os
import asyncio
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)
from aiohttp import web

# ========== Template Replies ==========
TEMPLATE_REPLIES = {
    "東京都": "江東区\n江戸川区\n足立区",
    "江東区": "亀戸6-12-7 第2伸光マンション\n亀戸6丁目47-2 ウィンベル亀戸(MONTHLY亀戸1)",
    "江戸川区": "西小岩1丁目30-11",
}

# ========== Handlers ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    reply = TEMPLATE_REPLIES.get(text, "何を言っているのか分かりません。もう一度お試しください。")
    await update.message.reply_text(reply)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("三上はじめにへようこそ")

# ========== Healthcheck Endpoint ==========
async def healthcheck(request):
    return web.Response(text="OK", status=200)

# ========== Main Entrypoint ==========
async def main():
    TOKEN = os.getenv("BOT_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    PORT = int(os.getenv("PORT", 8443))

    # 1. Tạo Telegram bot application
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 2. Tạo aiohttp server cho /health
    aio_app = web.Application()
    aio_app.router.add_get("/health", healthcheck)

    # 3. Chạy aiohttp site
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    print(f"✅ /health is running on port {PORT}")
    print("🚀 Starting Telegram bot webhook...")

    # 4. Chạy webhook Telegram bot
    await application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        stop_signals=None  # Tránh lỗi signal khi chạy trên Render
    )

if __name__ == '__main__':
    asyncio.run(main())
