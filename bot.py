from flask import Flask, request, jsonify
from transformers import MarianMTModel, MarianTokenizer

app = Flask(__name__)

MODEL_NAME = "Helsinki-NLP/opus-mt-en-ja"
tokenizer = MarianTokenizer.from_pretrained(MODEL_NAME)
model = MarianMTModel.from_pretrained(MODEL_NAME)

@app.route("/")
def index():
    return "✅ Translation API is running!"

@app.route("/translate", methods=["POST"])
def translate():
    data = request.get_json()
    if not data or "text" not in data:
        return jsonify({"error": "Missing 'text' in request."}), 400

    text = data["text"]

    # Tokenize và dịch
    inputs = tokenizer(text, return_tensors="pt", padding=True, truncation=True)
    translated_tokens = model.generate(**inputs)
    translated_text = tokenizer.decode(translated_tokens[0], skip_special_tokens=True)

    return jsonify({
        "input": text,
        "translated": translated_text
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
