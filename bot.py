import logging 
import os
import asyncio
import pandas as pd 

# Firebase Imports (giữ nguyên để tránh lỗi nếu bạn đã cấu hình, nhưng không bắt buộc cho logic reset này)
from firebase_admin import credentials, initialize_app, firestore, auth
from firebase_admin.exceptions import FirebaseError
import json # Để parse __firebase_config
import jwt # Để giải mã __initial_auth_token (cần pip install PyJWT)

# Telegram Imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler

# Flask Imports (for health check)
from flask import Flask, request, jsonify 
from hypercorn.asyncio import serve
from hypercorn.config import Config

# --- Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global Firebase and Firestore variables (giữ nguyên để tránh lỗi nếu bạn đã cấu hình)
db = None
firebase_auth = None
current_user_id = None # To store the authenticated user ID for the bot itself
__app_id = None # Global variable for app_id
firebase_config_str = None # Global variable for firebase_config
initial_auth_token = None # Global variable for initial_auth_token

flask_app = Flask(__name__)
application = None 

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

# --- Firestore Functions (giữ nguyên để tránh lỗi nếu bạn đã cấu hình) ---
async def is_user_welcomed_firestore(user_telegram_id: int) -> bool:
    """Checks if a user has been welcomed using Firestore."""
    if db is None or current_user_id is None:
        logger.error("Firestore DB or current_user_id not initialized.")
        return False 

    try:
        doc_ref = db.collection(f"artifacts/{__app_id}/users/{current_user_id}/welcomed_users_status").document(str(user_telegram_id))
        doc = await asyncio.to_thread(doc_ref.get) 
        return doc.exists and doc.get('welcomed') == True
    except FirebaseError as e:
        logger.error(f"Firestore error checking welcome status for {user_telegram_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error checking welcome status for {user_telegram_id}: {e}")
        return False

async def mark_user_welcomed_firestore(user_telegram_id: int):
    """Marks a user as welcomed in Firestore."""
    if db is None or current_user_id is None:
        logger.error("Firestore DB or current_user_id not initialized.")
        return

    try:
        doc_ref = db.collection(f"artifacts/{__app_id}/users/{current_user_id}/welcomed_users_status").document(str(user_telegram_id))
        await asyncio.to_thread(doc_ref.set({'welcomed': True})) 
        logger.info(f"User {user_telegram_id} marked as welcomed in Firestore.")
    except FirebaseError as e:
        logger.error(f"Firestore error marking welcome status for {user_telegram_id}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error marking welcome status for {user_telegram_id}: {e}")

# --- Hàm gửi các nút cấp độ đầu tiên ---
async def send_initial_key_buttons(update_object: Update):
    """Gửi tin nhắn chào mừng và các nút cấp độ 'Key' ban đầu."""
    initial_keys = set()
    for row in DATA_TABLE:
        initial_keys.add(row["Key"])

    keyboard = []
    for key_val in sorted(list(initial_keys)):
        keyboard.append([InlineKeyboardButton(key_val, callback_data=f"{LEVEL_KEY}:{key_val}::")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    user_telegram_id = update_object.message.from_user.id
    await update_object.message.reply_text(f"三上はじめにへようこそ (User ID: {user_telegram_id})")
    await update_object.message.reply_text("以下の選択肢からお選びください:", reply_markup=reply_markup) 


# --- Telegram Bot Handlers ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Phản hồi tin nhắn người dùng, xử lý chào mừng lần đầu."""
    user_telegram_id = update.message.from_user.id

    # Kiểm tra trạng thái chào mừng từ bộ nhớ (nếu không dùng Firestore)
    # Hoặc từ Firestore (nếu đã cấu hình)
    if user_telegram_id not in welcomed_users: # Logic cho bộ nhớ
        # if not await is_user_welcomed_firestore(user_telegram_id): # Logic cho Firestore
        await send_initial_key_buttons(update)
        welcomed_users.add(user_telegram_id) # Logic cho bộ nhớ
        # await mark_user_welcomed_firestore(user_telegram_id) # Logic cho Firestore
        return
    
    if update.message and update.message.text:
        logger.info(f"Received unexpected text from welcomed user: '{update.message.text}'")
        await update.message.reply_text("何を言っているのか分かりません。ボタンを使用してください。")
    else:
        logger.warning("Received an update without message text from welcomed user: %s", update)


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
    
    # Nếu bạn đang dùng Firestore, bạn cũng có thể reset trạng thái trong Firestore:
    # if db is not None:
    #     try:
    #         doc_ref = db.collection(f"artifacts/{__app_id}/users/{current_user_id}/welcomed_users_status").document(str(user_telegram_id))
    #         await asyncio.to_thread(doc_ref.delete())
    #         logger.info(f"User {user_telegram_id} welcome status deleted from Firestore.")
    #     except Exception as e:
    #         logger.error(f"Error deleting welcome status from Firestore for {user_telegram_id}: {e}")

    await send_initial_key_buttons(update)
    welcomed_users.add(user_telegram_id) # Thêm lại vào bộ nhớ sau khi chào mừng
    # await mark_user_welcomed_firestore(user_telegram_id) # Thêm lại vào Firestore sau khi chào mừng


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
                await query.message.reply_text(f"選択されました: {selected_key}\n以下の選択肢からお選びください:", reply_markup=reply_markup) 
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
                await query.message.reply_text(f"選択されました: {selected_rep1}\n以下の選択肢からお選びください:", reply_markup=reply_markup) 
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
    #         await context.bot.send_message(chat_id=YOUR_ADMIN_CHAT_ID, text=f"Error: {context.error}\nUpdate: {update}")
    #     except Exception as send_error:
    #         logger.error(f"Failed to send error notification: {send_error}")


# --- Main Application Logic (Entry Point) ---
async def run_full_application():
    global application, db, firebase_auth, current_user_id, __app_id, firebase_config_str, initial_auth_token

    # Lấy các biến môi trường
    TOKEN = os.getenv("BOT_TOKEN")
    BASE_WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    PORT = int(os.getenv("PORT", 8443)) 

    # Lấy các biến global từ môi trường Canvas
    __app_id = os.getenv('__app_id') 
    firebase_config_str = os.getenv('__firebase_config')
    initial_auth_token = os.getenv('__initial_auth_token')

    if not TOKEN:
        logger.critical("BOT_TOKEN environment variable not set. Exiting.")
        raise ValueError("BOT_TOKEN environment variable not set.")
    if not BASE_WEBHOOK_URL:
        logger.critical("WEBHOOK_URL environment variable not set. Exiting.")
        raise ValueError("WEBHOOK_URL environment variable not set.")
    
    # Kiểm tra các biến Firebase (chỉ khi bạn muốn sử dụng Firebase)
    # if not __app_id:
    #     logger.critical("__app_id environment variable not set. Exiting.")
    #     raise ValueError("__app_id environment variable not set.")
    # if not firebase_config_str:
    #     logger.critical("__firebase_config environment variable not set. Exiting.")
    #     raise ValueError("__firebase_config environment variable not set.")

    # --- Initialize Firebase (Chỉ khi bạn muốn sử dụng Firebase) ---
    # try:
    #     # Giả định __firebase_config là JSON string của service account key.
    #     service_account_info = json.loads(firebase_config_str)
    #     cred = credentials.Certificate(service_account_info)
    #     initialize_app(cred)
    #     db = firestore.client()
    #     firebase_auth = auth
    #     logger.info("Firebase initialized successfully.")
    # except Exception as e:
    #     logger.critical(f"Failed to initialize Firebase: {e}")
    #     raise SystemExit("Firebase initialization failed. Exiting.")

    # --- Authenticate Bot User (Chỉ khi bạn muốn sử dụng Firebase) ---
    # try:
    #     if initial_auth_token:
    #         try:
    #             decoded_token = jwt.decode(initial_auth_token, options={"verify_signature": False})
    #             current_user_id = decoded_token.get('uid')
    #             if not current_user_id:
    #                 raise ValueError("UID not found in initial_auth_token.")
    #             logger.info(f"Bot authenticated with user ID from token: {current_user_id}")
    #         except Exception as e:
    #             logger.warning(f"Could not decode initial_auth_token to get UID: {e}. Using default bot service user ID.")
    #             current_user_id = "default_bot_service_user_id" # Fallback
    #     else:
    #         current_user_id = "default_bot_service_user_id" # Fallback if no token
    #         logger.info("No initial_auth_token provided. Using default bot service user ID.")
    # except Exception as e:
    #     logger.critical(f"Failed to authenticate bot user: {e}")
    #     raise SystemExit("Bot authentication failed. Exiting.")


    FULL_WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

    # Build the Application của python-telegram-bot
    application = ApplicationBuilder().token(TOKEN).build()

    # Thêm handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_button_press))
    application.add_error_handler(error_handler)

    # Khởi tạo Application của python-telegram-bot
    await application.initialize() 

    # Thiết lập Telegram webhook
    logger.info("Setting Telegram webhook to: %s", FULL_WEBHOOK_URL)
    try:
        await application.bot.set_webhook(url=FULL_WEBHOOK_URL)
        logger.info("Telegram webhook set successfully.")
    except Exception as e:
        logger.error("Error setting Telegram webhook: %s", e)
        # Nếu webhook không thiết lập được, có thể bot sẽ không hoạt động
        # Bạn có thể chọn raise lỗi ở đây nếu muốn dừng triển khai
        # raise

    # Chạy Hypercorn để phục vụ Flask app (bao gồm cả webhook Telegram và health check)
    logger.info("Flask app (via Hypercorn) listening on port %d", PORT)
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    
    # serve là một coroutine và sẽ chạy vô thời hạn, giữ event loop mở
    await serve(flask_app, config)


if __name__ == '__main__':
    try:
        # Chạy hàm async chính
        asyncio.run(run_full_application())
    except Exception as e:
        logger.critical("Application stopped due to an unhandled error: %s", e)
