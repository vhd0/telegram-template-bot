import logging
import os
import asyncio
import pandas as pd

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler

from flask import Flask, request, jsonify
from hypercorn.asyncio import serve
from hypercorn.config import Config

# --- Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)
application = None

WEBHOOK_PATH = "/webhook_telegram"

# --- Bot Data Structure ---
# Đường dẫn đến file Excel
EXCEL_FILE_PATH = "rep.xlsx"
DATA_TABLE = [] # Khởi tạo rỗng, sẽ được điền từ Excel

# Global mappings for shortening callback_data
STRING_TO_ID_MAP = {} # Maps full string to a short integer ID
ID_TO_STRING_MAP = {} # Maps short integer ID back to full string
next_id = 0

def get_or_create_id(text: str) -> int:
    """Assigns a unique short integer ID to a given string."""
    global next_id
    if text not in STRING_TO_ID_MAP:
        STRING_TO_ID_MAP[text] = next_id
        ID_TO_STRING_MAP[next_id] = text
        next_id += 1
    return STRING_TO_ID_MAP[text]

# --- Tải dữ liệu từ Excel ---
try:
    df = pd.read_excel(EXCEL_FILE_PATH)
    # Kiểm tra các cột bắt buộc
    required_columns = ["Key", "Rep1", "Rep2", "Rep3"]
    if not all(col in df.columns for col in required_columns):
        raise ValueError(f"File Excel phải có các cột: {', '.join(required_columns)}")

    # Điền các giá trị NaN bằng chuỗi rỗng trước khi chuyển đổi toàn bộ DataFrame
    # Điều này khắc phục lỗi 'nan' khi đọc từ các ô trống trong Excel.
    df = df.fillna('')
    DATA_TABLE = df.astype(str).to_dict(orient='records')
    logger.info(f"Successfully loaded data from {EXCEL_FILE_PATH}")

    # Populate the ID mappings for all relevant strings in DATA_TABLE
    for row in DATA_TABLE:
        get_or_create_id(row["Key"])
        get_or_create_id(row["Rep1"])
        get_or_create_id(row["Rep2"])
        # Rep3 thường là text cuối cùng, không cần nút cho nó nên không cần tạo ID
    logger.info("String to ID mappings created successfully.")

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

# Thông tin kênh chat và hướng dẫn
TELEGRAM_CHANNEL_LINK = "https://t.me/mikami8186lt"

# Thông điệp chào mừng ban đầu
INITIAL_WELCOME_MESSAGE_JP = "三上はじめにへようこそ。以下の選択肢からお選びください。"

# Thông điệp hướng dẫn cuối cùng
INSTRUCTION_MESSAGE_JP = f"受け取った番号を、到着の10分前までにこちらのチャンネル <a href='{TELEGRAM_CHANNEL_LINK}'>Telegramチャネル</a> に送信してください。よろしくお願いいたします！"

# Thông điệp nhắc nhở khi người dùng gõ text không phải lệnh
UNRECOGNIZED_MESSAGE_JP = "何を言っているのか分かりません。選択肢を始めるか、選択ボードを再起動するには、/start と入力してください。"

# Set để lưu trữ user_id đã được chào mừng (sẽ bị reset khi bot khởi động lại)
welcomed_users = set()

# --- Hàm gửi các nút cấp độ đầu tiên ---
async def send_initial_key_buttons(update_object: Update):
    """Gửi tin nhắn chào mừng và các nút cấp độ 'Key' ban đầu."""
    initial_keys_display = set() # For display text

    for row in DATA_TABLE:
        # Chỉ thêm vào nếu 'Key' không rỗng
        if row["Key"]:
            initial_keys_display.add(row["Key"])

    keyboard = []
    # Sort by display text for consistent order
    for key_val_display in sorted(list(initial_keys_display)):
        key_val_id = get_or_create_id(key_val_display) # Get ID for callback_data
        keyboard.append([InlineKeyboardButton(key_val_display, callback_data=f"{LEVEL_KEY}:{key_val_id}::")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    user_telegram_id = update_object.message.from_user.id
    logger.info(f"Sending initial welcome message to user ID: {user_telegram_id}")
    await update_object.message.reply_text(INITIAL_WELCOME_MESSAGE_JP, reply_markup=reply_markup)


# --- Telegram Bot Handlers ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Phản hồi tin nhắn người dùng, xử lý chào mừng lần đầu."""
    user_telegram_id = update.message.from_user.id

    if user_telegram_id not in welcomed_users: # Logic cho bộ nhớ
        await send_initial_key_buttons(update)
        welcomed_users.add(user_telegram_id) # Logic cho bộ nhớ
        logger.info(f"User {user_telegram_id} welcomed for the first time.")
        return
        
    if update.message and update.message.text:
        logger.info(f"Received unexpected text from welcomed user: '{update.message.text}' (User ID: {user_telegram_id})")
        await update.message.reply_text(UNRECOGNIZED_MESSAGE_JP)
    else:
        logger.warning("Received an update without message text from welcomed user: %s (User ID: %s)", update, user_telegram_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý lệnh /start.
    Luôn gửi lại tin nhắn chào mừng và các nút ban đầu cho người dùng này,
    reset trạng thái "đã chào mừng" trong bộ nhớ.
    """
    user_telegram_id = update.message.from_user.id
    
    # Xóa người dùng khỏi danh sách đã chào mừng (nếu có) để buộc gửi lại tin nhắn chào mừng
    if user_telegram_id in welcomed_users:
        welcomed_users.remove(user_telegram_id)
        logger.info(f"User {user_telegram_id} removed from welcomed_users (session reset by /start).")
    
    await send_initial_key_buttons(update)
    welcomed_users.add(user_telegram_id) # Thêm lại vào bộ nhớ sau khi chào mừng


async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý các truy vấn callback đến từ các nút inline."""
    query = update.callback_query
    
    await query.answer()

    # Phân tích callback_data: level:key_id:rep1_id:rep2_id
    # Đảm bảo các phần tử đủ số lượng, nếu không có thì gán mặc định là rỗng
    data_parts = (query.data.split(':') + ['', '', '', ''])[:4] # Đảm bảo luôn có 4 phần tử
    current_level = data_parts[0]
    selected_key_id = int(data_parts[1]) if data_parts[1] else -1 # Sử dụng -1 hoặc giá trị không hợp lệ
    selected_rep1_id = int(data_parts[2]) if data_parts[2] else -1
    selected_rep2_id = int(data_parts[3]) if data_parts[3] else -1

    # Convert IDs back to original strings for logic and display
    # Sử dụng .get() an toàn hơn để tránh KeyError nếu ID không tồn tại
    selected_key_display = ID_TO_STRING_MAP.get(selected_key_id, f"ID_Key:{selected_key_id}")
    selected_rep1_display = ID_TO_STRING_MAP.get(selected_rep1_id, f"ID_Rep1:{selected_rep1_id}") if selected_rep1_id != -1 else ''
    selected_rep2_display = ID_TO_STRING_MAP.get(selected_rep2_id, f"ID_Rep2:{selected_rep2_id}") if selected_rep2_id != -1 else ''

    logger.info(f"Button press: Level={current_level}, Key_ID={selected_key_id} ({selected_key_display}), Rep1_ID={selected_rep1_id} ({selected_rep1_display}), Rep2_ID={selected_rep2_id} ({selected_rep2_display})")

    if current_level == LEVEL_KEY:
        next_rep1_values_display = set()
        for row in DATA_TABLE:
            # So sánh với ID của Key đã chọn và đảm bảo Rep1 không rỗng
            if get_or_create_id(row["Key"]) == selected_key_id and row["Rep1"]:
                next_rep1_values_display.add(row["Rep1"])
        
        if next_rep1_values_display:
            keyboard = []
            for rep1_val_display in sorted(list(next_rep1_values_display)):
                rep1_val_id = get_or_create_id(rep1_val_display)
                keyboard.append([InlineKeyboardButton(rep1_val_display, callback_data=f"{LEVEL_REP1}:{selected_key_id}:{rep1_val_id}:")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await query.edit_message_text(text=f"選択されました: {selected_key_display}\n次に進んでください:", reply_markup=reply_markup)
            except Exception as e:
                logger.warning("Could not edit message for REP1 (message ID: %s): %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_key_display}\n以下の選択肢からお選びください:", reply_markup=reply_markup)
        else:
            try:
                await query.edit_message_text(text=f"選択されました: {selected_key_display}\n情報が見つかりません。")
            except Exception as e:
                logger.warning("Could not edit message for REP1 (no info, message ID: %s): %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_key_display}\n情報が見つかりません。")

    elif current_level == LEVEL_REP1:
        next_rep2_values_display = set()
        for row in DATA_TABLE:
            # So sánh với ID của Key và Rep1 đã chọn và đảm bảo Rep2 không rỗng
            if get_or_create_id(row["Key"]) == selected_key_id and \
               get_or_create_id(row["Rep1"]) == selected_rep1_id and row["Rep2"]:
                next_rep2_values_display.add(row["Rep2"])

        if next_rep2_values_display:
            keyboard = []
            for rep2_val_display in sorted(list(next_rep2_values_display)):
                rep2_val_id = get_or_create_id(rep2_val_display)
                keyboard.append([InlineKeyboardButton(rep2_val_display, callback_data=f"{LEVEL_REP2}:{selected_key_id}:{selected_rep1_id}:{rep2_val_id}")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                await query.edit_message_text(text=f"選択されました: {selected_rep1_display}\n次に進んでください:", reply_markup=reply_markup)
            except Exception as e:
                logger.warning("Could not edit message for REP2 (message ID: %s): %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_rep1_display}\n以下の選択肢からお選びください:", reply_markup=reply_markup)
        else:
            try:
                await query.edit_message_text(text=f"選択されました: {selected_rep1_display}\n情報が見つかりません。")
            except Exception as e:
                logger.warning("Could not edit message for REP2 (no info, message ID: %s): %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_rep1_display}\n情報が見つかりません。")

    elif current_level == LEVEL_REP2:
        final_text = "情報が見つかりません。"
        for row in DATA_TABLE:
            # So sánh với ID của Key, Rep1, Rep2 đã chọn
            if get_or_create_id(row["Key"]) == selected_key_id and \
               get_or_create_id(row["Rep1"]) == selected_rep1_id and \
               get_or_create_id(row["Rep2"]) == selected_rep2_id:
                final_text = row["Rep3"]
                break
        
        # Thêm hướng dẫn và link vào final_text
        full_response_text = f"{final_text}\n\n{INSTRUCTION_MESSAGE_JP}"

        try:
            # Sử dụng parse_mode='HTML' để link được hiển thị đúng
            await query.edit_message_text(text=full_response_text, parse_mode='HTML')
        except Exception as e:
            logger.warning("Could not edit message (final, message ID: %s): %s", query.message.message_id, e)
            await query.message.reply_text(text=full_response_text, parse_mode='HTML')

    else:
        try:
            await query.edit_message_text(text="不明な操作です。")
        except Exception as e:
            logger.warning("Could not edit message (unknown operation, message ID: %s): %s", query.message.message_id, e)
            await query.message.reply_text("不明な操作です。")


# --- Flask Endpoints ---
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
            # Trả về 200 OK ngay cả khi có lỗi xử lý để Telegram không gửi lại nhiều lần
            return "ok", 200
    return "Method Not Allowed", 405

@flask_app.route("/health", methods=["GET"])
def health_check():
    """Endpoint for Render's health checks."""
    return jsonify({"status": "ok"})


# --- Global Error Handler for Application ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    # Bạn có thể thêm logic để gửi thông báo lỗi đến một admin chat_id cụ thể ở đây
    # if context.bot and update:
    #     try:
    #         # Lấy chat_id của admin từ biến môi trường hoặc cấu hình
    #         admin_chat_id = os.getenv("ADMIN_CHAT_ID")
    #         if admin_chat_id:
    #             await context.bot.send_message(chat_id=admin_chat_id, text=f"Error: {context.error}\nUpdate: {update}")
    #     except Exception as send_error:
    #         logger.error(f"Failed to send error notification: {send_error}")


# --- Main Application Logic (Entry Point) ---
async def run_full_application():
    global application

    TOKEN = os.getenv("BOT_TOKEN")
    BASE_WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    PORT = int(os.getenv("PORT", 8443))

    if not TOKEN:
        logger.critical("BOT_TOKEN environment variable not set. Exiting.")
        raise ValueError("BOT_TOKEN environment variable not set.")
    if not BASE_WEBHOOK_URL:
        logger.critical("WEBHOOK_URL environment variable not set. Exiting.")
        raise ValueError("WEBHOOK_URL environment variable not set.")

    FULL_WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_button_press))
    application.add_error_handler(error_handler)

    await application.initialize()

    logger.info("Setting Telegram webhook to: %s", FULL_WEBHOOK_URL)
    try:
        await application.bot.set_webhook(url=FULL_WEBHOOK_URL)
        logger.info("Telegram webhook set successfully.")
    except Exception as e:
        logger.error("Error setting Telegram webhook: %s", e)
        # Có thể chọn dừng ứng dụng nếu không thể thiết lập webhook
        raise SystemExit("Failed to set webhook. Exiting.")

    logger.info("Flask app (via Hypercorn) listening on port %d", PORT)
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    
    await serve(flask_app, config)


if __name__ == '__main__':
    try:
        asyncio.run(run_full_application())
    except Exception as e:
        logger.critical("Application stopped due to an unhandled error: %s", e)
