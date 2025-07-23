import os
from flask import Flask, request, jsonify
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from langdetect import detect
import requests
from functools import lru_cache

app = Flask(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HF_API_TOKEN = os.getenv("HF_API_TOKEN")

if not TELEGRAM_TOKEN or not HF_API_TOKEN:
    raise ValueError("Thiếu TELEGRAM_TOKEN hoặc HF_API_TOKEN")

bot = Bot(token=TELEGRAM_TOKEN)
HEADERS = {"Authorization": f"Bearer {HF_API_TOKEN}"}

# Chọn model dịch
MODEL_MAP = {
    "en": "Helsinki-NLP/opus-mt-en-ja",
    "vi": "Helsinki-NLP/opus-mt-vi-ja"
}

@lru_cache(maxsize=100)
def translate(text: str, lang: str = "en") -> str:
    model_id = MODEL_MAP.get(lang, "Helsinki-NLP/opus-mt-en-ja")
    api_url = f"https://api-inference.huggingface.co/models/{model_id}"
    payload = {"inputs": text}

    try:
        res = requests.post(api_url, headers=HEADERS, json=payload, timeout=10)
        if res.status_code == 200:
            result = res.json()
            if isinstance(result, list) and "translation_text" in result[0]:
                return result[0]["translation_text"]
            return "⚠️ Không tìm thấy bản dịch phù hợp."
        elif res.status_code == 503:
            return "⏳ Mô hình đang khởi động, vui lòng thử lại sau vài giây."
        else:
            return f"❌ Lỗi dịch ({res.status_code})"
    except Exception as e:
        return f"❌ Lỗi khi gọi API: {str(e)}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Xin chào!\nGửi một câu bằng tiếng Việt hoặc tiếng Anh, tôi sẽ dịch sang tiếng Nhật cho bạn 🇯🇵"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❗️ Tin nhắn trống.")
        return

    try:
        lang = detect(text)
    except:
        lang = "en"

    await update.message.reply_text("⏳ Đang dịch, vui lòng chờ...")

    translated = translate(text, lang)
    await update.message.reply_text(f"🈶 Bản dịch:\n{translated}")

# Setup Telegram app
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

# Flask webhook
@app.route("/webhook_telegram", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    application.process_update(update)
    return jsonify({"ok": True})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route("/", methods=["GET"])
def index():
    return "🤖 Telegram Translation Bot is running!"

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
