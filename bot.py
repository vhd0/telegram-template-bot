import logging
import os
import asyncio
import pandas as pd

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from telegram.error import TelegramError, BadRequest

from flask import Flask, request, jsonify
from hypercorn.asyncio import serve
from hypercorn.config import Config

# --- Cấu hình Logging ---
# Thiết lập cấu hình logging cơ bản để ghi lại các thông báo của bot
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Cấu hình Ứng dụng Flask và Telegram Bot ---
flask_app = Flask(__name__) # Khởi tạo ứng dụng Flask
application = None # Biến toàn cục để lưu trữ đối tượng Application của Telegram Bot

WEBHOOK_PATH = "/webhook_telegram" # Đường dẫn webhook cho Telegram

# --- Cấu hình Bot cho các tính năng mới ---
# Lấy ID kênh và ID admin từ biến môi trường
CHANNEL_CHAT_ID = os.getenv("CHANNEL_CHAT_ID")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# Chuyển đổi ADMIN_CHAT_ID sang số nguyên một lần để sử dụng dễ dàng hơn
try:
    ADMIN_CHAT_ID_INT = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None
except ValueError:
    logger.critical("ADMIN_CHAT_ID không phải là một số nguyên hợp lệ. Đang thoát.")
    raise SystemExit("Invalid ADMIN_CHAT_ID. Exiting.")

# Kiểm tra nếu CHANNEL_CHAT_ID chưa được đặt
if not CHANNEL_CHAT_ID:
    logger.critical("Biến môi trường CHANNEL_CHAT_ID chưa được đặt. Đang thoát.")
    raise ValueError("CHANNEL_CHAT_ID environment variable not set.")


# --- Cấu trúc Dữ liệu Bot ---
EXCEL_FILE_PATH = "rep.xlsx" # Đường dẫn đến file Excel chứa dữ liệu
DATA_TABLE = [] # Danh sách rỗng, sẽ được điền từ Excel

# Ánh xạ toàn cục để rút gọn callback_data (chuỗi dài thành ID số ngắn)
STRING_TO_ID_MAP = {} # Ánh xạ từ chuỗi đầy đủ sang ID số ngắn
ID_TO_STRING_MAP = {} # Ánh xạ từ ID số ngắn trở lại chuỗi đầy đủ
next_id = 0 # ID số tiếp theo sẽ được gán

def get_or_create_id(text: str) -> int:
    """
    Gán một ID số nguyên ngắn duy nhất cho một chuỗi cho trước.
    Nếu chuỗi đã có ID, trả về ID hiện có.
    """
    global next_id
    if text not in STRING_TO_ID_MAP:
        STRING_TO_ID_MAP[text] = next_id
        ID_TO_STRING_MAP[next_id] = text
        next_id += 1
    return STRING_TO_ID_MAP[text]

# --- Tải dữ liệu từ Excel ---
try:
    df = pd.read_excel(EXCEL_FILE_PATH)
    # Kiểm tra các cột bắt buộc trong file Excel
    required_columns = ["Key", "Rep1", "Rep2", "Rep3"]
    if not all(col in df.columns for col in required_columns):
        raise ValueError(f"File Excel phải có các cột: {', '.join(required_columns)}")

    # Điền các giá trị NaN (Not a Number) bằng chuỗi rỗng trước khi chuyển đổi toàn bộ DataFrame.
    # Điều này ngăn lỗi 'nan' khi đọc từ các ô trống trong Excel.
    df = df.fillna('')
    DATA_TABLE = df.astype(str).to_dict(orient='records')
    logger.info(f"Đã tải dữ liệu thành công từ {EXCEL_FILE_PATH}")

    # Điền ánh xạ ID cho tất cả các chuỗi liên quan trong DATA_TABLE
    for row in DATA_TABLE:
        get_or_create_id(row["Key"])
        get_or_create_id(row["Rep1"])
        get_or_create_id(row["Rep2"])
        # 'Rep3' thường là văn bản cuối cùng, không cần nút, nên không cần tạo ID.
    logger.info("Đã tạo ánh xạ chuỗi sang ID thành công.")

except FileNotFoundError:
    logger.critical(f"Lỗi: Không tìm thấy file {EXCEL_FILE_PATH}. Vui lòng đảm bảo nó nằm trong thư mục gốc.")
    raise SystemExit("Required data file not found. Exiting.")
except ValueError as ve:
    logger.critical(f"Lỗi định dạng file Excel: {ve}")
    raise SystemExit("Excel file format error. Exiting.")
except Exception as e:
    logger.critical(f"Lỗi khi tải dữ liệu từ Excel: {e}")
    raise SystemExit("An unexpected error occurred while loading data. Exiting.")


# --- Định nghĩa các hằng số cấp độ cho callback_data ---
LEVEL_KEY = "key"
LEVEL_REP1 = "rep1"
LEVEL_REP2 = "rep2"

# Thông tin kênh Telegram và hướng dẫn
TELEGRAM_CHANNEL_LINK = "https://t.me/+JlQulVIHX5AwOGVI" # Thay bằng link kênh của bạn

# Các thông điệp bot
INITIAL_WELCOME_MESSAGE_JP = "三上はじめにへようこそ。以下の選択肢からお選びください。"
INSTRUCTION_MESSAGE_JP = f"受け取った番号を、到着の10分前までにこちらのチャンネル <a href='{TELEGRAM_CHANNEL_LINK}'>Telegramチャネル</a> に送信してください。よろしくお願いいたします！"
UNRECOGNIZED_MESSAGE_JP = "何を言っているのか分かりません。選択肢を始めるか、選択ボードを再起動するには、/start と入力してください。"

# Set để lưu trữ user_id đã được chào mừng trong phiên hiện tại (sẽ bị reset khi bot khởi động lại)
welcomed_users = set()

# --- Hàm gửi các nút cấp độ đầu tiên ---
async def send_initial_key_buttons(update_object: Update):
    """Gửi tin nhắn chào mừng và các nút cấp độ 'Key' ban đầu."""
    initial_keys_display = set() # Dùng set để tránh trùng lặp hiển thị

    for row in DATA_TABLE:
        if row["Key"]: # Chỉ thêm vào nếu 'Key' không rỗng
            initial_keys_display.add(row["Key"])

    keyboard = []
    # Sắp xếp theo văn bản hiển thị để có thứ tự nhất quán
    for key_val_display in sorted(list(initial_keys_display)):
        key_val_id = get_or_create_id(key_val_display) # Lấy ID để sử dụng trong callback_data
        keyboard.append([InlineKeyboardButton(key_val_display, callback_data=f"{LEVEL_KEY}:{key_val_id}::")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    user_telegram_id = update_object.effective_user.id # Lấy ID người dùng an toàn hơn
    logger.info(f"Đang gửi tin nhắn chào mừng ban đầu đến người dùng ID: {user_telegram_id}")
    await update_object.message.reply_text(INITIAL_WELCOME_MESSAGE_JP, reply_markup=reply_markup)


# --- Telegram Bot Handlers ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Phản hồi tin nhắn người dùng, xử lý chào mừng lần đầu."""
    user_telegram_id = update.effective_user.id # Lấy ID người dùng an toàn hơn

    # Logic chào mừng lần đầu trong phiên hoạt động của bot
    if user_telegram_id not in welcomed_users:
        logger.info(f"Gửi tin nhắn chào mừng ban đầu đến người dùng ID: {user_telegram_id}")
        await send_initial_key_buttons(update)
        welcomed_users.add(user_telegram_id)
        return
        
    # Xử lý tin nhắn không mong đợi từ người dùng đã được chào mừng
    if update.message and update.message.text:
        logger.info(f"Đã nhận văn bản không mong đợi từ người dùng đã chào mừng: '{update.message.text}' (User ID: {user_telegram_id})")
        await update.message.reply_text(UNRECOGNIZED_MESSAGE_JP)
    else:
        logger.warning("Đã nhận một bản cập nhật không có văn bản tin nhắn từ người dùng đã chào mừng: %s (User ID: %s)", update, user_telegram_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý lệnh /start.
    Luôn gửi lại tin nhắn chào mừng và các nút ban đầu cho người dùng này,
    reset trạng thái "đã chào mừng" trong bộ nhớ.
    """
    user_telegram_id = update.effective_user.id
    
    # Xóa người dùng khỏi danh sách đã chào mừng (nếu có) để buộc gửi lại tin nhắn chào mừng
    if user_telegram_id in welcomed_users:
        welcomed_users.remove(user_telegram_id)
        logger.info(f"Người dùng {user_telegram_id} đã bị xóa khỏi welcomed_users (phiên đã đặt lại bởi /start).")
    
    await send_initial_key_buttons(update)
    welcomed_users.add(user_telegram_id) # Thêm lại vào bộ nhớ sau khi chào mừng


async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý các truy vấn callback đến từ các nút inline."""
    query = update.callback_query # Lấy đối tượng callback query
    
    await query.answer() # Luôn trả lời callback query càng sớm càng tốt để tránh lỗi "Loading..."

    # Phân tích callback_data: level:key_id:rep1_id:rep2_id
    # Đảm bảo các phần tử đủ số lượng, nếu không có thì gán mặc định là rỗng
    data_parts = (query.data.split(':') + ['', '', '', ''])[:4] # Đảm bảo luôn có 4 phần tử
    current_level = data_parts[0]
    # Chuyển đổi ID sang số nguyên an toàn, sử dụng -1 hoặc giá trị không hợp lệ nếu trống
    selected_key_id = int(data_parts[1]) if data_parts[1] else -1 
    selected_rep1_id = int(data_parts[2]) if data_parts[2] else -1
    selected_rep2_id = int(data_parts[3]) if data_parts[3] else -1

    # Chuyển đổi ID trở lại chuỗi gốc cho logic và hiển thị. Sử dụng .get() an toàn hơn.
    selected_key_display = ID_TO_STRING_MAP.get(selected_key_id, f"ID_Key:{selected_key_id}")
    selected_rep1_display = ID_TO_STRING_MAP.get(selected_rep1_id, f"ID_Rep1:{selected_rep1_id}") if selected_rep1_id != -1 else ''
    selected_rep2_display = ID_TO_STRING_MAP.get(selected_rep2_id, f"ID_Rep2:{selected_rep2_id}") if selected_rep2_id != -1 else ''

    logger.info(f"Nhấn nút: Level={current_level}, Key_ID={selected_key_id} ({selected_key_display}), Rep1_ID={selected_rep1_id} ({selected_rep1_display}), Rep2_ID={selected_rep2_id} ({selected_rep2_display})")

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
                logger.warning("Không thể chỉnh sửa tin nhắn cho REP1 (ID tin nhắn: %s): %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_key_display}\n以下の選択肢からお選びください:", reply_markup=reply_markup)
        else:
            try:
                await query.edit_message_text(text=f"選択されました: {selected_key_display}\n情報が見つかりません。")
            except Exception as e:
                logger.warning("Không thể chỉnh sửa tin nhắn cho REP1 (không có thông tin, ID tin nhắn: %s): %s", query.message.message_id, e)
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
                logger.warning("Không thể chỉnh sửa tin nhắn cho REP2 (ID tin nhắn: %s): %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_rep1_display}\n以下の選択肢からお選びください:", reply_markup=reply_markup)
        else:
            try:
                await query.edit_message_text(text=f"選択されました: {selected_rep1_display}\n情報が見つかりません。")
            except Exception as e:
                logger.warning("Không thể chỉnh sửa tin nhắn cho REP2 (không có thông tin, ID tin nhắn: %s): %s", query.message.message_id, e)
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
        
        full_response_text = f"{final_text}\n\n{INSTRUCTION_MESSAGE_JP}"

        try:
            # Sử dụng parse_mode='HTML' để link được hiển thị đúng
            await query.edit_message_text(text=full_response_text, parse_mode='HTML')
        except Exception as e:
            logger.warning("Không thể chỉnh sửa tin nhắn (cuối cùng, ID tin nhắn: %s): %s", query.message.message_id, e)
            await query.message.reply_text(text=full_response_text, parse_mode='HTML')

        # --- THÊM LOGIC MỚI: Thêm người dùng vào kênh và lên lịch kick ---
        user_telegram_id = query.from_user.id
        user_first_name = query.from_user.first_name
        user_username = query.from_user.username # Có thể là None

        # 1. Thêm người dùng vào kênh
        add_success = await add_user_to_channel(context, CHANNEL_CHAT_ID, user_telegram_id)

        if add_success:
            # 2. Gửi thông báo vào kênh chat mới
            try:
                # Tạo tên người dùng để hiển thị
                user_display_name = user_first_name
                if user_username:
                    # Escape các ký tự đặc biệt trong username cho MarkdownV2
                    escaped_username = user_username.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")
                    user_display_name += f" (@{escaped_username})"
                
                # Escape các ký tự đặc biệt trong final_text cho MarkdownV2
                escaped_final_text = final_text.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")

                # Gửi tin nhắn vào kênh
                notification_message = (
                    f"Người dùng mới đã hoàn tất lựa chọn:\n"
                    f"ID: `{user_telegram_id}`\n"
                    f"Tên: `{user_display_name}`\n"
                    f"Mã cuối cùng: `{escaped_final_text}`"
                )
                await context.bot.send_message(
                    chat_id=CHANNEL_CHAT_ID, 
                    text=notification_message, 
                    parse_mode='MarkdownV2' # Sử dụng MarkdownV2 để mã ID và tên hiển thị đúng
                )
                logger.info(f"Đã gửi thông báo đến kênh {CHANNEL_CHAT_ID} cho người dùng {user_telegram_id}.")
            except TelegramError as e:
                logger.error(f"Thất bại khi gửi thông báo đến kênh {CHANNEL_CHAT_ID}: {e}")
            
            # 3. Lên lịch kick người dùng sau 30 phút (trừ admin)
            asyncio.create_task(schedule_kick_user(context, CHANNEL_CHAT_ID, user_telegram_id))

    else:
        try:
            await query.edit_message_text(text="不明な操作です。")
        except Exception as e:
            logger.warning("Không thể chỉnh sửa tin nhắn (thao tác không xác định, ID tin nhắn: %s): %s", query.message.message_id, e)
            await query.message.reply_text("不明な操作です。")


# --- Hàm hỗ trợ quản lý người dùng kênh ---
async def add_user_to_channel(context: ContextTypes.DEFAULT_TYPE, channel_id: str, user_id: int) -> bool:
    """Thêm người dùng vào kênh Telegram."""
    try:
        # Cố gắng mời người dùng vào kênh
        await context.bot.add_chat_member(chat_id=channel_id, user_id=user_id)
        logger.info(f"Đã thêm người dùng ID: {user_id} thành công vào kênh: {channel_id}")
        return True
    except BadRequest as e:
        # Xử lý các lỗi cụ thể từ Telegram API
        if "USER_ALREADY_PARTICIPANT" in str(e):
            logger.warning(f"Người dùng ID: {user_id} đã có trong kênh: {channel_id}. Lỗi: {e}")
            return True # Coi như thành công vì người dùng đã ở trong kênh
        elif "USER_IS_BLOCKED" in str(e):
            logger.warning(f"Người dùng ID: {user_id} đã chặn bot. Không thể thêm vào kênh. Lỗi: {e}")
        elif "USER_NOT_FOUND" in str(e):
            logger.warning(f"Không tìm thấy người dùng ID: {user_id} hoặc ID không hợp lệ. Lỗi: {e}")
        elif "BOT_IS_NOT_AN_ADMIN" in str(e):
            logger.critical(f"Bot không phải là admin trong kênh {channel_id} hoặc không có quyền cần thiết (invite_users). Lỗi: {e}")
        else:
            logger.error(f"Thất bại khi thêm người dùng ID: {user_id} vào kênh: {channel_id} do BadRequest: {e}")
    except TelegramError as e:
        logger.error(f"Lỗi Telegram API khi thêm người dùng ID: {user_id} vào kênh: {channel_id}: {e}")
    except Exception as e:
        logger.error(f"Một lỗi không mong đợi đã xảy ra khi thêm người dùng ID: {user_id} vào kênh: {channel_id}: {e}")
    return False


async def schedule_kick_user(context: ContextTypes.DEFAULT_TYPE, channel_id: str, user_id: int):
    """
    Lên lịch để kick người dùng ra khỏi kênh sau 30 phút.
    Sẽ không kick nếu người dùng là admin được chỉ định.
    """
    if user_id == ADMIN_CHAT_ID_INT:
        logger.info(f"Người dùng ID: {user_id} là admin. Bỏ qua việc kick khỏi kênh: {channel_id}.")
        return

    delay_minutes = 30
    delay_seconds = delay_minutes * 60

    logger.info(f"Đang lên lịch kick người dùng ID: {user_id} khỏi kênh: {channel_id} trong {delay_minutes} phút.")
    await asyncio.sleep(delay_seconds) # Chờ 30 phút

    try:
        # Sử dụng ban_chat_member để kick người dùng. Sau đó, unban ngay lập tức nếu muốn họ có thể tham gia lại.
        # Điều này loại bỏ họ khỏi kênh và cho phép họ tham gia lại thông qua link mời nếu bạn muốn.
        await context.bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
        await context.bot.unban_chat_member(chat_id=channel_id, user_id=user_id)
        logger.info(f"Đã kick và unban người dùng ID: {user_id} thành công khỏi kênh: {channel_id}.")
    except BadRequest as e:
        if "USER_NOT_PARTICIPANT" in str(e):
            logger.warning(f"Người dùng ID: {user_id} đã bị xóa hoặc chưa bao giờ tham gia kênh: {channel_id}. Lỗi: {e}")
        elif "CHAT_ADMIN_REQUIRED" in str(e) or "BOT_IS_NOT_AN_ADMIN" in str(e):
            logger.critical(f"Bot không có đủ quyền để kick người dùng {user_id} khỏi kênh {channel_id}. Vui lòng cấp quyền 'Ban Users'. Lỗi: {e}")
        else:
            logger.error(f"Thất bại khi kick người dùng ID: {user_id} khỏi kênh: {channel_id} do BadRequest: {e}")
    except TelegramError as e:
        logger.error(f"Lỗi Telegram API khi kick người dùng ID: {user_id} khỏi kênh: {channel_id}: {e}")
    except Exception as e:
        logger.error(f"Một lỗi không mong đợi đã xảy ra khi kick người dùng ID: {user_id} khỏi kênh: {channel_id}: {e}")


# --- Flask Endpoints ---
@flask_app.route(WEBHOOK_PATH, methods=["POST"])
async def telegram_webhook():
    """Xử lý các cập nhật Telegram đến qua webhook."""
    global application
    if application is None:
        logger.error("Đối tượng Telegram Application chưa được khởi tạo.")
        return "Lỗi máy chủ nội bộ: Bot chưa sẵn sàng", 500

    if request.method == "POST":
        try:
            json_data = request.get_json(force=True)
            if not json_data:
                logger.warning("Đã nhận payload JSON trống hoặc không hợp lệ từ webhook.")
                return "Yêu cầu không hợp lệ", 400

            update = Update.de_json(json_data, application.bot)
            # Sử dụng asyncio.create_task để không chặn luồng chính của webhook,
            # cho phép Hypercorn trả về phản hồi 200 OK nhanh chóng.
            asyncio.create_task(application.process_update(update))
            logger.info("Đã lên lịch xử lý bản cập nhật Telegram thành công.")
            return "ok", 200
        except Exception as e:
            logger.error("Lỗi khi xử lý bản cập nhật Telegram: %s", e)
            # Luôn trả về 200 OK ngay cả khi có lỗi xử lý để Telegram không gửi lại nhiều lần
            return "ok", 200
    return "Phương thức không được phép", 405

@flask_app.route("/health", methods=["GET"])
def health_check():
    """Endpoint cho kiểm tra tình trạng sức khỏe của Render."""
    return jsonify({"status": "ok"})


# --- Global Error Handler cho Application ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ghi lại lỗi và có thể gửi tin nhắn Telegram để thông báo cho nhà phát triển."""
    logger.error("Ngoại lệ khi xử lý một bản cập nhật:", exc_info=context.error)
    # Bạn có thể bật đoạn code dưới đây để gửi thông báo lỗi đến ADMIN_CHAT_ID
    # if context.bot and update:
    #    try:
    #        admin_chat_id = ADMIN_CHAT_ID_INT
    #        if admin_chat_id:
    #            await context.bot.send_message(chat_id=admin_chat_id, text=f"Error: {context.error}\nUpdate: {update}")
    #    except Exception as send_error:
    #        logger.error(f"Failed to send error notification: {send_error}")


# --- Logic Ứng dụng Chính (Điểm Khởi đầu) ---
async def run_full_application():
    global application

    # Lấy các biến môi trường cần thiết
    TOKEN = os.getenv("BOT_TOKEN")
    BASE_WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    PORT = int(os.getenv("PORT", 8443)) # Cổng mặc định cho Render là 10000

    # Kiểm tra các biến môi trường bắt buộc
    if not TOKEN:
        logger.critical("Biến môi trường BOT_TOKEN chưa được đặt. Đang thoát.")
        raise ValueError("BOT_TOKEN environment variable not set.")
    if not BASE_WEBHOOK_URL:
        logger.critical("Biến môi trường WEBHOOK_URL chưa được đặt. Đang thoát.")
        raise ValueError("WEBHOOK_URL environment variable not set.")

    FULL_WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

    # Xây dựng đối tượng Application của Telegram Bot
    application = ApplicationBuilder().token(TOKEN).build()

    # Thêm các handler cho các lệnh và loại tin nhắn khác nhau
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_button_press))
    application.add_error_handler(error_handler) # Đăng ký global error handler

    # Khởi tạo ứng dụng (cần thiết cho webhook)
    await application.initialize()

    # Thiết lập webhook Telegram
    logger.info("Đang đặt webhook Telegram thành: %s", FULL_WEBHOOK_URL)
    try:
        await application.bot.set_webhook(url=FULL_WEBHOOK_URL)
        logger.info("Đã đặt webhook Telegram thành công.")
    except Exception as e:
        logger.error("Lỗi khi đặt webhook Telegram: %s", e)
        raise SystemExit("Không đặt được webhook. Đang thoát.") # Dừng ứng dụng nếu không thể thiết lập webhook

    # Khởi động ứng dụng Flask bằng Hypercorn
    logger.info("Ứng dụng Flask (qua Hypercorn) đang lắng nghe trên cổng %d", PORT)
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    
    await serve(flask_app, config)


if __name__ == '__main__':
    # Điểm khởi chạy chính của ứng dụng
    try:
        asyncio.run(run_full_application())
    except Exception as e:
        logger.critical("Ứng dụng đã dừng do lỗi không được xử lý: %s", e)
