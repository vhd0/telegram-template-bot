import os
import logging
import asyncio
import requests # Import thư viện requests để gọi API HTTP
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.error import BadRequest
from langdetect import detect, DetectorFactory
from aiohttp import web # Import aiohttp web framework

# Cấu hình logging chung cho bot
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO # Giữ cấp độ INFO cho các log quan trọng của bot
)
logger = logging.getLogger(__name__)

# Tinh chỉnh cấp độ log cho aiohttp.access để ẩn các log ping healthcheck
# Đặt cấp độ là WARNING hoặc ERROR để chỉ hiển thị các vấn đề nghiêm trọng
logging.getLogger('aiohttp.access').setLevel(logging.WARNING)


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
        "👋 Chào bạn! Mình là bot dịch tự động. Hãy gửi một đoạn văn bất kỳ, mình sẽ tự động phát hiện ngôn ngữ và cho bạn chọn dịch sang Tiếng Việt hoặc Tiếng Nhật. Thật tiện lợi phải không nào! ✨"
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
            "⚠️ Xin lỗi, mình không thể phát hiện ngôn ngữ của đoạn văn này. Vui lòng thử lại với một đoạn văn dài hơn hoặc rõ ràng hơn nhé."
        )
        logger.error(f"Error detecting language: {e} for text: {text}")
        return

    # Lưu trữ văn bản gốc và ngôn ngữ đã phát hiện vào user_data của context
    # Sử dụng message_id làm khóa để truy xuất sau này
    message_id = update.message.message_id
    context.user_data[message_id] = {
        "original_text": text,
        "detected_lang": detected_lang
    }
    logger.info(f"Stored text for message_id {message_id} in user_data.")

    # Tạo bàn phím inline với các tùy chọn ngôn ngữ đích
    keyboard = [
        [
            # Chỉ truyền ngôn ngữ đích và message_id vào callback_data
            InlineKeyboardButton(LANGUAGES[lang_code], callback_data=f"{lang_code}|{message_id}")
            for lang_code in LANGUAGES
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"🔍 Mình phát hiện ngôn ngữ là: **{detected_lang.upper()}**. Bạn muốn dịch sang ngôn ngữ nào? 👇",
        reply_markup=reply_markup,
        parse_mode="Markdown" # Sử dụng Markdown để in đậm ngôn ngữ phát hiện
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý các callback từ các nút inline.
    Thực hiện dịch văn bản bằng Lingva API và gửi kết quả.
    """
    query = update.callback_query
    await query.answer("Đang dịch... ⏳") # Phải gọi query.answer() để bỏ trạng thái "đang tải" trên nút và hiển thị thông báo ngắn

    data = query.data
    # Tách ngôn ngữ đích và message_id từ callback_data
    target_lang, message_id_str = data.split("|", 1) 
    message_id = int(message_id_str)

    # Lấy văn bản gốc và ngôn ngữ đã phát hiện từ user_data
    translation_data = context.user_data.get(message_id)

    if not translation_data:
        await query.edit_message_text(
            "❌ Rất tiếc, không tìm thấy văn bản gốc để dịch. Vui lòng gửi lại tin nhắn mới nhé."
        )
        logger.warning(f"No translation data found for message_id: {message_id}")
        return

    original_text = translation_data["original_text"]
    detected_lang = translation_data["detected_lang"]

    translated_text = ""
    try:
        # Gọi Lingva API
        # Lingva API là một dịch vụ mã nguồn mở, bạn có thể tự host hoặc sử dụng instance công khai.
        # Instance công khai có thể không ổn định hoặc có giới hạn.
        # Ví dụ: https://lingva.ml/
        lingva_api_base_url = "https://lingva.ml" # Hoặc URL instance Lingva của riêng bạn
        api_url = f"{lingva_api_base_url}/api/v1/translate"
        
        params = {
            "q": original_text,
            "source": detected_lang,
            "target": target_lang
        }
        
        response = requests.get(api_url, params=params)
        response.raise_for_status() # Ném lỗi cho các mã trạng thái HTTP xấu (4xx hoặc 5xx)
        
        json_data = response.json()
        
        if json_data and json_data.get("translation"):
            translated_text = json_data["translation"]
        else:
            translated_text = "Rất tiếc, không nhận được bản dịch từ API Lingva."
            logger.warning(f"Lingva API returned no translation for text: {original_text} to {target_lang}")

        if not translated_text: # Xử lý trường hợp dịch trả về chuỗi rỗng
            translated_text = "Rất tiếc, mình không thể dịch đoạn văn này. Vui lòng thử lại sau nhé."
            logger.warning(f"Translation returned empty for text: {original_text} to {target_lang}")

    except requests.exceptions.RequestException as e:
        translated_text = f"Lỗi kết nối API dịch Lingva: {str(e)}. Vui lòng thử lại."
        logger.error(f"Request error during translation with Lingva API: {e} for text: {original_text} to {target_lang}")
    except Exception as e:
        translated_text = f"Đã xảy ra lỗi trong quá trình dịch: {str(e)}. Vui lòng thử lại."
        logger.error(f"Error during translation with Lingva API: {e} for text: {original_text} to {target_lang}")

    # Chỉnh sửa tin nhắn gốc để hiển thị bản dịch
    try:
        await query.edit_message_text(
            f"✅ **Bản dịch ({LANGUAGES[target_lang]}):**\n{translated_text}",
            parse_mode="Markdown"
        )
    except BadRequest as e:
        # Xử lý lỗi BadRequest từ Telegram (ví dụ: nội dung bị chặn)
        logger.error(f"BadRequest error when sending message: {e}")
        await query.edit_message_text(
            "🚫 Xin lỗi, mình không thể gửi bản dịch này. Có thể nội dung vi phạm chính sách của Telegram hoặc có vấn đề về định dạng."
        )
    except Exception as e:
        # Xử lý các lỗi khác khi gửi tin nhắn
        logger.error(f"Unexpected error when sending message: {e}")
        await query.edit_message_text(
            "💥 Đã xảy ra lỗi không mong muốn khi gửi bản dịch. Vui lòng thử lại."
        )
    finally:
        # Xóa dữ liệu đã sử dụng khỏi user_data để giải phóng bộ nhớ
        if message_id in context.user_data:
            del context.user_data[message_id]
            logger.info(f"Cleaned up user_data for message_id: {message_id}")


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
