from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from langdetect import detect
from deep_translator import GoogleTranslator

TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"

LANGUAGES = {
    "vi": "Tiếng Việt",
    "ja": "Tiếng Nhật",
}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Chào bạn! Gửi một đoạn văn, mình sẽ tự động phát hiện ngôn ngữ và cho bạn chọn dịch sang Tiếng Việt hoặc Tiếng Nhật."
    )

async def translate_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    detected_lang = detect(text)

    keyboard = [
        [
            InlineKeyboardButton(LANGUAGES[lang_code], callback_data=f"{lang_code}|{text}")
            for lang_code in LANGUAGES
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Phát hiện ngôn ngữ: {detected_lang}. Bạn muốn dịch sang ngôn ngữ nào?",
        reply_markup=reply_markup
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    target_lang, original_text = data.split("|", 1)

    try:
        translated = GoogleTranslator(source='auto', target=target_lang).translate(original_text)
    except Exception as e:
        translated = f"Lỗi dịch: {str(e)}"

    await query.edit_message_text(f"**Bản dịch ({LANGUAGES[target_lang]}):**\n{translated}", parse_mode="Markdown")

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_text))
    app.add_handler(CallbackQueryHandler(button))

    print("Bot đang chạy...")
    app.run_polling()

if __name__ == "__main__":
    main()
