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
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Khởi tạo Flask và Application ---
flask_app = Flask(__name__)
application = None # Sẽ được khởi tạo trong run_full_application

WEBHOOK_PATH = "/webhook_telegram"

# --- Cấu trúc dữ liệu Bot ---
EXCEL_FILE_PATH = "rep.xlsx"
DATA_TABLE = []

STRING_TO_ID_MAP = {}
ID_TO_STRING_MAP = {}
next_id = 0

def get_or_create_id(text: str) -> int:
    """Tạo ID duy nhất cho mỗi chuỗi trong dữ liệu."""
    global next_id
    if text not in STRING_TO_ID_MAP:
        STRING_TO_ID_MAP[text] = next_id
        ID_TO_STRING_MAP[next_id] = text
        next_id += 1
    return STRING_TO_ID_MAP[text]

# --- Tải dữ liệu từ Excel ---
try:
    df = pd.read_excel(EXCEL_FILE_PATH)
    required_columns = ["Key", "Rep1", "Rep2", "Rep3"]
    if not all(col in df.columns for col in required_columns):
        raise ValueError(f"File Excel phải có các cột: {', '.join(required_columns)}")

    df = df.fillna('')
    DATA_TABLE = df.astype(str).to_dict(orient='records')
    logger.info(f"Đã tải dữ liệu thành công từ {EXCEL_FILE_PATH}")

    for row in DATA_TABLE:
        get_or_create_id(row["Key"])
        get_or_create_id(row["Rep1"])
        get_or_create_id(row["Rep2"])
    logger.info("Đã tạo ánh xạ chuỗi sang ID thành công.")

except FileNotFoundError:
    logger.critical(f"Lỗi: Không tìm thấy {EXCEL_FILE_PATH}. Đảm bảo nó nằm trong thư mục gốc.")
    raise SystemExit("Không tìm thấy file dữ liệu yêu cầu. Đang thoát.")
except ValueError as ve:
    logger.critical(f"Lỗi định dạng file Excel: {ve}")
    raise SystemExit("Lỗi định dạng file Excel. Đang thoát.")
except Exception as e:
    logger.critical(f"Lỗi khi tải dữ liệu từ Excel: {e}")
    raise SystemExit("Đã xảy ra lỗi không mong muốn khi tải dữ liệu. Đang thoát.")


# --- Định nghĩa hằng số ---
LEVEL_KEY = "key"
LEVEL_REP1 = "rep1"
LEVEL_REP2 = "rep2"

CHANNEL_CHAT_ID = os.getenv("TELEGRAM_CHANNEL_CHAT_ID", "") 
logger.info(f"Đã tải CHANNEL_CHAT_ID từ biến môi trường: '{CHANNEL_CHAT_ID}'")

ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
if ADMIN_CHAT_ID:
    logger.info(f"Đã tải ADMIN_CHAT_ID từ biến môi trường: '{ADMIN_CHAT_ID}'")
else:
    logger.warning("Biến môi trường ADMIN_CHAT_ID chưa được đặt. Admin có thể bị kick nếu cũng là người dùng thường.")

KICK_DELAY_SECONDS = 30 * 60 # 30 phút

INITIAL_WELCOME_MESSAGE_JP = "三上はじめにへようこそ。以下の選択肢からお選びください。\n\n**ボタンを押した後、処理のためしばらくお待ちください。数秒経っても変化がない場合は、再度ボタンをタップしてください。ありがとうございます。**"
WAIT_FOR_RESPONSE_MESSAGE_JP = "\n\n**処理のためしばらくお待ちください。数秒経っても変化がない場合は、再度ボタンをタップしてください。ありがとうございます。**"
POST_CODE_SUCCESS_MESSAGE_JP = "お客様の部屋番号は公式チャンネルに送信されました。チャンネルへようこそ！"
POST_CODE_FAIL_MESSAGE_JP = "申し訳ございません。現在、チャンネルへの追加または番号の送信に問題が発生しています。"
POST_CODE_NO_CONFIG_MESSAGE_JP = "チャンネル設定が完了していないため、部屋番号はチャンネルに送信されません。手動でチャンネルに参加して番号をご確認ください。"
POST_CODE_ALREADY_MEMBER_MESSAGE_JP = "お客様はすでにチャンネルのメンバーです。部屋番号はチャンネルに送信されました。"
WAIT_TIME_MESSAGE_JP = "通常、5分以内に部屋番号をお知らせしますが、担当者が忙しい場合、30分以上お待ちいただくこともございます。恐れ入りますが、しばらくお待ちください。"
UNRECOGNIZED_MESSAGE_JP = "何を言っているのか分かりません。選択肢を始めるか、選択ボードを再起動するには、/start と入力してください。"

welcomed_users = set()

# --- Hàm gửi các nút cấp độ đầu tiên ---
async def send_initial_key_buttons(update_object: Update):
    """Gửi các nút lựa chọn ban đầu cho người dùng."""
    initial_keys_display = set() 
    for row in DATA_TABLE:
        if row["Key"]:
            initial_keys_display.add(row["Key"])

    keyboard = []
    for key_val_display in sorted(list(initial_keys_display)):
        key_val_id = get_or_create_id(key_val_display) 
        keyboard.append([InlineKeyboardButton(key_val_display, callback_data=f"{LEVEL_KEY}:{key_val_id}::")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    user_telegram_id = update_object.message.from_user.id
    logger.info(f"Gửi tin nhắn chào mừng ban đầu đến người dùng ID: {user_telegram_id}")
    await update_object.message.reply_text(INITIAL_WELCOME_MESSAGE_JP, reply_markup=reply_markup, parse_mode='Markdown')


# --- Các trình xử lý Telegram Bot ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý các tin nhắn văn bản không phải lệnh."""
    user_telegram_id = update.message.from_user.id

    if user_telegram_id not in welcomed_users:
        await send_initial_key_buttons(update)
        welcomed_users.add(user_telegram_id)
        logger.info(f"Người dùng {user_telegram_id} được chào mừng lần đầu tiên.")
        return
        
    if update.message and update.message.text:
        logger.info(f"Nhận được tin nhắn không mong đợi từ người dùng đã chào mừng: '{update.message.text}' (ID người dùng: {user_telegram_id})")
        await update.message.reply_text(UNRECOGNIZED_MESSAGE_JP)
    else:
        logger.warning("Nhận được bản cập nhật không có văn bản tin nhắn từ người dùng đã chào mừng: %s (ID người dùng: %s)", update, user_telegram_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý lệnh /start."""
    user_telegram_id = update.message.from_user.id
    
    if user_telegram_id in welcomed_users:
        welcomed_users.remove(user_telegram_id)
        logger.info(f"Người dùng {user_telegram_id} đã bị xóa khỏi welcomed_users (phiên đã được đặt lại bởi /start).")
    
    await send_initial_key_buttons(update)
    welcomed_users.add(user_telegram_id)


async def schedule_kick_user(context: ContextTypes.DEFAULT_TYPE, channel_chat_id: str, user_id_to_kick: int):
    """Lên lịch kick người dùng khỏi kênh sau một thời gian trễ."""
    if ADMIN_CHAT_ID and str(user_id_to_kick) == ADMIN_CHAT_ID:
        logger.info(f"Người dùng {user_id_to_kick} là admin ({ADMIN_CHAT_ID}), bỏ qua việc kick khỏi kênh {channel_chat_id}.")
        return

    logger.info(f"Lên lịch kick người dùng {user_id_to_kick} khỏi kênh {channel_chat_id} sau {KICK_DELAY_SECONDS} giây.")
    await asyncio.sleep(KICK_DELAY_SECONDS)
    
    try:
        await context.bot.unban_chat_member(
            chat_id=channel_chat_id,
            user_id=user_id_to_kick
        )
        logger.info(f"Đã kick người dùng {user_id_to_kick} khỏi kênh {channel_chat_id} thành công.")
        
    except TelegramError as e:
        logger.error(f"Lỗi API Telegram khi kick người dùng {user_id_to_kick} khỏi kênh {channel_chat_id}: {e}")
    except Exception as e:
        logger.error(f"Lỗi chung khi kick người dùng {user_id_to_kick} khỏi kênh {channel_chat_id}: {e}")


async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý các lần nhấn nút inline keyboard."""
    query = update.callback_query
    await query.answer()

    # Phân tích callback_data
    data_parts = (query.data.split(':') + ['', '', '', ''])[:4]
    current_level = data_parts[0]
    selected_key_id = int(data_parts[1]) if data_parts[1] else -1
    selected_rep1_id = int(data_parts[2]) if data_parts[2] else -1
    selected_rep2_id = int(data_parts[3]) if data_parts[3] else -1

    selected_key_display = ID_TO_STRING_MAP.get(selected_key_id, f"ID_Key:{selected_key_id}")
    selected_rep1_display = ID_TO_STRING_MAP.get(selected_rep1_id, f"ID_Rep1:{selected_rep1_id}") if selected_rep1_id != -1 else ''
    selected_rep2_display = ID_TO_STRING_MAP.get(selected_rep2_id, f"ID_Rep2:{selected_rep2_id}") if selected_rep2_id != -1 else ''

    logger.info(f"Nhấn nút: Cấp độ={current_level}, Key_ID={selected_key_id} ({selected_key_display}), Rep1_ID={selected_rep1_id} ({selected_rep1_display}), Rep2_ID={selected_rep2_id} ({selected_rep2_display})")

    if current_level == LEVEL_KEY:
        # Xử lý cấp độ Key
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
            
            try:
                await query.edit_message_text(text=f"選択されました: {selected_key_display}\n次に進んでください:{WAIT_FOR_RESPONSE_MESSAGE_JP}", reply_markup=reply_markup, parse_mode='Markdown')
            except Exception as e:
                logger.warning("Không thể chỉnh sửa tin nhắn cho REP1 (ID tin nhắn: %s): %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_key_display}\n以下の選択肢からお選びください:{WAIT_FOR_RESPONSE_MESSAGE_JP}", reply_markup=reply_markup, parse_mode='Markdown')
        else:
            try:
                await query.edit_message_text(text=f"選択されました: {selected_key_display}\n情報が見つかりません。")
            except Exception as e:
                logger.warning("Không thể chỉnh sửa tin nhắn cho REP1 (không có thông tin, ID tin nhắn: %s): %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_key_display}\n情報が見つかりません。")

    elif current_level == LEVEL_REP1:
        # Xử lý cấp độ Rep1
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

            try:
                await query.edit_message_text(text=f"選択されました: {selected_rep1_display}\n次に進んでください:{WAIT_FOR_RESPONSE_MESSAGE_JP}", reply_markup=reply_markup, parse_mode='Markdown')
            except Exception as e:
                logger.warning("Không thể chỉnh sửa tin nhắn cho REP2 (ID tin nhắn: %s): %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_rep1_display}\n以下の選択肢からお選びください:{WAIT_FOR_RESPONSE_MESSAGE_JP}", reply_markup=reply_markup, parse_mode='Markdown')
        else:
            try:
                await query.edit_message_text(text=f"選択されました: {selected_rep1_display}\n情報が見つかりません。")
            except Exception as e:
                logger.warning("Không thể chỉnh sửa tin nhắn cho REP2 (không có thông tin, ID tin nhắn: %s): %s", query.message.message_id, e)
                await query.message.reply_text(f"選択されました: {selected_rep1_display}\n情報が見つかりません。")

    elif current_level == LEVEL_REP2:
        # Xử lý cấp độ Rep2 (kết thúc)
        final_rep3_text = "情報が見つかりません。"
        for row in DATA_TABLE:
            if get_or_create_id(row["Key"]) == selected_key_id and \
               get_or_create_id(row["Rep1"]) == selected_rep1_id and \
               get_or_create_id(row["Rep2"]) == selected_rep2_id:
                final_rep3_text = row["Rep3"]
                break
        
        user_telegram_id = query.from_user.id
        user_full_name = query.from_user.full_name or f"User ID: {user_telegram_id}"
        message_to_channel = f"コード: {final_rep3_text}\nユーザー: {user_full_name}\nID: `{user_telegram_id}`"

        try:
            await query.edit_message_text(text=f"あなたの部屋番号: {final_rep3_text}")
            logger.info(f"Đã gửi Rep3 đến người dùng {user_telegram_id}.")
        except Exception as e:
            logger.warning("Không thể chỉnh sửa tin nhắn (gửi Rep3, ID tin nhắn: %s): %s", query.message.message_id, e)
            await query.message.reply_text(text=f"あなたの部屋番号: {final_rep3_text}")
        
        instruction_message_for_user = ""
        user_is_member_of_channel = False 

        if CHANNEL_CHAT_ID: 
            try:
                chat_member_status = await context.bot.get_chat_member(chat_id=CHANNEL_CHAT_ID, user_id=user_telegram_id)
                
                if chat_member_status.status in ['member', 'creator', 'administrator', 'restricted']:
                    instruction_message_for_user = POST_CODE_ALREADY_MEMBER_MESSAGE_JP
                    user_is_member_of_channel = True
                    logger.info(f"Người dùng {user_telegram_id} đã là thành viên kênh {CHANNEL_CHAT_ID}. Bỏ qua nỗ lực thêm.")
                else:
                    await context.bot.set_chat_member(
                        chat_id=CHANNEL_CHAT_ID,
                        user_id=user_telegram_id,
                        status='member'
                    )
                    logger.info(f"Đã cố gắng thêm người dùng {user_telegram_id} vào kênh {CHANNEL_CHAT_ID}.")
                    instruction_message_for_user = POST_CODE_SUCCESS_MESSAGE_JP
                    user_is_member_of_channel = True

            except BadRequest as e: 
                if "user not found" in e.message.lower() or "user is a bot" in e.message.lower():
                    logger.warning(f"BadRequest khi thêm người dùng {user_telegram_id} vào kênh {CHANNEL_CHAT_ID}: {e.message}")
                    instruction_message_for_user = f"{POST_CODE_FAIL_MESSAGE_JP} (Không thể thêm người dùng)"
                elif "user is already a member of the chat" in e.message.lower():
                    instruction_message_for_user = POST_CODE_ALREADY_MEMBER_MESSAGE_JP
                    user_is_member_of_channel = True
                    logger.info(f"Người dùng {user_telegram_id} đã là thành viên kênh {CHANNEL_CHAT_ID}. (Bắt được BadRequest)")
                else:
                    logger.error(f"Lỗi BadRequest cụ thể khi thêm người dùng {user_telegram_id} vào kênh {CHANNEL_CHAT_ID}: {e}")
                    instruction_message_for_user = f"{POST_CODE_FAIL_MESSAGE_JP} (Lỗi: {e.message})"
                
                if not user_is_member_of_channel: 
                    try:
                        invite_link_object = await context.bot.create_chat_invite_link(chat_id=CHANNEL_CHAT_ID, member_limit=1)
                        instruction_message_for_user += f"\n\nThay vào đó, hãy tham gia thủ công qua liên kết này: <a href='{invite_link_object.invite_link}'>Tham gia kênh</a>"
                    except Exception as link_e:
                        logger.error(f"Không thể tạo liên kết mời: {link_e}")
                        instruction_message_for_user += "\n\n(Không thể tạo liên kết)"

            except TelegramError as e: 
                logger.error(f"Lỗi API Telegram khi thêm người dùng {user_telegram_id} vào kênh {CHANNEL_CHAT_ID}: {e}")
                instruction_message_for_user = f"{POST_CODE_FAIL_MESSAGE_JP} (Lỗi: {e.message})"
                try:
                    invite_link_object = await context.bot.create_chat_invite_link(chat_id=CHANNEL_CHAT_ID, member_limit=1)
                    instruction_message_for_user += f"\n\nThay vào đó, hãy tham gia thủ công qua liên kết này: <a href='{invite_link_object.invite_link}'>Tham gia kênh</a>"
                except Exception as link_e:
                    logger.error(f"Không thể tạo liên kết mời: {link_e}")
                    instruction_message_for_user += "\n\n(Không thể tạo liên kết)"

            except Exception as e: 
                logger.error(f"Lỗi chung khi thêm người dùng {user_telegram_id} vào kênh {CHANNEL_CHAT_ID}: {e}")
                instruction_message_for_user = f"{POST_CODE_FAIL_MESSAGE_JP} (Lỗi chung: {e})"
                try:
                    invite_link_object = await context.bot.create_chat_invite_link(chat_id=CHANNEL_CHAT_ID, member_limit=1)
                    instruction_message_for_user += f"\n\nThay vào đó, hãy tham gia thủ công qua liên kết này: <a href='{invite_link_object.invite_link}'>Tham gia kênh</a>"
                except Exception as link_e:
                    logger.error(f"Không thể tạo liên kết mời: {link_e}")
                    instruction_message_for_user += "\n\n(Không thể tạo liên kết)"
            
            try:
                await context.bot.send_message(
                    chat_id=CHANNEL_CHAT_ID,
                    text=message_to_channel,
                    parse_mode='Markdown'
                )
                logger.info(f"Đã gửi mã '{final_rep3_text}' cho người dùng {user_telegram_id} đến kênh {CHANNEL_CHAT_ID}.")

                if user_is_member_of_channel: 
                    # Kích hoạt việc lên lịch xóa, trừ khi user_telegram_id là ADMIN_CHAT_ID
                    asyncio.create_task(schedule_kick_user(context, CHANNEL_CHAT_ID, user_telegram_id))
                    logger.info(f"Người dùng {user_telegram_id} đã được lên lịch kick khỏi kênh {CHANNEL_CHAT_ID}.")

            except Exception as e:
                logger.error(f"Không gửi được tin nhắn đến kênh {CHANNEL_CHAT_ID}: {e}")
                instruction_message_for_user += "\n\n(Không thể gửi số đến kênh.)"
        else:
            logger.warning("CHANNEL_CHAT_ID chưa được đặt. Bỏ qua việc thêm người dùng vào kênh và gửi tin nhắn đến kênh.")
            instruction_message_for_user = POST_CODE_NO_CONFIG_MESSAGE_JP
            
        full_instruction_and_wait_text = f"{instruction_message_for_user}\n\n{WAIT_TIME_MESSAGE_JP}"
        
        try:
            await query.message.reply_text(text=full_instruction_and_wait_text, parse_mode='HTML')
            logger.info(f"Đã gửi hướng dẫn cuối cùng cho người dùng {user_telegram_id}.")
        except Exception as e:
            logger.error("Không gửi được tin nhắn hướng dẫn cuối cùng cho người dùng: %s", e)
            pass

    else:
        try:
            await query.edit_message_text(text="不明な操作です。")
        except Exception as e:
            logger.warning("Không thể chỉnh sửa tin nhắn (thao tác không xác định, ID tin nhắn: %s): %s", query.message.message_id, e)
            await query.message.reply_text("不明な操作です。")


# --- Flask Endpoints ---
# ... (giữ nguyên các phần khác) ...

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
                logger.warning("Nhận được payload JSON trống hoặc không hợp lệ từ webhook.")
                return "Yêu cầu không hợp lệ", 400

            update = Update.de_json(json_data, application.bot)
            
            # --- ĐOẠN CODE ĐÃ SỬA: Chờ đợi process_update hoàn thành ---
            # Trở lại cách chờ đợi trực tiếp process_update.
            # Lý do: Trong môi trường Render, việc bắn tác vụ đi có thể khiến
            # event loop bị đóng trước khi tác vụ kịp hoàn thành,
            # hoặc nó gặp vấn đề với việc quản lý thread/context.
            # Việc chờ đợi trực tiếp sẽ đảm bảo tác vụ hoàn thành trước khi response được gửi.
            await application.process_update(update)
            
            logger.info("Đã xử lý bản cập nhật Telegram thành công.")
            return "ok", 200
        except Exception as e:
            logger.error("Lỗi khi xử lý bản cập nhật Telegram: %s", e)
            # Luôn trả về 200 OK để Telegram không gửi lại nhiều lần
            return "ok", 200 
    return "Phương thức không được phép", 405

# ... (giữ nguyên các phần khác) ...

@flask_app.route("/health", methods=["GET"])
def health_check():
    """Endpoint cho kiểm tra sức khỏe của Render."""
    return jsonify({"status": "ok"})


# --- Trình xử lý lỗi toàn cục cho Application ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ngoại lệ khi xử lý bản cập nhật:", exc_info=context.error)
    if ADMIN_CHAT_ID:
        try:
            error_message_for_admin = f"❌ Lỗi Bot ❌\n\nUpdate: {update}\n\nLỗi: {context.error}"
            # Cắt bớt tin nhắn nếu quá dài
            if len(error_message_for_admin) > 4000:
                error_message_for_admin = error_message_for_admin[:3900] + "\n... (đã cắt bớt)"
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=error_message_for_admin)
            logger.info(f"Đã gửi thông báo lỗi đến ADMIN_CHAT_ID: {ADMIN_CHAT_ID}")
        except Exception as send_error:
            logger.error(f"Không gửi được thông báo lỗi cho admin: {send_error}")


# --- Logic ứng dụng chính (Điểm khởi đầu) ---
async def run_full_application():
    global application

    TOKEN = os.getenv("BOT_TOKEN")
    BASE_WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    PORT = int(os.getenv("PORT", 8443)) # Render sẽ dùng 10000

    if not TOKEN:
        logger.critical("Biến môi trường BOT_TOKEN chưa được đặt. Đang thoát.")
        raise ValueError("Biến môi trường BOT_TOKEN chưa được đặt.")
    if not BASE_WEBHOOK_URL:
        logger.critical("Biến môi trường WEBHOOK_URL chưa được đặt. Đang thoát.")
        raise ValueError("Biến môi trường WEBHOOK_URL chưa được đặt.")

    FULL_WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_button_press))
    application.add_error_handler(error_handler)

    await application.initialize()

    logger.info("Đang đặt webhook Telegram thành: %s", FULL_WEBHOOK_URL)
    try:
        await application.bot.set_webhook(url=FULL_WEBHOOK_URL)
        logger.info("Đã đặt webhook Telegram thành công.")
    except Exception as e:
        logger.error("Lỗi khi đặt webhook Telegram: %s", e)
        raise SystemExit("Không đặt được webhook. Đang thoát.")

    logger.info("Ứng dụng Flask (qua Hypercorn) đang lắng nghe trên cổng %d", PORT)
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    
    await serve(flask_app, config)


if __name__ == '__main__':
    try:
        asyncio.run(run_full_application())
    except Exception as e:
        logger.critical("Ứng dụng đã dừng do lỗi không được xử lý: %s", e)
