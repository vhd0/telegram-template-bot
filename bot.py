import os
from flask import Flask, request, jsonify
from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, filters
from langdetect import detect
import requests

app = Flask(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HF_API_TOKEN = os.getenv("HF_API_TOKEN")

if not TELEGRAM_TOKEN or not HF_API_TOKEN:
    raise ValueError("Missing TELEGRAM_TOKEN or HF_API_TOKEN environment variable")

bot = Bot(token=TELEGRAM_TOKEN)
dispatcher = Dispatcher(bot, None, workers=0, use_context=True)

HEADERS = {"Authorization": f"Bearer {HF_API_TOKEN}"}

# Map ngôn ngữ phát hiện sang model Huggingface tương ứng
MODEL_MAP = {
    "en": "Helsinki-NLP/opus-mt-en-jap",
    "vi": "Helsinki-NLP/opus-mt-vi-jap"
}

def translate(text):
    try:
        lang = detect(text)
    except:
        lang = "en"  # Mặc định nếu detect lỗi

    model_id = MODEL_MAP.get(lang, "Helsinki-NLP/opus-mt-en-jap")
    api_url = f"https://api-inference.huggingface.co/models/{model_id}"

    payload = {"inputs": text}
    response = requests.post(api_url, headers=HEADERS, json=payload, timeout=10)

    if response.status_code == 200:
        data = response.json()
        if isinstance(data, list) and "translation_text" in data[0]:
            return data[0]["translation_text"]
        else:
            return "❌ Không thể dịch, lỗi dữ liệu trả về."
    else:
        return f"❌ Lỗi API dịch: {response.status_code}"

def start(update, context):
    update.message.reply_text(
        "Chào bạn! Gửi câu tiếng Việt hoặc tiếng Anh, tôi sẽ dịch sang tiếng Nhật."
    )

def handle_message(update, context):
    text = update.message.text
    translated = translate(text)
    update.message.reply_text(translated)

# Đăng ký handler
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

@app.route("/webhook_telegram", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return jsonify({"ok": True})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
