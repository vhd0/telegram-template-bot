from flask import Flask, request, jsonify
from transformers import MarianMTModel, MarianTokenizer

app = Flask(__name__)

model_name = "Helsinki-NLP/opus-mt-en-ja"
tokenizer = MarianTokenizer.from_pretrained(model_name)
model = MarianMTModel.from_pretrained(model_name)

@app.route("/translate", methods=["POST"])
def translate():
    data = request.json
    text = data.get("text", "")
    tokens = tokenizer(text, return_tensors="pt", padding=True, truncation=True)
    translated = model.generate(**tokens)
    result = tokenizer.decode(translated[0], skip_special_tokens=True)
    return jsonify({"translated": result})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
