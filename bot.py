import os
import threading
from flask import Flask, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import requests
from langdetect import detect

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HF_API_TOKEN = os.getenv("HF_API_TOKEN")  # Token HuggingFace API

PORT = int(os.getenv("PORT", 10000))

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set")
if not HF_API_TOKEN:
    raise ValueError("HF_API_TOKEN not set")

headers = {
    "Authorization": f"Bearer {HF_API_TOKEN}"
}

# Map source language to HuggingFace model repo for English and Vietnamese to Japanese
MODEL_MAP = {
    "en": "Helsinki-NLP/opus-mt-en-jap",
    "vi": "Helsinki-NLP/opus-mt-vi-jap"
}

def translate(text: str) -> str:
    lang = detect(text)
    model_id = MODEL_MAP.get(lang, "Helsinki-NLP/opus-mt-en-jap")
    api_url = f"https://api-inference.huggingface.co/models/{model_id}"
    payload = {"inputs": text}
    response = requests.post(api_url, headers=headers, json=payload)
    if response.status_code == 200:
        data = response.json()
        if isinstance(data, list) and "translation_text" in data[0]:
            return data[0]["translation_text"]
        else:
            return "❌ Dịch lỗi - không nhận được kết quả."
    else:
        return f"❌ Lỗi API dịch: {response.status_code}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Xin chào! Gửi câu tiếng Việt hoặc tiếng Anh, tôi sẽ dịch sang tiếng Nhật.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    translated = translate(text)
    await update.message.reply_text(translated)

def run_telegram_bot():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "Bot running"

@flask_app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    threading.Thread(target=run_telegram_bot).start()
    flask_app.run(host="0.0.0.0", port=PORT)
