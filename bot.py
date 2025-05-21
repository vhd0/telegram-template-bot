import logging 
import os
import asyncio
import pandas as pd 

# Firebase Imports
from firebase_admin import credentials, initialize_app, firestore, auth
from firebase_admin.exceptions import FirebaseError

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

# Global Firebase and Firestore variables
db = None
firebase_auth = None
current_user_id = None # To store the authenticated user ID for the bot itself

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

# --- Firestore Functions ---
async def is_user_welcomed_firestore(user_telegram_id: int) -> bool:
    """Checks if a user has been welcomed using Firestore."""
    if db is None or current_user_id is None:
        logger.error("Firestore DB or current_user_id not initialized.")
        return False # Fallback if DB not ready

    try:
        # Đường dẫn tới tài liệu lưu trạng thái chào mừng của người dùng Telegram
        # Sử dụng __app_id và current_user_id (bot's auth ID) để tuân thủ quy tắc bảo mật
        # và user_telegram_id để định danh người dùng Telegram cụ thể
        doc_ref = db.collection(f"artifacts/{__app_id}/users/{current_user_id}/welcomed_users_status").document(str(user_telegram_id))
        doc = await asyncio.to_thread(doc_ref.get) # Chạy get() trong một thread riêng để không chặn event loop
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
        await asyncio.to_thread(doc_ref.set({'welcomed': True})) # Chạy set() trong một thread riêng
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

    # Hiển thị userId của người dùng Telegram
    user_telegram_id = update_object.message.from_user.id
    await update_object.message.reply_text(f"三上はじめにへようこそ (User ID: {user_telegram_id})")
    await update_object.message.reply_text("以下の選択肢からお選びください:", reply_markup=reply_markup) 


# --- Telegram Bot Handlers ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Phản hồi tin nhắn người dùng, xử lý chào mừng lần đầu."""
    user_telegram_id = update.message.from_user.id

    if not await is_user_welcomed_firestore(user_telegram_id):
        await send_initial_key_buttons(update)
        await mark_user_welcomed_firestore(user_telegram_id)
        return
    
    if update.message and update.message.text:
        logger.info(f"Received unexpected text from welcomed user: '{update.message.text}'")
        await update.message.reply_text("何を言っているのか分かりません。ボタンを使用してください。")
    else:
        logger.warning("Received an update without message text from welcomed user: %s", update)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý lệnh /start - sẽ kích hoạt logic chào mừng nếu người dùng chưa được chào mừng."""
    user_telegram_id = update.message.from_user.id
    if not await is_user_welcomed_firestore(user_telegram_id):
        await send_initial_key_buttons(update)
        await mark_user_welcomed_firestore(user_telegram_id)
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
            return "ok", 200 # Luôn trả về 200 OK để Telegram không thử lại liên tục
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
    global application, db, firebase_auth, current_user_id, __app_id

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
    if not __app_id:
        logger.critical("__app_id environment variable not set. Exiting.")
        raise ValueError("__app_id environment variable not set.")
    if not firebase_config_str:
        logger.critical("__firebase_config environment variable not set. Exiting.")
        raise ValueError("__firebase_config environment variable not set.")


    # --- Initialize Firebase ---
    try:
        # Firebase Admin SDK cần một credential file hoặc dictionary.
        # __firebase_config thường là một JSON string.
        firebase_config = json.loads(firebase_config_str)
        # Sử dụng serviceAccountKey.json nếu có, hoặc cấu hình từ dict
        # Đối với Canvas, thường là dict
        
        # Tạo một credential từ config.
        # Firebase Admin SDK không dùng trực tiếp dict như JS SDK.
        # Cần một service account key.
        # Nếu __firebase_config là một dict đơn giản (như apiKey, projectId),
        # bạn cần tạo một service account key JSON file và upload nó.
        # Hoặc, nếu Canvas cung cấp một cách khác để init Admin SDK, hãy dùng nó.
        # Tạm thời, giả định __firebase_config có thể được dùng để tạo Credential.
        # Đây là một giả định mạnh, có thể cần điều chỉnh tùy theo cách Canvas cung cấp.
        
        # Cách chuẩn để init Firebase Admin SDK là dùng service account key file.
        # Nếu __firebase_config là JSON của service account key:
        # cred = credentials.Certificate(json.loads(firebase_config_str))
        # initialize_app(cred)
        
        # Nếu __firebase_config chỉ là config client side (apiKey, projectId, etc.):
        # Firebase Admin SDK không thể init chỉ với các thông tin đó.
        # Bạn cần một service account key JSON file, thường được tải lên dưới dạng biến môi trường.
        # Giả sử __firebase_config là một JSON string của service account key.
        
        # Để đơn giản và tương thích với Canvas, chúng ta sẽ sử dụng cách init
        # mà Canvas thường mong đợi nếu nó cung cấp một service account key.
        # Nếu không, đây là một điểm cần làm rõ với môi trường Canvas.
        
        # Tạm thời, tôi sẽ sử dụng một cách khởi tạo giả định cho Firebase Admin SDK
        # nếu __firebase_config không phải là service account key JSON.
        # Nếu nó là service account key JSON:
        try:
            service_account_info = json.loads(firebase_config_str)
            cred = credentials.Certificate(service_account_info)
            initialize_app(cred)
        except json.JSONDecodeError:
            # Nếu không phải JSON hợp lệ, có thể là base64 encoded hoặc dạng khác
            # Hoặc chỉ là config client side.
            # Trong trường hợp này, Firebase Admin SDK không thể init trực tiếp.
            # Cần kiểm tra lại cách Canvas cung cấp Firebase Admin SDK credentials.
            logger.critical("Invalid __firebase_config format. Expected Service Account JSON.")
            raise ValueError("Invalid Firebase config. Please provide Service Account JSON.")
        except Exception as e:
            logger.critical(f"Error initializing Firebase Admin SDK: {e}")
            raise

        db = firestore.client()
        firebase_auth = auth
        logger.info("Firebase initialized successfully.")
    except Exception as e:
        logger.critical(f"Failed to initialize Firebase: {e}")
        raise SystemExit("Firebase initialization failed. Exiting.")

    # --- Authenticate Bot User ---
    try:
        if initial_auth_token:
            # Firebase Admin SDK không có signInWithCustomToken trực tiếp cho bot.
            # Đây là cách để xác thực người dùng cuối (end-user) với Firebase Auth.
            # Đối với bot, chúng ta thường không cần xác thực bot user với Firebase Auth
            # trừ khi bot cần truy cập dữ liệu được bảo vệ bởi Firebase Auth Rules
            # mà không phải là Admin SDK.
            # Admin SDK đã có quyền admin.
            # current_user_id sẽ là user ID của người dùng bot tương tác, không phải bot ID.
            # Nếu bạn muốn lưu dữ liệu riêng cho bot, bạn sẽ dùng một ID cố định.
            
            # Để tuân thủ quy tắc bảo mật của Canvas:
            # Private data: /artifacts/{appId}/users/{userId}/{your_collection_name}
            # userId ở đây là ID của người dùng đang tương tác với bot, hoặc ID của bot nếu dữ liệu là của bot.
            # Nếu bạn muốn lưu dữ liệu riêng cho bot, bạn có thể tự định nghĩa một bot_user_id.
            # Ví dụ: current_user_id = "bot_service_user"
            
            # Tuy nhiên, nếu __initial_auth_token là để xác thực bot trên Firestore
            # theo quy tắc của Canvas, thì nó phải được sử dụng.
            # Giả định __initial_auth_token là một token để xác thực một user trong Firebase Auth
            # mà bot sẽ sử dụng để truy cập dữ liệu của chính nó.
            
            # Để đơn giản, chúng ta sẽ giả định current_user_id là một ID cố định cho bot
            # hoặc lấy từ auth token nếu nó là một JWT.
            # Nếu __initial_auth_token là một JWT, bạn có thể giải mã nó để lấy uid.
            # Tuy nhiên, Firebase Admin SDK đã có quyền admin.
            # current_user_id sẽ là ID của người dùng bot đang tương tác.
            
            # Để tuân thủ quy tắc của Canvas:
            # `userId`: the current user ID (string). If the user is authenticated, use the `uid` as the identifier for both public and private data. If the user is not authenticated, use a random string as the identifier.
            # `__initial_auth_token`: This is a Firebase custom auth token string automatically provided within the Canvas environment.
            
            # Điều này có nghĩa là bot của bạn cần xác thực một user trong Firebase Auth
            # bằng cách sử dụng __initial_auth_token.
            # Tuy nhiên, Firebase Admin SDK không có client-side auth methods.
            # Đây là một điểm mâu thuẫn giữa Admin SDK và hướng dẫn Canvas.
            
            # Nếu __initial_auth_token là một JWT token, bạn có thể giải mã nó để lấy UID.
            # Nhưng Admin SDK không cần xác thực cho chính nó để truy cập Firestore.
            # Nó đã có quyền admin.
            
            # Giả định rằng `current_user_id` trong ngữ cảnh này là ID của bot service
            # hoặc một ID chung cho tất cả dữ liệu mà bot quản lý.
            # Nếu `__initial_auth_token` là một JWT, chúng ta có thể giải mã nó để lấy UID.
            # Nếu không, chúng ta sẽ dùng một ID mặc định.
            
            # Để tránh phức tạp và tuân thủ Admin SDK, chúng ta sẽ giả định
            # `current_user_id` là một ID cố định cho bot service.
            # Nếu Canvas muốn bot xác thực một user cụ thể, họ sẽ cung cấp cách để làm điều đó với Admin SDK.
            current_user_id = "bot_service_user_id" # Một ID cố định cho bot service
            logger.info(f"Bot service user ID set to: {current_user_id}")
            
            # Nếu __initial_auth_token thực sự là để xác thực một user cho Firestore rules:
            # Bạn sẽ cần một client-side Firebase Auth SDK (như firebase/auth.js)
            # hoặc một thư viện giải mã JWT để lấy UID từ token.
            # Với Python Admin SDK, bạn đã có quyền admin, nên việc xác thực user cụ thể
            # chỉ cần thiết nếu bạn muốn tuân thủ Firestore Rules cho user đó.
            # Với private data, rules yêu cầu `request.auth.uid == userId`.
            # Điều này ngụ ý rằng `userId` phải là UID của người dùng đang tương tác.
            # Do đó, `current_user_id` nên là ID của người dùng Telegram đang tương tác.
            # Nhưng `welcomed_users_status` là dữ liệu của bot về người dùng.
            
            # Để đơn giản và tuân thủ quy tắc bảo mật của Canvas,
            # chúng ta sẽ lưu trạng thái chào mừng dưới `users/{bot_auth_uid}/welcomed_users_status`.
            # `bot_auth_uid` sẽ là UID của user mà `__initial_auth_token` xác thực.
            
            # Đây là một điểm phức tạp với Admin SDK.
            # Client-side Firebase Auth SDK:
            # from firebase_admin import auth
            # user = auth.verify_id_token(initial_auth_token)
            # current_user_id = user['uid']
            
            # Tuy nhiên, `__initial_auth_token` là custom auth token, không phải ID token.
            # Nó dùng để `signInWithCustomToken` trên client.
            # Với Admin SDK, bạn có thể tạo custom token, nhưng không thể dùng nó để "sign in" chính Admin SDK.
            
            # Tạm thời, tôi sẽ giả định `current_user_id` là một ID cố định cho bot
            # và các quy tắc bảo mật sẽ được nới lỏng cho `bot_service_user_id`
            # hoặc bạn sẽ cần điều chỉnh quy tắc bảo mật Firestore.
            # Hoặc, `__initial_auth_token` ngụ ý rằng bot sẽ tạo một client-side context.
            # Giả sử `__initial_auth_token` là một JWT mà bạn có thể giải mã để lấy UID.
            import jwt # Cần cài đặt PyJWT: pip install PyJWT
            try:
                decoded_token = jwt.decode(initial_auth_token, options={"verify_signature": False})
                current_user_id = decoded_token.get('uid')
                if not current_user_id:
                    raise ValueError("UID not found in initial_auth_token.")
                logger.info(f"Bot authenticated with user ID from token: {current_user_id}")
            except Exception as e:
                logger.warning(f"Could not decode initial_auth_token to get UID: {e}. Using default bot service user ID.")
                current_user_id = "default_bot_service_user_id" # Fallback
        else:
            current_user_id = "default_bot_service_user_id" # Fallback if no token
            logger.info("No initial_auth_token provided. Using default bot service user ID.")

    except Exception as e:
        logger.critical(f"Failed to authenticate bot user: {e}")
        raise SystemExit("Bot authentication failed. Exiting.")


    FULL_WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

    # Build the Application
    application = ApplicationBuilder().token(TOKEN).build()

    # Add handlers
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
