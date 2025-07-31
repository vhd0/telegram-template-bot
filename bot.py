from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from langdetect import detect, DetectorFactory
from deep_translator import GoogleTranslator

# Đặt token bot Telegram của bạn vào đây
TOKEN = "TELEGRAM_BOT_TOKEN"

# Ngôn ngữ được hỗ trợ cho các nút inline
LANGUAGES = {
    "vi": "Tiếng Việt",
    "ja": "Tiếng Nhật",
}

# Đặt seed cho langdetect để đảm bảo kết quả nhất quán (tùy chọn)
DetectorFactory.seed = 0

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý lệnh /start. Gửi tin nhắn chào mừng đến người dùng.
    """
    await update.message.reply_text(
        "Chào bạn! Gửi một đoạn văn, mình sẽ tự động phát hiện ngôn ngữ và cho bạn chọn dịch sang Tiếng Việt hoặc Tiếng Nhật."
    )

async def translate_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý tin nhắn văn bản từ người dùng.
    Tự động phát hiện ngôn ngữ và hiển thị các nút inline để chọn ngôn ngữ đích.
    """
    text = update.message.text
    
    try:
        detected_lang = detect(text)
    except Exception as e:
        # Xử lý trường hợp không thể phát hiện ngôn ngữ (ví dụ: văn bản quá ngắn)
        await update.message.reply_text(
            "Xin lỗi, mình không thể phát hiện ngôn ngữ của đoạn văn này. Vui lòng thử lại với một đoạn văn dài hơn hoặc rõ ràng hơn."
        )
        print(f"Lỗi phát hiện ngôn ngữ: {e}")
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
        f"Phát hiện ngôn ngữ: **{detected_lang.upper()}**. Bạn muốn dịch sang ngôn ngữ nào?",
        reply_markup=reply_markup,
        parse_mode="Markdown" # Sử dụng Markdown để in đậm ngôn ngữ phát hiện
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý các callback từ các nút inline.
    Thực hiện dịch văn bản và gửi kết quả.
    """
    query = update.callback_query
    await query.answer() # Phải gọi query.answer() để bỏ trạng thái "đang tải" trên nút

    data = query.data
    target_lang, original_text = data.split("|", 1) # Tách ngôn ngữ đích và văn bản gốc

    translated_text = ""
    try:
        # Sử dụng GoogleTranslator với nguồn 'auto' để tự động phát hiện ngôn ngữ nguồn
        translator = GoogleTranslator(source='auto', target=target_lang)
        translated_text = translator.translate(original_text)
        if not translated_text: # Xử lý trường hợp dịch trả về chuỗi rỗng
            translated_text = "Không thể dịch đoạn văn này. Vui lòng thử lại."
    except Exception as e:
        translated_text = f"Đã xảy ra lỗi trong quá trình dịch: {str(e)}"
        print(f"Lỗi dịch: {e}")

    # Chỉnh sửa tin nhắn gốc để hiển thị bản dịch
    await query.edit_message_text(
        f"**Bản dịch ({LANGUAGES[target_lang]}):**\n{translated_text}",
        parse_mode="Markdown"
    )

def main():
    """
    Hàm chính để khởi tạo và chạy bot.
    """
    # Xây dựng ứng dụng bot với token của bạn
    app = ApplicationBuilder().token(TOKEN).build()

    # Đăng ký các handler cho các lệnh và tin nhắn
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_text))
    app.add_handler(CallbackQueryHandler(button))

    print("Bot Telegram đang chạy...")
    # Bắt đầu polling để nhận các cập nhật từ Telegram
    app.run_polling()

if __name__ == "__main__":
    main()
