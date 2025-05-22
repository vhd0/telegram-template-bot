import logging
import os
import asyncio
import pandas as pd

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler

from flask import Flask, request, jsonify
from hypercorn.asyncio import serve
from hypercorn.config import Config

# --- Cấu hình chung ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)
application = None

# --- Biến môi trường và hằng số ---
# Lấy từ biến môi trường
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8443))
WEBHOOK_PATH = "/webhook_telegram"
FULL_WEBHOOK_URL = f"{WEBHOOK_URL}{WEBHOOK_PATH}"

# Cấu hình dữ liệu Excel
EXCEL_FILE_PATH = "rep.xlsx"
DATA_TABLE = []  # Sẽ được điền từ Excel

# Mapping để rút gọn callback_data
STRING_TO_ID_MAP = {}
ID_TO_STRING_MAP = {}
next_id = 0

# Định nghĩa các hằng số cấp độ cho callback_data
LEVEL_KEY = "key"
LEVEL_REP1 = "rep1"
LEVEL_REP2 = "rep2"

# Thông tin kênh chat và hướng dẫn (Tiếng Nhật)
TELEGRAM_CHANNEL_LINK = "https://t.me/mikami8186lt"
INITIAL_WELCOME_MESSAGE_JP = "三上はじめにへようこそ。以下の選択肢からお選びください。\n\n**ボタンを押した後、処理のためしばらくお待ちください。数秒経っても変化がない場合は、再度ボタンをタップしてください。ありがとうございます。**"
PROCESSING_WAIT_MESSAGE_JP = "ボタンを押した後、処理のためしばらくお待ちください。数秒経っても変化がない場合は、再度ボタンをタップしてください。ありがとうございます。" # Thêm dòng này
INSTRUCTION_MESSAGE_JP = f"受け取った番号を、到着の10分前までにこちらのチャンネル <a href='{TELEGRAM_CHANNEL_LINK}'>Telegramチャネル</a> に送信してください。よろしくお願いいたします！"
WAIT_TIME_MESSAGE_JP = "通常、5分以内に部屋番号をお知らせしますが、担当者が忙しい場合、30分以上お待ちいただくこともございます。恐れ入りますが、しばらくお待ちください。"
UNRECOGNIZED_MESSAGE_JP = "何を言っているのか分かりません。選択肢を始めるか、選択ボードを再起動するには、/start と入力してください。"

# Set để lưu trữ user_id đã được chào mừng (sẽ bị reset khi bot khởi động lại)
welcomed_users = set()

# --- Hàm tiện ích ---
def get_or_create_id(text: str) -> int:
    """Gán một ID số nguyên duy nhất cho một chuỗi."""
    global next_id
    if text not in STRING_TO_ID_MAP:
        STRING_TO_ID_MAP[text] = next_id
        ID_TO_STRING_MAP[next_id] = text
        next_id += 1
    return STRING_TO_ID_MAP[text]

def load_data_from_excel():
    """Tải và xử lý dữ liệu từ file Excel."""
    global DATA_TABLE
    try:
        df = pd.read_excel(EXCEL_FILE_PATH)
        required_columns = ["Key", "Rep1", "Rep2", "Rep3"]
        if not all(col in df.columns for col in required_columns):
            raise ValueError(f"File Excel phải có các cột: {', '.join(required_columns)}")

        df = df.fillna('')
        DATA_TABLE = df.astype(str).to_dict(orient='records')
        logger.info(f"Successfully loaded data from {EXCEL_FILE_PATH}")

        # Populate the ID mappings for all relevant strings in DATA_TABLE
        for row in DATA_TABLE:
            get_or_create_id(row["Key"])
            get_or_create_id(row["Rep1"])
            get_or_create_id(row["Rep2"])
        logger.info("String to ID mappings created successfully.")

    except FileNotFoundError:
        logger.critical(f"Lỗi: Không tìm thấy file {EXCEL_FILE_PATH}. Đảm bảo file nằm ở thư mục gốc.")
        raise SystemExit("Required data file not found. Exiting.")
    except ValueError as ve:
        logger.critical(f"Lỗi định dạng file Excel: {ve}")
        raise SystemExit("Excel file format error. Exiting.")
    except Exception as e:
        logger.critical(f"Lỗi khi tải dữ liệu từ Excel: {e}")
        raise SystemExit("An unexpected error occurred while loading data. Exiting.")

# Tải dữ liệu ngay khi khởi động
load_data_from_excel()

# --- Xử lý Bot Telegram ---

async def send_initial_key_buttons(update_object: Update):
    """Gửi tin nhắn chào mừng và các nút cấp độ 'Key' ban đầu."""
    initial_keys_display = set()
    for row in DATA_TABLE:
        if row["Key"]:
            initial_keys_display.add(row["Key"])

    keyboard = []
    for key_val_display in sorted(list(initial_keys_display)):
        key_val_id = get_or_create_id(key_val_display)
        keyboard.append([InlineKeyboardButton(key_val_display, callback_data=f"{LEVEL_KEY}:{key_val_id}::")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    user_telegram_id = update_object.effective_user.id
    logger.info(f"Sending initial welcome message to user ID: {user_telegram_id}")
    await update_object.message.reply_text(INITIAL_WELCOME_MESSAGE_JP, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Phản hồi tin nhắn người dùng, xử lý chào mừng lần đầu."""
    user_telegram_id = update.effective_user.id

    if user_telegram_id not in welcomed_users:
        await send_initial_key_buttons(update)
        welcomed_users.add(user_telegram_id)
        logger.info(f"User {user_telegram_id} welcomed for the first time.")
        return
        
    if update.message and update.message.text:
        logger.info(f"Received unexpected text from welcomed user: '{update.message.text}' (User ID: {user_telegram_id})")
        await update.message.reply_text(UNRECOGNIZED_MESSAGE_JP)
    else:
        logger.warning("Received an update without message text from welcomed user: %s (User ID: %s)", update, user_telegram_id)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý lệnh /start, reset trạng thái và gửi lại nút ban đầu."""
    user_telegram_id = update.effective_user.id
    
    if user_telegram_id in welcomed_users:
        welcomed_users.remove(user_telegram_id)
        logger.info(f"User {user_telegram_id} removed from welcomed_users (session reset by /start).")
        
    await send_initial_key_buttons(update)
    welcomed_users.add(user_telegram_id)

async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý các truy vấn callback đến từ các nút inline."""
    query = update.callback_query
    await query.answer() # Bắt buộc gọi để tắt trạng thái "đang tải" trên nút

    # Phân tích callback_data: level:key_id:rep1_id:rep2_id
    data_parts = (query.data.split(':') + ['', '', '', ''])[:4]
    current_level = data_parts[0]
    selected_key_id = int(data_parts[1]) if data_parts[1] else -1
    selected_rep1_id = int(data_parts[2]) if data_parts[2] else -1
    selected_rep2_id = int(data_parts[3]) if data_parts[3] else -1

    selected_key_display = ID_TO_STRING_MAP.get(selected_key_id, f"ID_Key:{selected_key_id}")
    selected_rep1_display = ID_TO_STRING_MAP.get(selected_rep1_id, f"ID_Rep1:{selected_rep1_id}") if selected_rep1_id != -1 else ''
    selected_rep2_display = ID_TO_STRING_MAP.get(selected_rep2_id, f"ID_Rep2:{selected_rep2_id}") if selected_rep2_id != -1 else ''

    logger.info(f"Button press: Level={current_level}, Key_ID={selected_key_id} ({selected_key_display}), Rep1_ID={selected_rep1_id} ({selected_rep1_display}), Rep2_ID={selected_rep2_id} ({selected_rep2_display})")

    # Hàm trợ giúp để chỉnh sửa hoặc gửi tin nhắn mới
    async def edit_or_reply(text: str, reply_markup: InlineKeyboardMarkup = None, parse_mode: str = None):
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e:
            logger.warning(f"Could not edit message (ID: {query.message.message_id}, Level: {current_level}): {e}")
            # Nếu không thể edit (ví dụ: tin nhắn quá cũ hoặc đã bị sửa bởi người khác), gửi tin nhắn mới
            await query.message.reply_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)

    if current_level == LEVEL_KEY:
        next_rep1_values_display = set()
        for row in DATA_TABLE:
            if get_or_create_id(row["Key"]) == selected_key_id and row["Rep1"]:
                next_rep1_values_display.add(row["Rep1"])
        
        if next_rep1_values_display:
            keyboard = []
            for rep1_val_display in sorted(list(next_rep1_values_display)):
                rep1_val_id = get_or_create_id(rep1_val_display)
                keyboard.append([InlineKeyboardButton(rep1_val_display, callback_data=f"{LEVEL_REP1}:{selected_key_id}:{rep1_val_id}:")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            message_text = f"選択されました: {selected_key_display}\n{PROCESSING_WAIT_MESSAGE_JP}\n\n次に進んでください:"
            await edit_or_reply(message_text, reply_markup=reply_markup)
        else:
            await edit_or_reply(f"選択されました: {selected_key_display}\n情報が見つかりません。")

    elif current_level == LEVEL_REP1:
        next_rep2_values_display = set()
        for row in DATA_TABLE:
            if get_or_create_id(row["Key"]) == selected_key_id and \
               get_or_create_id(row["Rep1"]) == selected_rep1_id and row["Rep2"]:
                next_rep2_values_display.add(row["Rep2"])

        if next_rep2_values_display:
            keyboard = []
            for rep2_val_display in sorted(list(next_rep2_values_display)):
                rep2_val_id = get_or_create_id(rep2_val_display)
                keyboard.append([InlineKeyboardButton(rep2_val_display, callback_data=f"{LEVEL_REP2}:{selected_key_id}:{selected_rep1_id}:{rep2_val_id}")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            message_text = f"選択されました: {selected_rep1_display}\n{PROCESSING_WAIT_MESSAGE_JP}\n\n次に進んでください:"
            await edit_or_reply(message_text, reply_markup=reply_markup)
        else:
            await edit_or_reply(f"選択されました: {selected_rep1_display}\n情報が見つかりません。")

    elif current_level == LEVEL_REP2:
        final_rep3_text = "情報が見つかりません。"
        for row in DATA_TABLE:
            if get_or_create_id(row["Key"]) == selected_key_id and \
               get_or_create_id(row["Rep1"]) == selected_rep1_id and \
               get_or_create_id(row["Rep2"]) == selected_rep2_id:
                final_rep3_text = row["Rep3"]
                break
        
        # Sửa tin nhắn gốc hiển thị REP3
        await edit_or_reply(text=f"あなたの番号: {final_rep3_text}")
        
        # Gửi tin nhắn hướng dẫn và thời gian chờ riêng
        full_instruction_and_wait_text = f"{INSTRUCTION_MESSAGE_JP}\n\n{WAIT_TIME_MESSAGE_JP}"
        try:
            await query.message.reply_text(text=full_instruction_and_wait_text, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Could not send final instruction message: {e}")
            pass # Không cần raise Exception ở đây

    else:
        await edit_or_reply(text="不明な操作です。")

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
            return "ok", 200 # Trả về 200 OK ngay cả khi có lỗi xử lý để Telegram không gửi lại nhiều lần
    return "Method Not Allowed", 405

@flask_app.route("/health", methods=["GET"])
def health_check():
    """Endpoint for Render's health checks."""
    return jsonify({"status": "ok"})

# --- Xử lý lỗi toàn cục cho Application ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ghi log lỗi và có thể gửi tin nhắn Telegram để thông báo cho nhà phát triển."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    # Bạn có thể thêm logic để gửi thông báo lỗi đến một admin chat_id cụ thể ở đây
    # if context.bot and update:
    #     try:
    #         admin_chat_id = os.getenv("ADMIN_CHAT_ID")
    #         if admin_chat_id:
    #             await context.bot.send_message(chat_id=admin_chat_id, text=f"Error: {context.error}\nUpdate: {update}")
    #     except Exception as send_error:
    #         logger.error(f"Failed to send error notification: {send_error}")

# --- Logic chính của ứng dụng (điểm vào) ---
async def run_full_application():
    global application

    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable not set. Exiting.")
        raise ValueError("BOT_TOKEN environment variable not set.")
    if not WEBHOOK_URL:
        logger.critical("WEBHOOK_URL environment variable not set. Exiting.")
        raise ValueError("WEBHOOK_URL environment variable not set.")

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Thêm các handler
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
