import os
import threading
import logging
from flask import Flask, jsonify
from langdetect import detect
from transformers import MarianMTModel, MarianTokenizer
from huggingface_hub import login
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ==== ENVIRONMENT VARIABLES ====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN")
PORT = int(os.environ.get("PORT", 10000))

if not TELEGRAM_TOKEN:
    raise ValueError("❗ TELEGRAM_TOKEN is not set in environment.")

if HF_TOKEN:
    login(HF_TOKEN)

# ==== LOAD MODELS ====
print("🚀 Loading translation models...")

tokenizer_en = MarianTokenizer.from_pretrained("Helsinki-NLP/opus-mt-en-ROMANCE")
model_en = MarianMTModel.from_pretrained("Helsinki-NLP/opus-mt-en-ROMANCE")

tokenizer_vi = MarianTokenizer.from_pretrained("Helsinki-NLP/opus-mt-vi-ROMANCE")
model_vi = MarianMTModel.from_pretrained("Helsinki-NLP/opus-mt-vi-ROMANCE")

print("✅ Models loaded.")

# ==== TRANSLATION FUNCTION ====
def translate_text(text: str) -> str:
    lang = detect(text)
    print(f"🌍 Detected language: {lang}")

    tgt_lang_token = ">>jpn<<"  # Japanese language token

    if lang == "vi":
        tokenizer, model = tokenizer_vi, model_vi
    else:
        tokenizer, model = tokenizer_en, model_en

    # Append language token
    text_with_lang_token = f"{tgt_lang_token} {text}"
    inputs = tokenizer(text_with_lang_token, return_tensors="pt", padding=True, truncation=True)
    translated = model.generate(**inputs)
    return tokenizer.decode(translated[0], skip_special_tokens=True)

# ==== TELEGRAM BOT HANDLERS ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Xin chào! Gửi tôi câu tiếng Việt hoặc tiếng Anh, tôi sẽ dịch sang tiếng Nhật 🇯🇵.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text
        print(f"📥 Message: {text}")
        translated = translate_text(text)
        await update.message.reply_text(f"🇯🇵 {translated}")
    except Exception as e:
        logging.exception("Translation error:")
        await update.message.reply_text("❌ Đã xảy ra lỗi khi dịch.")

def run_telegram_bot():
    logging.basicConfig(level=logging.INFO)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

# ==== FLASK APP ====
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "✅ Telegram translation bot is running."

@flask_app.route("/health")
def health():
    return jsonify({"status": "ok"})

# ==== MAIN ====
if __name__ == "__main__":
    # Start bot in background thread
    bot_thread = threading.Thread(target=run_telegram_bot)
    bot_thread.start()

    # Start Flask app to keep service alive
    flask_app.run(host="0.0.0.0", port=PORT)
