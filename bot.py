import os
import requests
from flask import Flask, request, jsonify
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from langdetect import detect

# === Config ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
HF_API_TOKEN = os.getenv("HF_API_TOKEN")

# === Flask App ===
app = Flask(__name__)
bot = Bot(token=TELEGRAM_TOKEN)

# === Telegram Handlers ===
application = Application.builder().token(TELEGRAM_TOKEN).build()


# === Translation Logic ===
def translate_text(text, src, tgt):
    model_id = f"opus-mt-{src}-{tgt}"
    model_url = f"https://api-inference.huggingface.co/models/Helsinki-NLP/{model_id}"
    headers = {
        "Authorization": f"Bearer {HF_API_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {"inputs": text}

    try:
        print(f"[Translation] Requesting model: {model_id}")
        print(f"[Translation] Text: {text}")

        response = requests.post(model_url, headers=headers, json=payload, timeout=15)
        print(f"[Translation] Status Code: {response.status_code}")
        print(f"[Translation] Response: {response.text}")

        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list) and "translation_text" in data[0]:
                return data[0]["translation_text"]
            else:
                print("[Translation] Unexpected response structure.")
        elif response.status_code == 503:
            return "⏳ Mô hình đang khởi động, vui lòng thử lại sau vài giây."
        else:
            print("[Translation] API Error:", response.text)
    except Exception as e:
        print("[Translation] Exception:", str(e))

    return "⚠️ Lỗi khi dịch văn bản."


# === Telegram Bot Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Xin chào! Gửi tôi một đoạn tiếng Anh hoặc tiếng Việt để tôi dịch cho bạn.")


async def translate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    try:
        detected_lang = detect(user_text)
        print(f"[Detect] Text: {user_text} | Detected: {detected_lang}")

        if detected_lang.startswith('en'):
            src, tgt = "en", "vi"
        elif detected_lang.startswith('vi'):
            src, tgt = "vi", "en"
        else:
            await update.message.reply_text("⚠️ Không nhận diện được ngôn ngữ hoặc không hỗ trợ.")
            return

        translated = translate_text(user_text, src, tgt)
        await update.message.reply_text(f"🔁 Dịch ({src} → {tgt}):\n{translated}")

    except Exception as e:
        print("[Translate Message] Error:", e)
        await update.message.reply_text("⚠️ Lỗi khi xử lý văn bản.")


# === Register Telegram Handlers ===
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_message))


# === Flask Routes ===
@app.route("/")
def home():
    return "🤖 Telegram Translation Bot is up."

@app.route("/health")
def health():
    return jsonify(status="ok")

@app.route("/webhook_telegram", methods=["POST"])
def webhook_telegram():
    try:
        update = Update.de_json(request.get_json(force=True), bot)
        print("[Webhook] Incoming update:", update)
        application.create_task(application.process_update(update))
    except Exception as e:
        print("[Webhook Error]", str(e))
    return "OK", 200


# === Main Runner ===
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    PORT = int(os.environ.get("PORT", 10000))
    print(f"Starting Flask app on port {PORT}...")
    app.run(host="0.0.0.0", port=PORT)
