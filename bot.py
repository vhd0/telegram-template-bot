import os
from flask import Flask, request, jsonify
from transformers import MarianMTModel, MarianTokenizer
from langdetect import detect
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
import threading
import logging

# ========== CONFIG ==========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # Đặt biến môi trường trong Render
PORT = int(os.environ.get("PORT", 10000))

# ========== Flask App ==========
app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "message": "Translation bot is alive!"})

@app.route("/")
def home():
    return "✅ Telegram translation bot is running!"

# ========== Load mô hình ==========
model_en = "Helsinki-NLP/opus-mt-en-ja"
model_vi = "Helsinki-NLP/opus-mt-vi-ja"

print("🔁 Loading English model...")
tokenizer_en = MarianTokenizer.from_pretrained(model_en)
model_en_ja = MarianMTModel.from_pretrained(model_en)

print("🔁 Loading Vietnamese model...")
tokenizer_vi = MarianTokenizer.from_pretrained(model_vi)
model_vi_ja = MarianMTModel.from_pretrained(model_vi)

# ========== Dịch tự động ==========
def translate_to_japanese(text: str) -> str:
    lang = detect(text)
    print(f"📘 Detected language: {lang}")

    if lang == "vi":
        tokenizer, model = tokenizer_vi, model_vi_ja
    else:
        tokenizer, model = tokenizer_en, model_en_ja

    inputs = tokenizer(text, return_tensors="pt", padding=True, truncation=True)
    output = model.generate(**inputs)
    translated = tokenizer.decode(output[0], skip_special_tokens=True)
    return translated

# ========== Telegram Bot ==========
async def start(update: Update, context):
    await update.message.reply_text("👋 Xin chào! Gửi mình câu tiếng Việt hoặc tiếng Anh, mình sẽ dịch sang tiếng Nhật 🇯🇵.")

async def handle_message(update: Update, context):
    text = update.message.text
    try:
        translated = translate_to_japanese(text)
        await update.message.reply_text(f"🇯🇵 {translated}")
    except Exception as e:
        await update.message.reply_t_
