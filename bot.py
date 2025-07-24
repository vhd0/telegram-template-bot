import requests
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from http.client import HTTPException # Import HTTPException for proper error handling

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

# Main execution block for webhook deployment
if __name__ == "__main__":
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        print("Lỗi: Vui lòng đặt biến môi trường TELEGRAM_BOT_TOKEN")
        exit(1)

    # Render provides PORT environment variable
    PORT = int(os.environ.get("PORT", "8080")) 
    
    # You'll set this in Render as an environment variable
    WEBHOOK_URL = os.getenv("WEBHOOK_URL") 
    if not WEBHOOK_URL:
        print("Lỗi: Vui lòng đặt biến môi trường WEBHOOK_URL")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), translate))

    print("Thiết lập webhook...")
    # Use run_webhook for deployment on platforms like Render
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN, # Use the token as the url_path for security
        webhook_url=f"{WEBHOOK_URL}/{TOKEN}"
    )
    print(f"Bot đang chạy trên cổng {PORT} với webhook_url: {WEBHOOK_URL}/{TOKEN}")
