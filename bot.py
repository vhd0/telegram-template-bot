from flask import Flask, request
import os
import requests
from langdetect import detect

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

HF_API_TOKEN = os.environ.get("HF_API_TOKEN")  # bạn cần token từ Hugging Face (miễn phí)
HF_API_URL_TEMPLATE = "https://api-inference.huggingface.co/models/Helsinki-NLP/opus-mt-{src}-{tgt}"

app = Flask(__name__)


@app.route("/")
def index():
    return "Telegram Translation Bot is up!"


@app.route("/health")
def health():
    return "OK", 200


@app.route("/webhook_telegram", methods=["POST"])
def webhook():
    data = request.get_json()

    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        user_text = data["message"]["text"]

        try:
            detected_lang = detect(user_text)
        except:
            detected_lang = "unknown"

        if detected_lang == "vi":
            src, tgt = "vi", "en"
        elif detected_lang == "en":
            src, tgt = "en", "vi"
        else:
            send_message(chat_id, "❌ Không nhận diện được ngôn ngữ.")
            return "ok"

        translation = translate_text(user_text, src, tgt)
        if translation:
            send_message(chat_id, f"🈯 Dịch ({src}→{tgt}): {translation}")
        else:
            send_message(chat_id, "⚠️ Không thể dịch nội dung.")

    return "ok"


def translate_text(text, src, tgt):
    headers = {"Authorization": f"Bearer {HF_API_TOKEN}"}
    payload = {"inputs": text}
    model_url = HF_API_URL_TEMPLATE.format(src=src, tgt=tgt)

    try:
        response = requests.post(model_url, headers=headers, json=payload, timeout=10)
        if response.status_code == 200:
            result = response.json()
            return result[0]["translation_text"]
        else:
            print("API error:", response.status_code, response.text)
    except Exception as e:
        print("Translation error:", e)
    return None


def send_message(chat_id, text):
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    requests.post(url, json=payload)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
