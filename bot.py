from flask import Flask, request
import os
import requests
from langdetect import detect
from langdetect.lang_detect_exception import LangDetectException

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
HF_API_TOKEN = os.environ.get("HF_API_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
HF_API_URL_TEMPLATE = "https://api-inference.huggingface.co/models/Helsinki-NLP/opus-mt-{src}-{tgt}"

app = Flask(__name__)

@app.route("/")
def index():
    return "Telegram Translation Bot is running!"

@app.route("/health")
def health():
    return "OK", 200

@app.route("/webhook_telegram", methods=["POST"])
def webhook():
    data = request.get_json()
    print("[Webhook received]", data)

    if "message" not in data or "text" not in data["message"]:
        print("No valid message")
        return "ok"

    chat_id = data["message"]["chat"]["id"]
    user_text = data["message"]["text"].strip()

    # Xử lý lệnh /start
    if user_text.lower() == "/start":
        send_message(chat_id, "👋 Chào mừng bạn đến với bot dịch tự động!\nGửi văn bản bằng tiếng Việt hoặc tiếng Anh để được dịch.")
        return "ok"

    # Nhận diện ngôn ngữ
    try:
        detected_lang = detect(user_text)
        print(f"[LangDetect] Text: {user_text} | Detected: {detected_lang}")
    except LangDetectException:
        send_message(chat_id, "⚠️ Không thể nhận diện ngôn ngữ. Vui lòng thử lại.")
        return "ok"

    # Xác định hướng dịch
    if detected_lang == "vi":
        src, tgt = "vi", "en"
    elif detected_lang == "en":
        src, tgt = "en", "vi"
    else:
        send_message(chat_id, f"⚠️ Ngôn ngữ không được hỗ trợ: `{detected_lang}`.")
        return "ok"

    # Dịch văn bản
    translation = translate_text(user_text, src, tgt)
    if translation:
        send_message(chat_id, f"📤 Dịch ({src} → {tgt}):\n{translation}")
    else:
        send_message(chat_id, "⚠️ Lỗi khi dịch văn bản.")

    return "ok"


def translate_text(text, src, tgt):
    headers = {"Authorization": f"Bearer {HF_API_TOKEN}"}
    payload = {"inputs": text}
    model_url = HF_API_URL_TEMPLATE.format(src=src, tgt=tgt)

    try:
        print(f"[Translation API] Requesting: {model_url} | Text: {text}")
        response = requests.post(model_url, headers=headers, json=payload, timeout=10)
        print("[Translation API] Status:", response.status_code)
        print("[Translation API] Response:", response.text)

        if response.status_code == 200:
            result = response.json()
            if isinstance(result, list) and len(result) > 0 and "translation_text" in result[0]:
                return result[0]["translation_text"]
        else:
            print("Translation API Error:", response.text)
    except Exception as e:
        print("Exception during translation:", str(e))

    return None


def send_message(chat_id, text):
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        r = requests.post(url, json=payload)
        print("[SendMessage] Status:", r.status_code)
        print("[SendMessage] Response:", r.text)
    except Exception as e:
        print("[SendMessage] Exception:", str(e))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
