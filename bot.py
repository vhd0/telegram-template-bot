import os
import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from langdetect import detect, DetectorFactory
from deep_translator import GoogleTranslator
from aiohttp import web # Import aiohttp web framework

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
PORT = int(os.getenv("PORT", "8000")) # Render sẽ cung cấp cổng này, mặc định là 8000

# Ngôn ngữ được hỗ trợ cho các nút inline
LANGUAGES = {
    "vi": "Tiếng Việt",
    "ja": "Tiếng Nhật",
}

# Đặt seed cho langdetect để đảm bảo kết quả nhất quán (tùy chọn)
DetectorFactory.seed = 0

# Khởi tạo Telegram Bot Application (không chạy server riêng)
# Chúng ta sẽ sử dụng aiohttp để xử lý HTTP server
ptb_app = ApplicationBuilder().token(TOKEN).build()

# --- Telegram Bot Handlers ---

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

# --- aiohttp Web Server Handlers ---

async def health_check_handler(request):
    """
    Handler cho health check. Trả về "Bot is alive!" khi được truy cập.
    """
    return web.Response(text="Bot is alive!")

async def telegram_webhook_handler(request):
    """
    Handler cho webhook của Telegram. Nhận cập nhật và chuyển cho bot.
    """
    if request.method == "POST":
        update_json = await request.json()
        if not update_json:
            logger.warning("Received empty JSON from webhook.")
            return web.Response(status=400, text="No JSON data")

        try:
            update = Update.de_json(update_json, ptb_app.bot)
            await ptb_app.process_update(update) # Xử lý cập nhật bằng ptb_app
            return web.Response(status=200, text="OK")
        except Exception as e:
            logger.error(f"Error processing update: {e}", exc_info=True)
            return web.Response(status=500, text=f"Error: {e}")
    return web.Response(status=405, text="Method Not Allowed")


async def run_bot():
    """
    Hàm chính bất đồng bộ để khởi tạo và chạy bot.
    """
    if not TOKEN or not WEBHOOK_URL:
        logger.error("TELEGRAM_BOT_TOKEN hoặc WEBHOOK_URL chưa được đặt trong biến môi trường.")
        print("Lỗi: Vui lòng đặt TELEGRAM_BOT_TOKEN và WEBHOOK_URL trong các biến môi trường.")
        return

    # Khởi tạo Telegram Bot Application
    # Các handler được thêm vào ptb_app
    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_text))
    ptb_app.add_handler(CallbackQueryHandler(button))

    # Khởi tạo ptb_app để nó sẵn sàng xử lý các cập nhật
    await ptb_app.initialize()

    # Thiết lập webhook với Telegram
    webhook_path = TOKEN
    full_webhook_url = f"{WEBHOOK_URL}/{webhook_path}"
    await ptb_app.bot.set_webhook(url=full_webhook_url)
    logger.info(f"Webhook set to: {full_webhook_url}")

    # Khởi tạo aiohttp web application
    aio_app = web.Application()
    aio_app.router.add_get('/', health_check_handler) # Health check endpoint cho đường dẫn gốc
    aio_app.router.add_get('/health', health_check_handler) # Thêm health check endpoint cho /health
    aio_app.router.add_post(f'/{webhook_path}', telegram_webhook_handler) # Telegram webhook endpoint

    # Khởi chạy aiohttp web server
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    logger.info(f"Bot Telegram đang chạy trên webhook: {full_webhook_url} tại cổng {PORT}")
    print(f"Bot Telegram đang chạy trên webhook: {full_webhook_url} tại cổng {PORT}")

    # Giữ cho ứng dụng chạy mãi mãi
    await asyncio.Event().wait()


def main():
    """
    Hàm chính để chạy hàm bất đồng bộ run_bot.
    """
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error(f"An unhandled error occurred: {e}", exc_info=True)
    finally:
        # Đảm bảo ptb_app được tắt đúng cách khi thoát
        if ptb_app.running:
            asyncio.run(ptb_app.shutdown())
            asyncio.run(ptb_app.stop())


if __name__ == "__main__":
    main()
