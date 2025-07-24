import requests
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from flask import Flask, request
import asyncio

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
    print(f"Received /start command from {update.effective_user.id}")
    welcome_text = (
        "Xin chào!\n"
        "Bot này sẽ dịch văn bản bạn gửi sang tiếng Nhật.\n"
        "Bạn chỉ cần gửi tin nhắn, bot sẽ trả về bản dịch.\n"
        "Gõ /help nếu cần trợ giúp."
    )
    await update.message.reply_text(welcome_text)

# Xử lý lệnh /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Received /help command from {update.effective_user.id}")
    help_text = (
        "Hướng dẫn sử dụng:\n"
        "- Gửi bất kỳ văn bản nào, bot sẽ dịch sang tiếng Nhật.\n"
        "- /start để xem lời chào.\n"
        "- /help để xem lại hướng dẫn này."
    )
    await update.message.reply_text(help_text)

# Xử lý tin nhắn văn bản để dịch
async def translate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Received text message from {update.effective_user.id}: '{update.message.text}'")
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Vui lòng nhập văn bản để dịch.")
        return

    # Thông báo đang dịch
    await update.message.chat.send_action(action="typing")

    translated = libre_translate(text, source='auto', target='ja')

    if translated:
        reply_text = f"{translated}"
        print(f"Translated '{text}' to '{translated}'")
    else:
        reply_text = "⚠️ Có lỗi xảy ra khi dịch văn bản, vui lòng thử lại sau."
        print(f"Translation failed for '{text}'")

    await update.message.reply_text(reply_text)

# --- Khởi tạo Flask App ---
flask_app = Flask(__name__)

# Endpoint cho Render Health Check
@flask_app.route('/')
def index():
    return "Bot is running!", 200

# Endpoint cụ thể cho Health Check của Render nếu nó truy cập /health
@flask_app.route('/health')
def health_check():
    return "OK", 200

# Main execution block
if __name__ == "__main__":
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        print("Lỗi: Vui lòng đặt biến môi trường TELEGRAM_BOT_TOKEN")
        exit(1)

    PORT = int(os.environ.get("PORT", "8080")) 
    
    WEBHOOK_URL = os.getenv("WEBHOOK_URL") 
    if not WEBHOOK_URL:
        print("Lỗi: Vui lòng đặt biến môi trường WEBHOOK_URL")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).build()

    # Thêm các handler VÀO ĐÂY (trước khi initialize)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), translate))

    # --- Cấu hình Webhook cho Telegram Bot ---
    telegram_webhook_path = f'/{TOKEN}'
    telegram_webhook_url_full = f'{WEBHOOK_URL}{telegram_webhook_path}'

    async def setup_webhook_and_initialize(): # Đổi tên hàm để rõ ràng hơn
        # KHỞI TẠO APPLICATION TRƯỚC KHI XỬ LÝ BẤT KỲ UPDATE NÀO
        print("Initializing Telegram Application...")
        await app.initialize() # <--- DÒNG MỚI QUAN TRỌNG NHẤT

        print("Thiết lập webhook với Telegram...")
        try:
            await app.bot.set_webhook(url=telegram_webhook_url_full)
            print(f"Webhook đã được thiết lập thành công: {telegram_webhook_url_full}")
        except Exception as e:
            print(f"Lỗi khi thiết lập webhook với Telegram: {e}")
            pass

    # Chạy hàm async để thiết lập webhook và khởi tạo
    asyncio.run(setup_webhook_and_initialize())

    # Flask route để nhận updates từ Telegram
    @flask_app.route(telegram_webhook_path, methods=['POST'])
    async def telegram_update_handler():
        print("Received POST request on Telegram webhook endpoint.")
        if request.json:
            print(f"Request JSON: {request.json}")
            try:
                update = Update.de_json(request.get_json(force=True), app.bot)
                print(f"Successfully parsed Telegram update: {update.update_id}")
                await app.process_update(update)
                print("Finished processing Telegram update.")
                return "ok", 200
            except Exception as e:
                print(f"Error processing Telegram update: {e}")
                return "Error processing update", 500
        else:
            print("Received POST request with no JSON data or invalid JSON.")
            return "Invalid request", 400

    print("Bot đang chạy và sẵn sàng nhận updates...")
    flask_app.run(host="0.0.0.0", port=PORT)
