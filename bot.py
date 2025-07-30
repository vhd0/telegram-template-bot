import os
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Dispatcher, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from deep_translator import GoogleTranslator
from langdetect import detect

app = FastAPI()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

bot = Bot(token=TOKEN)
dp = Dispatcher(bot=bot, update_queue=None, use_context=True)

# Các lựa chọn ngôn ngữ đầu ra
LANG_CHOICES = {
    "vi": "Tiếng Việt",
    "ja": "Tiếng Nhật",
}

def start(update, context):
    update.message.reply_text(
        "Chào bạn! Gửi cho tôi bất kỳ văn bản nào, tôi sẽ tự động phát hiện ngôn ngữ và cho bạn chọn dịch sang Tiếng Việt hoặc Tiếng Nhật."
    )

def translate_text(text, dest_lang):
    try:
        # Dùng deep-translator với GoogleTranslator
        translated = GoogleTranslator(source='auto', target=dest_lang).translate(text)
        return translated
    except Exception as e:
        return f"Lỗi khi dịch: {str(e)}"

def handle_message(update, context):
    text = update.message.text
    try:
        detected_lang = detect(text)
    except Exception:
        detected_lang = "unknown"

    keyboard = [
        [
            InlineKeyboardButton(f"Dịch sang {name}", callback_data=f"translate_{dest}")
            for dest, name in LANG_CHOICES.items()
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    update.message.reply_text(
        f"Phát hiện ngôn ngữ: {detected_lang}\nBạn muốn dịch sang ngôn ngữ nào?",
        reply_markup=reply_markup
    )

    # Lưu text gốc vào context.user_data để callback có thể dùng
    context.user_data["last_text"] = text
    context.user_data["detected_lang"] = detected_lang

def button_handler(update, context):
    query = update.callback_query
    query.answer()
    data = query.data

    if data.startswith("translate_"):
        dest_lang = data.replace("translate_", "")
        original_text = context.user_data.get("last_text", "")
        detected_lang = context.user_data.get("detected_lang", "unknown")

        if not original_text:
            query.edit_message_text("Không tìm thấy văn bản để dịch.")
            return

        if detected_lang == dest_lang:
            query.edit_message_text(f"Văn bản đã là ngôn ngữ {LANG_CHOICES.get(dest_lang)} rồi!")
            return

        translated_text = translate_text(original_text, dest_lang)
        query.edit_message_text(
            f"Ngôn ngữ gốc: {detected_lang}\nDịch sang {LANG_CHOICES.get(dest_lang)}:\n\n{translated_text}"
        )

# Đăng ký handler
dp.add_handler(CommandHandler("start", start))
dp.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
dp.add_handler(CallbackQueryHandler(button_handler))

@app.post(f"/{TOKEN}")
async def telegram_webhook(req: Request):
    update = Update.de_json(await req.json(), bot)
    dp.process_update(update)
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(WEBHOOK_URL + "/" + TOKEN)
