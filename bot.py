from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
import os
from flask import Flask, request, jsonify 
import asyncio
import pandas as pd 

# --- Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)
application = None # Khai báo 'application' là biến global

WEBHOOK_PATH = "/webhook_telegram"

# --- Bot Data Structure ---
# Đường dẫn đến file Excel
EXCEL_FILE_PATH = "rep.xlsx"
DATA_TABLE = [] # Khởi tạo rỗng, sẽ được điền từ Excel

# --- Tải dữ liệu từ Excel ---
try:
    df = pd.read_excel(EXCEL_FILE_PATH)
    # Kiểm tra các cột bắt buộc
    required_columns = ["Key", "Rep1", "Rep2", "Rep3"]
    if not all(col in df.columns for col in required_columns):
        raise ValueError(f"File Excel phải có các cột: {', '.join(required_columns)}")

    # Chuyển đổi DataFrame thành danh sách các dictionary
    # Đảm bảo tất cả các giá trị được chuyển thành chuỗi để tránh lỗi so khớp
    DATA_TABLE = df.astype(str).to_dict(orient='records')
    logger.info(f"Successfully loaded data from {EXCEL_FILE_PATH}")

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
    initial_keys = set()
    for row in DATA_TABLE:
        initial_keys.add(row["Key"])

    keyboard = []
    for key_val in sorted(list(initial_keys)):
        keyboard.append([InlineKeyboardButton(key_val, callback_data=f"{LEVEL_KEY}:{key_val}::")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if isinstance(update_or_message_object, Update):
        await update_or_message_object.message.reply_text("三上はじめにへようこそ")
        await update_or_message_object.message.reply_text("以下の選択選項からお選びください:", reply_markup=reply_markup) # Sửa lỗi chính tả
    else:
        await update_or_message_object.message.reply_text("以下の選択選項からお選びください:", reply_markup=reply_markup) # Sửa lỗi chính tả


# --- Telegram Bot Handlers ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Phản hồi tin nhắn người dùng, xử lý chào mừng lần đầu."""
    user_id = update.message.from_user.id

    if user_id not in welcomed_users:
        await send_initial_key_buttons(update)
        welcomed_users.add(user_id)
        return
    
    if update.message and update.message.text:
        logger.info(f"Received unexpected text from welcomed user: '{update.message.text}'")
        await update.message.reply_text("何を言っているのか分かりません。ボタンを使用してください。")
    else:
        logger.warning("Received an update without message text from welcomed user: %s", update)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý lệnh /start - sẽ kích hoạt logic chào mừng nếu người dùng chưa được chào mừng."""
    user_id = update.message.from_user.id
    if user_id not in welcomed_users:
        await send_initial_key_buttons(update)
        welcomed_users.add(user_id)
    else:
        await update.message.reply_text("すでにようこそ！")


async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý các truy vấn callback đến từ các nút inline."""
    query = update.callback_query
    
    await query.answer() 

    data_parts = query.data.split(':')
    current_level = data_parts[0]
    selected_key = data_parts[1]
    selected_rep1 = data_parts[2] if len(data_parts) > 2 else ''
    selected_rep2 = data_parts[3] if len(data_parts) > 3 else ''

    logger.info(f"Button press: Level={current_level}, Key={selected_key}, Rep1={selected_rep1}, Rep2={selected_rep2}")

    if current_level == LEVEL_KEY:
        next_rep1_values = set()
        for row in DATA_TABLE:
            if row["Key"] == selected_key:
                next_rep1_values.add(row["Rep1"])
        
        if next_rep1_values:
            keyboard = []
            for rep1_val in sorted(list(next_rep1_values)):
                keyboard.append([InlineKeyboardButton(rep1_val, callback_data=f"{LEVEL_REP1}:{selected_key}:{rep1_val}:")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await query.edit_message_text(text=f"選択されました: {selected_key}\n次に進んでください:", reply_markup=reply_markup)
            except Exception as e:
                logger.warning("Could not edit message for REP1: %s - %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_key}\n以下の選択選項からお選びください:", reply_markup=reply_markup) # Sửa lỗi chính tả
        else:
            try:
                await query.edit_message_text(text=f"選択されました: {selected_key}\n情報が見つかりません。")
            except Exception as e:
                logger.warning("Could not edit message for REP1 (no info): %s - %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_key}\n情報が見つかりません。")

    elif current_level == LEVEL_REP1:
        next_rep2_values = set()
        for row in DATA_TABLE:
            if row["Key"] == selected_key and row["Rep1"] == selected_rep1:
                next_rep2_values.add(row["Rep2"])

        if next_rep2_values:
            keyboard = []
            for rep2_val in sorted(list(next_rep2_values)):
                keyboard.append([InlineKeyboardButton(rep2_val, callback_data=f"{LEVEL_REP2}:{selected_key}:{selected_rep1}:{rep2_val}")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                await query.edit_message_text(text=f"選択されました: {selected_rep1}\n次に進んでください:", reply_markup=reply_markup)
            except Exception as e:
                logger.warning("Could not edit message for REP2: %s - %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_rep1}\n以下の選択選項からお選びください:", reply_markup=reply_markup) # Sửa lỗi chính tả
        else:
            try:
                await query.edit_message_text(text=f"選択されました: {selected_rep1}\n情報が見つかりません。")
            except Exception as e:
                logger.warning("Could not edit message for REP2 (no info): %s - %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_rep1}\n情報が見つかりません。")

    elif current_level == LEVEL_REP2:
        final_text = "情報が見つかりません。"
        for row in DATA_TABLE: 
            if row["Key"] == selected_key and \
               row["Rep1"] == selected_rep1 and \
               row["Rep2"] == selected_rep2:
                final_text = row["Rep3"]
                break
        
        try:
            await query.edit_message_text(text=f"選択されました: {selected_rep2}\n詳細情報:\n{final_text}\nありがとうございました。")
        except Exception as e:
            logger.warning("Could not edit message (final): %s - %s", query.message.message_id, e)
            await query.message.reply_text(f"選択されました: {selected_rep2}\n詳細情報:\n{final_text}\nありがとうございました。")

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

# ĐỊNH NGHĨA ROUTE WEBHOOK BÊN NGOÀI HÀM main()
@flask_app.route(WEBHOOK_PATH, methods=["POST"])
async def telegram_webhook():
    """Xử lý các cập nhật Telegram đến qua webhook."""
    global application 
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
# Loại bỏ async def main() và asyncio.run(main())
# Thay vào đó, chạy trực tiếp application.run_webhook()
if __name__ == '__main__':
    # Khởi tạo application ở đây
    application = ApplicationBuilder().token(os.getenv("BOT_TOKEN")).build()

    # Thêm các handler
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_button_press))

    # Lấy các biến môi trường
    TOKEN = os.getenv("BOT_TOKEN")
    BASE_WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    PORT = int(os.getenv("PORT", 8443)) # Render thường dùng 10000, nhưng 8443 cũng phổ biến

    if not TOKEN:
        logger.critical("BOT_TOKEN environment variable not set. Exiting.")
        raise ValueError("BOT_TOKEN environment variable not set.")
    if not BASE_WEBHOOK_URL:
        logger.critical("WEBHOOK_URL environment variable not set. Exiting.")
        raise ValueError("WEBHOOK_URL environment variable not set.")

    FULL_WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

    # Thiết lập Webhook Telegram (có thể cần await)
    # Tuy nhiên, run_webhook() sẽ tự động gọi set_webhook()
    # Nên chúng ta có thể bỏ qua việc gọi set_webhook() ở đây để tránh trùng lặp
    # và để run_webhook() quản lý hoàn toàn.
    # logger.info("Setting Telegram webhook to: %s", FULL_WEBHOOK_URL)
    # try:
    #     asyncio.run(application.bot.set_webhook(url=FULL_WEBHOOK_URL))
    #     logger.info("Telegram webhook set successfully.")
    # except Exception as e:
    #     logger.error("Error setting Telegram webhook: %s", e)

    # Chạy Telegram Bot Application trong chế độ webhook
    logger.info("Starting Telegram Bot Application in webhook mode.")
    try:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=FULL_WEBHOOK_URL
        )
    except Exception as e:
        logger.critical("Application stopped due to an unhandled error: %s", e)
