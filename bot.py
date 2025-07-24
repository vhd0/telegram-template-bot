import requests
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
# from http.client import HTTPException # This import is not strictly necessary for this error, but keep it if you need it elsewhere

# Hàm gọi LibreTranslate API
def libre_translate(text, source='auto', target='ja'):
    url = "https://libretranslate.de/translate"
    payload = {
        "q": text,
        "source": source,
        "target": target,
        "format": "text"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status() # Raises an HTTPError for bad responses (4xx or 5xx)
        result = response.json()
        return result.get('translatedText')
    except requests.exceptions.RequestException as e: # Catch all requests-related errors
        print(f"[LibreTranslate API error]: {e}")
        return None
    except Exception as e: # Catch any other unexpected errors
        print(f"[General error in libre_translate]: {e}")
        return None

# Xử lý lệnh /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "Xin chào!\n"
        "Bot này sẽ dịch văn bản bạn gửi sang tiếng Nhật.\n"
        "Bạn chỉ cần gửi tin nhắn, bot sẽ trả về bản dịch.\n"
        "Gõ /help nếu cần trợ giúp."
    )
    await update.message.reply_text(welcome_text)

# Xử lý lệnh /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Hướng dẫn sử dụng:\n"
        "- Gửi bất kỳ văn bản nào, bot sẽ dịch sang tiếng Nhật.\n"
        "- /start để xem lời chào.\n"
        "- /help để xem lại hướng dẫn này."
    )
    await update.message.reply_text(help_text)

# Xử lý tin nhắn văn bản để dịch
async def translate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Vui lòng nhập văn bản để dịch.")
        return

    # Thông báo đang dịch
    await update.message.chat.send_action(action="typing")

    translated = libre_translate(text, source='auto', target='ja')

    if translated:
        reply_text = f"{translated}"
    else:
        reply_text = "⚠️ Có lỗi xảy ra khi dịch văn bản, vui lòng thử lại sau."

    await update.message.reply_text(reply_text)

# --- NEW: Health check handler ---
async def health_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responds to Render's health check at the root URL."""
    # This handler is only for HTTP GET requests to the root path
    # It doesn't use update.message because it's not a Telegram message.
    # The webhook server will automatically return a 200 OK for this path.
    # We just need to define a handler for the path '/'
    print("Health check received!")
    # The python-telegram-bot library's webhook server automatically handles
    # the HTTP response for paths registered. We just need to ensure a handler exists.
    # For a simple health check, it might not directly interact with update.message
    # but rather ensures the server is running and responds.
    pass # No explicit action needed for this type of health check within the bot logic


# Main execution block for webhook deployment
if __name__ == "__main__":
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        print("Lỗi: Vui lòng đặt biến môi trường TELEGRAM_BOT_TOKEN")
        exit(1)

    # Render provides PORT environment variable
    PORT = int(os.environ.get("PORT", "10000")) 
    
    # You'll set this in Render as an environment variable
    WEBHOOK_URL = os.getenv("WEBHOOK_URL") 
    if not WEBHOOK_URL:
        print("Lỗi: Vui lòng đặt biến môi trường WEBHOOK_URL")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), translate))
    
    # --- NEW: Add the health check route ---
    # The Telegram bot library's webhook server will handle requests to '/'
    # You don't usually add a MessageHandler for this, but rather configure the webhook server
    # to respond to a specific path for health checks.
    # With python-telegram-bot, the simplest way is to ensure the server starts
    # and handles its internal routes. If Render specifically hits '/', you might need
    # to adjust how run_webhook handles paths or use a more advanced web framework (like Flask/FastAPI)
    # alongside PTB if this isn't enough.

    # However, PTB's run_webhook by default only routes to the TOKEN path.
    # For a health check on '/', you'd typically need to integrate with a small web server.
    # Let's try to explicitly add a dummy handler for the root path using a custom handler.
    # This might require a slightly different approach with PTB's webhook server.

    # A more robust solution for health checks on Render would be to serve
    # a simple HTTP response on the root path *before* starting the PTB webhook.
    # This often involves using a micro-framework like Flask.

    # Let's adjust the `run_webhook` slightly or add a Flask app if needed.
    # For now, let's just make sure the `print` statement for the bot running
    # is the last thing, to confirm it starts.
    
    print("Thiết lập webhook...")
    # Use run_webhook for deployment on platforms like Render
    # The url_path for the Telegram updates is TOKEN. Render's health check hits '/'.
    # PTB's run_webhook expects all requests to come to url_path.
    # This is the core conflict.
    
    # To fix this, you often need a simple HTTP server (like Flask)
    # that wraps your PTB app.

    # Let's try a simple Flask integration.
    # First, add `Flask` to your requirements.txt:
    # python-telegram-bot[webhooks]==20.X
    # requests
    # Flask

    from flask import Flask, request

    # Initialize Flask app
    flask_app = Flask(__name__)

    @flask_app.route('/')
    def index():
        return "Bot is running!", 200

    @flask_app.route(f'/{TOKEN}', methods=['POST'])
    def telegram_webhook():
        # This is where Telegram sends updates
        update = Update.de_json(request.get_json(force=True), app.bot)
        app.process_update(update)
        return "ok", 200

    print("Bot đang chạy...")
    # Run the Flask app
    flask_app.run(host="0.0.0.0", port=PORT)

    # Remove the app.run_webhook() line as Flask is now handling the server.
    # app.run_webhook(
    #     listen="0.0.0.0",
    #     port=PORT,
    #     url_path=TOKEN,
    #     webhook_url=f"{WEBHOOK_URL}/{TOKEN}"
    # )
    print(f"Bot đang chạy trên cổng {PORT} với webhook_url: {WEBHOOK_URL}/{TOKEN}")
