import logging
import os
import asyncio
import pandas as pd

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from telegram.error import TelegramError # Import để bắt lỗi cụ thể của Telegram

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

# CHỈ LẤY CHANNEL_CHAT_ID TỪ BIẾN MÔI TRƯỜNG
CHANNEL_CHAT_ID = os.getenv("TELEGRAM_CHANNEL_CHAT_ID", "") # Mặc định là chuỗi rỗng nếu không tìm thấy

# Thông điệp chào mừng ban đầu
INITIAL_WELCOME_MESSAGE_JP = "三上はじめにへようこそ。以下の選択肢からお選びください。\n\n**ボタンを押した後、処理のためしばらくお待ちください。数秒経っても変化がない場合は、再度ボタンをタップしてください。ありがとうございます。**"

# Thông điệp nhắc nhở chờ phản hồi hoặc nhấn lại nút
WAIT_FOR_RESPONSE_MESSAGE_JP = "\n\n**処理のためしばらくお待ちください。数秒経っても変化がない場合は、再度ボタンをタップしてください。ありがとうございます。**"

# Thông điệp hướng dẫn sau khi bot đã xử lý (cố gắng) thêm vào kênh và gửi mã số
POST_CODE_SUCCESS_MESSAGE_JP = "お客様の番号は公式チャンネルに送信され、チャンネルへの追加が試行されました。ご確認ください。"
POST_CODE_FAIL_MESSAGE_JP = "申し訳ございません。チャンネルへの追加または番号の送信に問題が発生しました。手動でチャンネルに参加して、番号をご確認ください。" # Thêm chỗ này nếu cần link kênh trực tiếp
POST_CODE_NO_CONFIG_MESSAGE_JP = "チャンネルへの自動追加が設定されていないため、番号はチャンネルに送信されません。手動でチャンネルに参加して番号をご確認ください。"


# Thông điệp thông tin về thời gian chờ (dành cho final message)
WAIT_TIME_MESSAGE_JP = "通常、5分以内に部屋番号をお知らせしますが、担当者が忙しい場合、30分以上お待ちいただくこともございます。恐れ入りますが、しばらくお待ちください。"

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
    # Sử dụng parse_mode='Markdown' để bold đoạn văn bản
    await update_object.message.reply_text(INITIAL_WELCOME_MESSAGE_JP, reply_markup=reply_markup, parse_mode='Markdown')


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
                # Thêm thông báo chờ vào đây
                await query.edit_message_text(text=f"選択されました: {selected_key_display}\n次に進んでください:{WAIT_FOR_RESPONSE_MESSAGE_JP}", reply_markup=reply_markup, parse_mode='Markdown')
            except Exception as e:
                logger.warning("Could not edit message for REP1 (message ID: %s): %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_key_display}\n以下の選択肢からお選びください:{WAIT_FOR_RESPONSE_MESSAGE_JP}", reply_markup=reply_markup, parse_mode='Markdown')
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
                # Thêm thông báo chờ vào đây
                await query.edit_message_text(text=f"選択されました: {selected_rep1_display}\n次に進んでください:{WAIT_FOR_RESPONSE_MESSAGE_JP}", reply_markup=reply_markup, parse_mode='Markdown')
            except Exception as e:
                logger.warning("Could not edit message for REP2 (message ID: %s): %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_rep1_display}\n以下の選択肢からお選びください:{WAIT_FOR_RESPONSE_MESSAGE_JP}", reply_markup=reply_markup, parse_mode='Markdown')
        else:
            try:
                await query.edit_message_text(text=f"選択されました: {selected_rep1_display}\n情報が見つかりません。")
            except Exception as e:
                logger.warning("Could not edit message for REP2 (no info, message ID: %s): %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_rep1_display}\n情報が見つかりません。")

    elif current_level == LEVEL_REP2:
        final_rep3_text = "情報が見つかりません。"
        for row in DATA_TABLE:
            # So sánh với ID của Key, Rep1, Rep2 đã chọn
            if get_or_create_id(row["Key"]) == selected_key_id and \
               get_or_create_id(row["Rep1"]) == selected_rep1_id and \
               get_or_create_id(row["Rep2"]) == selected_rep2_id:
                final_rep3_text = row["Rep3"]
                break
        
        user_telegram_id = query.from_user.id
        user_full_name = query.from_user.full_name or f"User ID: {user_telegram_id}"
        message_to_channel = f"{final_rep3_text} - {user_full_name}"

        # Gửi REP3 thành tin nhắn riêng cho người dùng trước
        try:
            await query.edit_message_text(text=f"あなたの番号: {final_rep3_text}")
            logger.info(f"Sent Rep3 to user {user_telegram_id}.")
        except Exception as e:
            logger.warning("Could not edit message (sending Rep3, message ID: %s): %s", query.message.message_id, e)
            await query.message.reply_text(text=f"あなたの番号: {final_rep3_text}")
        
        # --- Logic thêm người dùng vào kênh và gửi mã số ---
        instruction_message_for_user = ""

        if CHANNEL_CHAT_ID: # Chỉ thực hiện nếu CHANNEL_CHAT_ID được thiết lập
            try:
                # Cố gắng thêm thành viên. Lưu ý: Chỉ hoạt động nếu kênh là Supergroup
                # và bot là admin với quyền "Invite Users" (can_invite_users=True)
                # và người dùng đã chat với bot trước đó.
                # set_chat_member status='member' là cách để thêm thành viên mới nhất
                await context.bot.set_chat_member(
                    chat_id=CHANNEL_CHAT_ID,
                    user_id=user_telegram_id,
                    status='member' # Đặt trạng thái là 'member' để thêm vào
                )
                logger.info(f"Attempted to add user {user_telegram_id} to channel {CHANNEL_CHAT_ID}.")
                
                # Gửi tin nhắn mã số vào kênh
                await context.bot.send_message(
                    chat_id=CHANNEL_CHAT_ID,
                    text=message_to_channel
                )
                logger.info(f"Sent code '{final_rep3_text}' for user {user_telegram_id} to channel {CHANNEL_CHAT_ID}.")
                instruction_message_for_user = POST_CODE_SUCCESS_MESSAGE_JP

            except TelegramError as e:
                logger.error(f"Telegram API Error adding user {user_telegram_id} or sending message to channel {CHANNEL_CHAT_ID}: {e}")
                instruction_message_for_user = f"{POST_CODE_FAIL_MESSAGE_JP} (Lỗi: {e.message})"
                # Nếu bot không thể thêm, có thể tạo link mời và gửi cho người dùng
                try:
                    # Tạo link mời có giới hạn 1 thành viên để chỉ dùng 1 lần
                    invite_link = await context.bot.create_chat_invite_link(chat_id=CHANNEL_CHAT_ID, member_limit=1)
                    instruction_message_for_user += f"\n\nまたは、このリンクから手動で参加してください: <a href='{invite_link.invite_link}'>チャンネルに参加</a>"
                except Exception as link_e:
                    logger.error(f"Could not create invite link: {link_e}")
                    instruction_message_for_user += "\n\n(リンクを作成できませんでした)"

            except Exception as e:
                logger.error(f"General Error adding user {user_telegram_id} or sending message to channel {CHANNEL_CHAT_ID}: {e}")
                instruction_message_for_user = f"{POST_CODE_FAIL_MESSAGE_JP} (Lỗi chung: {e})"
        else:
            logger.warning("CHANNEL_CHAT_ID is not set. Skipping adding user to channel and sending message to channel.")
            instruction_message_for_user = POST_CODE_NO_CONFIG_MESSAGE_JP
            
        # Gửi tin nhắn hướng dẫn và thời gian chờ tiếp theo cho người dùng (vào chat riêng của họ)
        full_instruction_and_wait_text = f"{instruction_message_for_user}\n\n{WAIT_TIME_MESSAGE_JP}"
        
        try:
            # Sử dụng parse_mode='HTML' nếu bạn có thể có link mời trong instruction_message_for_user
            await query.message.reply_text(text=full_instruction_and_wait_text, parse_mode='HTML')
            logger.info(f"Sent final instruction to user {user_telegram_id}.")
        except Exception as e:
            logger.error("Could not send final instruction message to user: %s", e)
            pass

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
