from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
import os
from flask import Flask, request, jsonify
import asyncio
from hypercorn.asyncio import serve
from hypercorn.config import Config
import logging
from pykakasi import kakasi
import pandas as pd

# --- Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)
application = None # Khai báo 'application' là biến global

WEBHOOK_PATH = "/webhook_telegram"

# Initialize Kakasi for text conversion to Kanji
kks = kakasi()
kks.setMode("H", "J") # Convert Hiragana to Kanji
kks.setMode("K", "J") # Convert Katakana to Kanji
kks.setMode("J", "J") # Keep Kanji as is
converter = kks.getConverter()

# --- Utility Function to Normalize Japanese Input to Kanji ---
def normalize_japanese_input(text: str) -> str:
    """Converts Japanese text (Hiragana/Katakana/Kanji) to its Kanji representation for consistent matching."""
    return converter.do(text)

# --- Bot Data Structure ---
# Đường dẫn đến file Excel
EXCEL_FILE_PATH = "rep.xlsx"
DATA_TABLE = [] # Khởi tạo rỗng, sẽ được điền từ Excel
NORMALIZED_DATA_TABLE = [] # Sẽ chứa các giá trị Kanji (đã qua normalize_japanese_input)

# Hàm để chuẩn hóa các khóa 'Key', 'Rep1', 'Rep2' trong DATA_TABLE sang Kanji
# (Mặc dù chúng ta giả định chúng đã là Kanji, nhưng hàm này sẽ đảm bảo tính nhất quán)
def normalize_data_table_keys(table):
    normalized_table = []
    for row in table:
        normalized_row = {}
        for k, v in row.items():
            original_val = str(v) # Đảm bảo giá trị là chuỗi
            if k in ["Key", "Rep1", "Rep2"]:
                # Chuẩn hóa sang Kanji. Nếu đã là Kanji, sẽ giữ nguyên.
                normalized_row[k] = normalize_japanese_input(original_val)
            else:
                normalized_row[k] = original_val # Rep3 là văn bản cuối cùng, không cần chuẩn hóa
        normalized_table.append(normalized_row)
    return normalized_table

# --- Tải dữ liệu từ Excel ---
try:
    df = pd.read_excel(EXCEL_FILE_PATH)
    # Kiểm tra các cột bắt buộc
    required_columns = ["Key", "Rep1", "Rep2", "Rep3"]
    if not all(col in df.columns for col in required_columns):
        raise ValueError(f"File Excel phải có các cột: {', '.join(required_columns)}")

    # Chuyển đổi DataFrame thành danh sách các dictionary
    DATA_TABLE = df.to_dict(orient='records')
    logger.info(f"Successfully loaded data from {EXCEL_FILE_PATH}")

    # Gọi hàm chuẩn hóa dữ liệu
    NORMALIZED_DATA_TABLE = normalize_data_table_keys(DATA_TABLE)
    logger.info("Data table normalized successfully.")

except FileNotFoundError:
    logger.critical(f"Error: {EXCEL_FILE_PATH} not found. Please ensure it's in the root directory.")
    raise SystemExit("Required data file not found. Exiting.")
except ValueError as ve:
    logger.critical(f"Error in Excel file format: {ve}")
    raise SystemExit("Excel file format error. Exiting.")
except Exception as e:
    logger.critical(f"Error loading data from Excel: {e}")
    raise SystemExit("An unexpected error occurred while loading data. Exiting.")


# Định nghĩa các hằng số cấp độ cho callback_data
LEVEL_KEY = "key"
LEVEL_REP1 = "rep1"
LEVEL_REP2 = "rep2"
LEVEL_REP3 = "rep3" # Cấp độ này chỉ ra đây là phản hồi văn bản cuối cùng

# Set để lưu trữ user_id đã được chào mừng (sẽ bị reset khi bot khởi động lại)
welcomed_users = set()

# --- Hàm gửi các nút cấp độ đầu tiên ---
async def send_initial_key_buttons(update_or_message_object):
    """Gửi tin nhắn chào mừng và các nút cấp độ 'Key' ban đầu."""
    # Lấy các giá trị 'Key' đã chuẩn hóa (Kanji) duy nhất để hiển thị và dùng cho callback_data
    initial_keys_normalized = {row["Key"] for row in NORMALIZED_DATA_TABLE}

    keyboard = []
    for key_val_normalized in sorted(list(initial_keys_normalized)):
        # Hiển thị và callback_data đều là giá trị Kanji đã chuẩn hóa
        keyboard.append([InlineKeyboardButton(key_val_normalized, callback_data=f"{LEVEL_KEY}:{key_val_normalized}::")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if isinstance(update_or_message_object, Update):
        await update_or_message_object.message.reply_text("三上はじめにへようこそ")
        await update_or_message_object.message.reply_text("以下の選択肢からお選びください:", reply_markup=reply_markup)
    else:
        await update_or_message_object.edit_message_text(text="以下の選択肢からお選びください:", reply_markup=reply_markup)


# --- Telegram Bot Handlers ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Phản hồi tin nhắn người dùng, xử lý chào mừng lần đầu và tìm kiếm theo văn bản."""
    user_id = update.message.from_user.id

    # Nếu đây là lần đầu tiên người dùng tương tác, gửi tin nhắn chào mừng và nút ban đầu
    if user_id not in welcomed_users:
        await send_initial_key_buttons(update)
        welcomed_users.add(user_id) # Đánh dấu người dùng đã được chào mừng
        return # Dừng xử lý tin nhắn này vì nó là tin nhắn chào mừng

    # Nếu người dùng đã được chào mừng, xử lý đầu vào của họ bình thường
    if update.message and update.message.text:
        user_input_raw = update.message.text
        # Chuẩn hóa đầu vào người dùng sang Kanji để so khớp
        user_input_normalized = normalize_japanese_input(user_input_raw) 
        
        logger.info(f"Received text: '{user_input_raw}', Normalized: '{user_input_normalized}'")

        # Lấy các khóa 'Key' đã chuẩn hóa duy nhất để so sánh
        unique_keys_normalized = {row["Key"] for row in NORMALIZED_DATA_TABLE}

        if user_input_normalized in unique_keys_normalized:
            # Người dùng đã gõ một 'Key' ban đầu. Hiển thị các nút Rep1 cho Key này.
            next_normalized_rep1_values = set()
            for row in NORMALIZED_DATA_TABLE:
                if row["Key"] == user_input_normalized:
                    next_normalized_rep1_values.add(row["Rep1"])
            
            if next_normalized_rep1_values:
                keyboard = []
                for rep1_val_normalized in sorted(list(next_normalized_rep1_values)):
                    # Hiển thị và callback_data đều là giá trị Kanji đã chuẩn hóa
                    keyboard.append([InlineKeyboardButton(rep1_val_normalized, callback_data=f"{LEVEL_REP1}:{user_input_normalized}:{rep1_val_normalized}:")])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(text=f"選択されました: {user_input_normalized}\n次に進んでください:", reply_markup=reply_markup)
            else:
                await update.message.reply_text(text=f"選択されました: {user_input_normalized}\n情報が見つかりません。")
        else:
            # Văn bản nhập vào không khớp với bất kỳ 'Key' nào
            await update.message.reply_text("何を言っているのか分かりません。もう一度お試しください。")
    else:
        logger.warning("Received an update without message text: %s", update)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý lệnh /start - sẽ kích hoạt logic chào mừng nếu người dùng chưa được chào mừng."""
    user_id = update.message.from_user.id
    if user_id not in welcomed_users: # Để tránh chào mừng hai lần nếu /start được gửi sau khi đã chào mừng
        await send_initial_key_buttons(update)
        welcomed_users.add(user_id)
    else:
        await update.message.reply_text("すでにようこそ！") # Tin nhắn đã chào mừng


async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý các truy vấn callback đến từ các nút inline."""
    query = update.callback_query
    
    await query.answer() 

    # Phân tích callback_data: level:key_val:rep1_val:rep2_val
    data_parts = query.data.split(':')
    current_level = data_parts[0]
    selected_key_normalized = data_parts[1] # Đây là Kanji đã chuẩn hóa
    selected_rep1_normalized = data_parts[2] if len(data_parts) > 2 else ''
    selected_rep2_normalized = data_parts[3] if len(data_parts) > 3 else ''

    logger.info(f"Button press: Level={current_level}, Key={selected_key_normalized}, Rep1={selected_rep1_normalized}, Rep2={selected_rep2_normalized}")

    if current_level == LEVEL_KEY:
        next_normalized_rep1_values = set()
        for row in NORMALIZED_DATA_TABLE:
            if row["Key"] == selected_key_normalized:
                next_normalized_rep1_values.add(row["Rep1"])
        
        if next_normalized_rep1_values:
            keyboard = []
            for rep1_val_normalized in sorted(list(next_normalized_rep1_values)):
                # Hiển thị và callback_data đều là giá trị Kanji đã chuẩn hóa
                keyboard.append([InlineKeyboardButton(rep1_val_normalized, callback_data=f"{LEVEL_REP1}:{selected_key_normalized}:{rep1_val_normalized}:")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await query.edit_message_text(text=f"選択されました: {selected_key_normalized}\n次に進んでください:", reply_markup=reply_markup)
            except Exception as e:
                logger.warning("Could not edit message for REP1: %s - %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_key_normalized}\n以下の選択肢からお選びください:", reply_markup=reply_markup)
        else:
            try:
                await query.edit_message_text(text=f"選択されました: {selected_key_normalized}\n情報が見つかりません。")
            except Exception as e:
                logger.warning("Could not edit message for REP1 (no info): %s - %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_key_normalized}\n情報が見つかりません。")

    elif current_level == LEVEL_REP1:
        next_normalized_rep2_values = set()
        for row in NORMALIZED_DATA_TABLE:
            if row["Key"] == selected_key_normalized and row["Rep1"] == selected_rep1_normalized:
                next_normalized_rep2_values.add(row["Rep2"])

        if next_normalized_rep2_values:
            keyboard = []
            for rep2_val_normalized in sorted(list(next_normalized_rep2_values)):
                # Hiển thị và callback_data đều là giá trị Kanji đã chuẩn hóa
                keyboard.append([InlineKeyboardButton(rep2_val_normalized, callback_data=f"{LEVEL_REP2}:{selected_key_normalized}:{selected_rep1_normalized}:{rep2_val_normalized}")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                await query.edit_message_text(text=f"選択されました: {selected_rep1_normalized}\n次に進んでください:", reply_markup=reply_markup)
            except Exception as e:
                logger.warning("Could not edit message for REP2: %s - %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_rep1_normalized}\n以下の選択肢からお選びください:", reply_markup=reply_markup)
        else:
            try:
                await query.edit_message_text(text=f"選択されました: {selected_rep1_normalized}\n情報が見つかりません。")
            except Exception as e:
                logger.warning("Could not edit message for REP2 (no info): %s - %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_rep1_normalized}\n情報が見つかりません。")

    elif current_level == LEVEL_REP2:
        final_text = "情報が見つかりません。"
        # Dùng DATA_TABLE gốc để lấy Rep3 vì nó không được chuẩn hóa
        for row in DATA_TABLE: 
            # Chuẩn hóa các giá trị của hàng để so sánh với các giá trị đã chọn
            # Đảm bảo chuyển đổi sang string trước khi chuẩn hóa
            normalized_row_key = normalize_japanese_input(str(row["Key"]))
            normalized_row_rep1 = normalize_japanese_input(str(row["Rep1"]))
            normalized_row_rep2 = normalize_japanese_input(str(row["Rep2"]))

            if normalized_row_key == selected_key_normalized and \
               normalized_row_rep1 == selected_rep1_normalized and \
               normalized_row_rep2 == selected_rep2_normalized:
                final_text = row["Rep3"]
                break
        
        try:
            await query.edit_message_text(text=f"選択されました: {selected_rep2_normalized}\n詳細情報:\n{final_text}\nありがとうございました。")
        except Exception as e:
            logger.warning("Could not edit message (final): %s - %s", query.message.message_id, e)
            await query.message.reply_text(f"選択されました: {selected_rep2_normalized}\n詳細情報:\n{final_text}\nありがとうございました。")

    else:
        try:
            await query.edit_message_text(text="不明な操作です。")
        except Exception as e:
            logger.warning("Could not edit message (unknown operation): %s - %s", query.message.message_id, e)
            await query.message.reply_text("不明な操作です。")


# --- Flask Endpoints ---
@flask_app.route("/health", methods=["GET"])
def health_check():
    """Endpoint for Render's health checks."""
    return jsonify({"status": "ok"})

# Định nghĩa ROUTE WEBHOOK BÊN NGOÀI HÀM main()
@flask_app.route(WEBHOOK_PATH, methods=["POST"])
async def telegram_webhook():
    """Xử lý các cập nhật Telegram đến qua webhook."""
    global application # Truy cập biến global 'application'
    if application is None:
        logger.error("Telegram Application object not initialized yet.")
        return "Internal Server Error: Bot not ready", 500

    if request.method == "POST":
        try:
            json_data = request.get_json(force=True)
            if not json_data:
                logger.warning("Received empty or invalid JSON payload from webhook.")
                return "Bad Request", 400

            update = Update.de_json(json_data, application.bot)
            await application.process_update(update)
            logger.info("Successfully processed Telegram update.")
            return "ok", 200
        except Exception as e:
            logger.error("Error processing Telegram update: %s", e)
            return "ok", 200
    return "Method Not Allowed", 405


# --- Main Application Logic ---
async def main():
    """Hàm chính để khởi tạo và chạy bot Telegram và server Flask."""
    global application # Gán giá trị cho biến global 'application'

    TOKEN = os.getenv("BOT_TOKEN")
    BASE_WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    PORT = int(os.getenv("PORT", 8443))

    if not TOKEN:
        logger.error("BOT_TOKEN environment variable not set.")
        raise ValueError("BOT_TOKEN environment variable not set.")
    if not BASE_WEBHOOK_URL:
        logger.error("WEBHOOK_URL environment variable not set.")
        raise ValueError("WEBHOOK_URL environment variable not set.")

    FULL_WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

    application = ApplicationBuilder().token(TOKEN).build()

    await application.initialize()

    # Thêm các handler
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_button_press))

    # --- Thiết lập Webhook Telegram ---
    logger.info("Setting Telegram webhook to: %s", FULL_WEBHOOK_URL)
    try:
        await application.bot.set_webhook(url=FULL_WEBHOOK_URL)
        logger.info("Telegram webhook set successfully.")
    except Exception as e:
        logger.error("Error setting Telegram webhook: %s", e)

    # --- Chạy Server Hypercorn ---
    logger.info("Flask app listening on port %d", PORT)
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]

    server_task = asyncio.create_task(serve(flask_app, config))
    await server_task

# --- Điểm vào ---
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical("Application stopped due to an unhandled error: %s", e)
