import os
import logging
import asyncio

from flask import Flask, request

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from langdetect import detect, LangDetectException
from transformers import MarianMTModel, MarianTokenizer

# --- Cấu hình logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Biến môi trường ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    logger.error("Bạn phải thiết lập biến môi trường TELEGRAM_TOKEN")
    exit(1)

PORT = int(os.getenv("PORT", "8443"))

# --- Khởi tạo Flask app ---
app = Flask(__name__)

# --- Khởi tạo Bot và Application ---
bot = Bot(token=TOKEN)
application = Application.builder().token(TOKEN).build()

# --- Load model dịch ---
logger.info("Đang tải model dịch, vui lòng đợi...")
# Model dịch tiếng Anh sang Tiếng Việt (ví dụ)
model_name_en_vi = "Helsinki-NLP/opus-mt-en-vi"
tokenizer_en_vi = MarianTokenizer.from_pretrained(model_name_en_vi)
model_en_vi = MarianMTModel.from_pretrained(model_name_en_vi)
logger.info("Đã tải model dịch.")

def translate_text(text: str, src_lang: str, tgt_lang: str) -> str:
    try:
        if src_lang == 'en' and tgt_lang == 'vi':
            tokenizer = tokenizer_en_vi
            model = model_en_vi
        else:
            return f"Hiện tại bot chưa hỗ trợ dịch từ {src_lang} sang {tgt_lang}."
        
        inputs = tokenizer(text, return_tensors="pt", padding=True)
        translated = model.generate(**inputs)
        tgt_text = tokenizer.decode(translated[0], skip_special_tokens=True)
        return tgt_text

    except Exception as e:
        logger.error(f"Lỗi khi dịch: {e}")
        return "Lỗi khi dịch văn bản."

# --- Handlers ---

async def start(update: Update, context):
    logger.info(f"User {update.effective_user.id} gọi /start")
    await update.message.reply_text(
        "Chào bạn! Gửi cho tôi một đoạn văn bản tiếng Anh hoặc tiếng Việt, tôi sẽ giúp bạn dịch sang ngôn ngữ còn lại."
    )

async def translate(update: Update, context):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    logger.info(f"Nhận tin nhắn từ user {user_id}: {text}")

    if not text:
        await update.message.reply_text("Vui lòng gửi văn bản hợp lệ để dịch.")
        return

    # Phát hiện ngôn ngữ
    try:
        lang = detect(text)
        logger.info(f"Phát hiện ngôn ngữ: {lang}")
    except LangDetectException:
        logger.warning("Không thể phát hiện ngôn ngữ.")
        await update.message.reply_text("Xin lỗi, tôi không nhận diện được ngôn ngữ của bạn.")
        return

    # Xác định dịch sang ngôn ngữ nào
    if lang.startswith("en"):
        tgt_lang = "vi"
    elif lang.startswith("vi"):
        tgt_lang = "en"
    else:
        await update.message.reply_text("Hiện tại chỉ hỗ trợ dịch giữa tiếng Anh và tiếng Việt.")
        return

    # Dịch
    result = translate_text(text, src_lang=lang[:2], tgt_lang=tgt_lang)
    await update.message.reply_text(result)

# --- Đăng ký handler ---
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), translate))

# --- Flask route webhook ---

@app.route("/webhook_telegram", methods=["POST"])
def webhook():
    try:
        update = Update.de_json(request.get_json(force=True), bot)
        # Chạy xử lý update async trong context sync của Flask
        asyncio.run(application.process_update(update))
    except Exception as e:
        logger.error(f"[Webhook Error] {e}")
    return "ok"

@app.route("/")
def index():
    return "Bot is running."

@app.route("/health")
def health():
    return "OK"

# --- Chạy Flask app ---
if __name__ == "__main__":
    logger.info(f"Starting app on port {PORT}")
    app.run(host="0.0.0.0", port=PORT)
