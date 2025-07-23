import os
import logging
import asyncio
from flask import Flask, request, jsonify
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, ContextTypes, CommandHandler, filters

from transformers import MarianMTModel, MarianTokenizer, pipeline
from langdetect import detect
import torch

# =======================
# CONFIG & INIT
# =======================
TOKEN = os.environ.get("TELEGRAM_TOKEN")  # Set in Render Environment
bot = Bot(token=TOKEN)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# =======================
# LOAD MODEL
# =======================
DEVICE = 0 if torch.cuda.is_available() else -1

model_name = "Helsinki-NLP/opus-mt-ROMANCE-en"
tokenizer = MarianTokenizer.from_pretrained(model_name)
model = MarianMTModel.from_pretrained(model_name)
translator = pipeline("translation", model=model, tokenizer=tokenizer, device=DEVICE)

# =======================
# HANDLERS
# =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text="👋 Xin chào! Gửi cho tôi đoạn văn bản và tôi sẽ dịch sang tiếng Anh!")

async def translate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text.strip()
        lang = detect(text)

        await update.message.reply_text("⏳ Đang dịch, vui lòng chờ...")

        if lang == "en":
            response = "✅ Văn bản đã là tiếng Anh."
        else:
            result = translator(text, max_length=400)[0]["translation_text"]
            response = f"🈶 Bản dịch:\n\n{result}"

        await update.message.reply_text(response)

    except Exception as e:
        logging.error(f"Lỗi dịch: {e}")
        await update.message.reply_text("❌ Xin lỗi, đã có lỗi xảy ra khi dịch.")

# =======================
# APPLICATION
# =======================
application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate))

# =======================
# FLASK ROUTES
# =======================
@app.route("/")
def index():
    return "✅ Bot is running."

@app.route("/health")
def health():
    return jsonify(status="ok")

@app.route("/webhook_telegram", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    asyncio.run(application.process_update(update))
    return jsonify({"ok": True})
