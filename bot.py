import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from langdetect import detect, DetectorFactory
from deep_translator import GoogleTranslator
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# Cấu hình logging để xem các thông báo từ bot
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Lấy các biến môi trường. Điều này rất quan trọng khi triển khai trên Render.
# Đảm bảo bạn đã đặt các biến này trong cấu hình môi trường của Render.
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL") # URL công khai của dịch vụ Render của bạn
PORT = int(os.getenv("PORT", "10000")) # Render sẽ cung cấp cổng này, mặc định là 8000

# Ngôn ngữ được hỗ trợ cho các nút inline
LANGUAGES = {
    "vi": "Tiếng Việt",
    "ja": "Tiếng Nhật",
}

# Đặt seed cho langdetect để đảm bảo kết quả nhất quán (tùy chọn)
DetectorFactory.seed = 0

# Handler cho health check
class HealthCheckHandler(BaseHTTPRequestHandler):
    """
    Một HTTP handler đơn giản để phản hồi các yêu cầu kiểm tra sức khỏe (health check)
    từ Render hoặc các dịch vụ khác.
    """
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"Bot is alive!")
        else:
            self.send_response(404)
            self.end_headers()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý lệnh /start. Gửi tin nhắn chào mừng và hướng dẫn sử dụng bot.
    """
    await update.message.reply_text(
        "Chào bạn! Mình là bot dịch tự động. Hãy gửi một đoạn văn bất kỳ, mình sẽ tự động phát hiện ngôn ngữ và cho bạn chọn dịch sang Tiếng Việt hoặc Tiếng Nhật. Thật tiện lợi phải không nào!"
    )

async def translate_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý tin nhắn văn bản từ người dùng.
    Tự động phát hiện ngôn ngữ và hiển thị các nút inline để chọn ngôn ngữ đích.
    """
    text = update.message.text
    
    try:
        detected_lang = detect(text)
        logger.info(f"Detected language: {detected_lang} for text: {text[:50]}...")
    except Exception as e:
        # Xử lý trường hợp không thể phát hiện ngôn ngữ (ví dụ: văn bản quá ngắn hoặc không rõ ràng)
        await update.message.reply_text(
            "Xin lỗi, mình không thể phát hiện ngôn ngữ của đoạn văn này. Vui lòng thử lại với một đoạn văn dài hơn hoặc rõ ràng hơn nhé."
        )
        logger.error(f"Error detecting language: {e} for text: {text}")
        return

    # Tạo bàn phím inline với các tùy chọn ngôn ngữ đích
    keyboard = [
        [
            InlineKeyboardButton(LANGUAGES[lang_code], callback_data=f"{lang_code}|{text}")
            for lang_code in LANGUAGES
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Mình phát hiện ngôn ngữ là: **{detected_lang.upper()}**. Bạn muốn dịch sang ngôn ngữ nào?",
        reply_markup=reply_markup,
        parse_mode="Markdown" # Sử dụng Markdown để in đậm ngôn ngữ phát hiện
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý các callback từ các nút inline.
    Thực hiện dịch văn bản và gửi kết quả.
    """
    query = update.callback_query
    await query.answer("Đang dịch...") # Phải gọi query.answer() để bỏ trạng thái "đang tải" trên nút và hiển thị thông báo ngắn

    data = query.data
    target_lang, original_text = data.split("|", 1) # Tách ngôn ngữ đích và văn bản gốc

    translated_text = ""
    try:
        # Sử dụng GoogleTranslator với nguồn 'auto' để tự động phát hiện ngôn ngữ nguồn
        translator = GoogleTranslator(source='auto', target=target_lang)
        translated_text = translator.translate(original_text)
        
        if not translated_text: # Xử lý trường hợp dịch trả về chuỗi rỗng
            translated_text = "Rất tiếc, mình không thể dịch đoạn văn này. Vui lòng thử lại sau nhé."
            logger.warning(f"Translation returned empty for text: {original_text} to {target_lang}")
    except Exception as e:
        translated_text = f"Đã xảy ra lỗi trong quá trình dịch: {str(e)}. Vui lòng thử lại."
        logger.error(f"Error during translation: {e} for text: {original_text} to {target_lang}")

    # Chỉnh sửa tin nhắn gốc để hiển thị bản dịch
    await query.edit_message_text(
        f"**Bản dịch ({LANGUAGES[target_lang]}):**\n{translated_text}",
        parse_mode="Markdown"
    )

def run_health_check_server():
    """
    Chạy một máy chủ HTTP nhỏ trên cùng cổng để xử lý health check.
    """
    server_address = ('0.0.0.0', PORT)
    httpd = HTTPServer(server_address, HealthCheckHandler)
    logger.info(f"Health check server running on port {PORT}")
    httpd.serve_forever()


def main():
    """
    Hàm chính để khởi tạo và chạy bot.
    Sử dụng webhook cho triển khai trên Render.
    """
    if not TOKEN or not WEBHOOK_URL:
        logger.error("TELEGRAM_BOT_TOKEN hoặc WEBHOOK_URL chưa được đặt trong biến môi trường.")
        print("Lỗi: Vui lòng đặt TELEGRAM_BOT_TOKEN và WEBHOOK_URL trong các biến môi trường.")
        return

    # Khởi chạy máy chủ health check trong một luồng riêng
    health_check_thread = threading.Thread(target=run_health_check_server)
    health_check_thread.daemon = True # Đặt luồng là daemon để nó tự động kết thúc khi chương trình chính kết thúc
    health_check_thread.start()

    # Xây dựng ứng dụng bot với token của bạn
    app = ApplicationBuilder().token(TOKEN).build()

    # Đăng ký các handler cho các lệnh và tin nhắn
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_text))
    app.add_handler(CallbackQueryHandler(button))

    # Cấu hình webhook cho Render
    # url_path thường là TOKEN hoặc một chuỗi ngẫu nhiên để bảo mật webhook endpoint
    webhook_path = TOKEN
    full_webhook_url = f"{WEBHOOK_URL}/{webhook_path}"

    app.run_webhook(
        listen="0.0.0.0", # Lắng nghe trên tất cả các giao diện mạng
        port=PORT,
        url_path=webhook_path,
        webhook_url=full_webhook_url
    )

    logger.info(f"Bot Telegram đang chạy trên webhook: {full_webhook_url} tại cổng {PORT}")
    print(f"Bot Telegram đang chạy trên webhook: {full_webhook_url} tại cổng {PORT}")

if __name__ == "__main__":
    main()
