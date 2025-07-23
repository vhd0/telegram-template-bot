import os
import logging
from langdetect import detect
from transformers import MarianMTModel, MarianTokenizer
from huggingface_hub import login
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# === HuggingFace Login (nếu có token) ===
HF_TOKEN = os.getenv("HF_TOKEN")
if HF_TOKEN:
    login(HF_TOKEN)

# === Load mô hình ===
print("🚀 Loading translation models...")
model_en_name = "Helsinki-NLP/opus-mt-en-ja"
model_vi_name = "Helsinki-NLP/opus-mt-vi-ja"

tokenizer_en = MarianTokenizer.from_pretrained(model_en_name)
model_en = MarianMTModel.from_pretrained(model_en_name)

tokenizer_vi = MarianTokenizer.from_pretrained(model_vi_name)
model_vi = MarianMTModel.from_pretrained(model_vi_name)
print("✅ Models loaded.")

# === Hàm dịch tự động ===
def translate_text(text: str) -> str:
    lang = detect(text)
    print(f"📘 Detected language: {lang}")

    if lang == "vi":
        tokenizer, model = tokenizer_vi, model_vi
    else:
        tokenizer, model = tokenizer_en, model_en

    inputs = tokenizer(text, return_tensors="pt", padding=True, truncation=True)
    translated = model.generate(**inputs)
    return tokenizer.decode(translated[0], skip_special_tokens=True)

# === Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Xin chào! Gửi mình câu tiếng Việt hoặc tiếng Anh, mình sẽ dịch sang tiếng Nhật 🇯🇵."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text
        print(f"📩 Received: {text}")
        translated = translate_text(text)
        await update.message.reply_text(f"🇯🇵 {translated}")
    except Exception as e:
        logging.exception("Error during translation:")
        await update.message.reply_text("❌ Lỗi trong quá trình dịch. Vui lòng thử lại.")

# === Main ===
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TELEGRAM_TOKEN:
        raise ValueError("❗ TELEGRAM_TOKEN environment variable is missing.")

    print("🤖 Starting Telegram bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    app.run_polling()
