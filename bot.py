import logging
import os
import asyncio
import pandas as pd

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from telegram.error import TelegramError, BadRequest

from flask import Flask, request, jsonify # Giữ Flask cho health check và có thể là các API khác nếu cần
from hypercorn.asyncio import serve
from hypercorn.config import Config

# --- Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask app giờ chỉ dùng cho health check
flask_app = Flask(__name__)
# Ứng dụng Telegram Bot
application = None 

WEBHOOK_PATH = "/webhook_telegram"

# --- Bot Data Structure ---
EXCEL_FILE_PATH = "rep.xlsx"
DATA_TABLE = []

STRING_TO_ID_MAP = {}
ID_TO_STRING_MAP = {}
next_id = 0

def get_or_create_id(text: str) -> int:
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
    logger.info(f"Successfully loaded data from {EXCEL_FILE_PATH}")

    for row in DATA_TABLE:
        get_or_create_id(row["Key"])
        get_or_create_id(row["Rep1"])
        get_or_create_id(row["Rep2"])
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

CHANNEL_CHAT_ID = os.getenv("TELEGRAM_CHANNEL_CHAT_ID", "") 
logger.info(f"Loaded CHANNEL_CHAT_ID from environment: '{CHANNEL_CHAT_ID}'")

ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
if ADMIN_CHAT_ID:
    logger.info(f"Loaded ADMIN_CHAT_ID from environment: '{ADMIN_CHAT_ID}'")
else:
    logger.warning("ADMIN_CHAT_ID environment variable not set. Admin might be kicked if they are also a user.")

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
    logger.info(f"Sending initial welcome message to user ID: {user_telegram_id}")
    await update_object.message.reply_text(INITIAL_WELCOME_MESSAGE_JP, reply_markup=reply_markup, parse_mode='Markdown')


# --- Telegram Bot Handlers ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_telegram_id = update.message.from_user.id

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
    user_telegram_id = update.message.from_user.id
    
    if user_telegram_id in welcomed_users:
        welcomed_users.remove(user_telegram_id)
        logger.info(f"User {user_telegram_id} removed from welcomed_users (session reset by /start).")
    
    await send_initial_key_buttons(update)
    welcomed_users.add(user_telegram_id)


async def schedule_kick_user(context: ContextTypes.DEFAULT_TYPE, channel_chat_id: str, user_id_to_kick: int):
    if ADMIN_CHAT_ID and str(user_id_to_kick) == ADMIN_CHAT_ID:
        logger.info(f"User {user_id_to_kick} is admin ({ADMIN_CHAT_ID}), skipping kick from channel {channel_chat_id}.")
        return

    logger.info(f"Scheduling kick for user {user_id_to_kick} from channel {channel_chat_id} in {KICK_DELAY_SECONDS} seconds.")
    await asyncio.sleep(KICK_DELAY_SECONDS)
    
    try:
        await context.bot.unban_chat_member(
            chat_id=channel_chat_id,
            user_id=user_id_to_kick
        )
        logger.info(f"Successfully kicked user {user_id_to_kick} from channel {channel_chat_id}.")
        
    except TelegramError as e:
        logger.error(f"Telegram API Error kicking user {user_id_to_kick} from channel {channel_chat_id}: {e}")
    except Exception as e:
        logger.error(f"General Error kicking user {user_id_to_kick} from channel {channel_chat_id}: {e}")


async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data_parts = (query.data.split(':') + ['', '', '', ''])[:4]
    current_level = data_parts[0]
    selected_key_id = int(data_parts[1]) if data_parts[1] else -1
    selected_rep1_id = int(data_parts[2]) if data_parts[2] else -1
    selected_rep2_id = int(data_parts[3]) if data_parts[3] else -1

    selected_key_display = ID_TO_STRING_MAP.get(selected_key_id, f"ID_Key:{selected_key_id}")
    selected_rep1_display = ID_TO_STRING_MAP.get(selected_rep1_id, f"ID_Rep1:{selected_rep1_id}") if selected_rep1_id != -1 else ''
    selected_rep2_display = ID_TO_STRING_MAP.get(selected_rep2_id, f"ID_Rep2:{selected_rep2_id}") if selected_rep2_id != -1 else ''

    logger.info(f"Button press: Level={current_level}, Key_ID={selected_key_id} ({selected_key_display}), Rep1_ID={selected_rep1_id} ({selected_rep1_display}), Rep2_ID={selected_rep2_id} ({selected_rep2_display})")

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
            
            try:
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
            logger.info(f"Sent Rep3 to user {user_telegram_id}.")
        except Exception as e:
            logger.warning("Could not edit message (sending Rep3, message ID: %s): %s", query.message.message_id, e)
            await query.message.reply_text(text=f"あなたの部屋番号: {final_rep3_text}")
        
        instruction_message_for_user = ""
        user_is_member_of_channel = False 

        if CHANNEL_CHAT_ID: 
            try:
                chat_member_status = await context.bot.get_chat_member(chat_id=CHANNEL_CHAT_ID, user_id=user_telegram_id)
                
                if chat_member_status.status in ['member', 'creator', 'administrator', 'restricted']:
                    instruction_message_for_user = POST_CODE_ALREADY_MEMBER_MESSAGE_JP
                    user_is_member_of_channel = True
                    logger.info(f"User {user_telegram_id} is already a member of channel {CHANNEL_CHAT_ID}. Skipping add attempt.")
                else:
                    await context.bot.set_chat_member(
                        chat_id=CHANNEL_CHAT_ID,
                        user_id=user_telegram_id,
                        status='member'
                    )
                    logger.info(f"Attempted to add user {user_telegram_id} to channel {CHANNEL_CHAT_ID}.")
                    instruction_message_for_user = POST_CODE_SUCCESS_MESSAGE_JP
                    user_is_member_of_channel = True

            except BadRequest as e: 
                if "user not found" in e.message.lower() or "user is a bot" in e.message.lower():
                    logger.warning(f"BadRequest when adding user {user_telegram_id} to channel {CHANNEL_CHAT_ID}: {e.message}")
                    instruction_message_for_user = f"{POST_CODE_FAIL_MESSAGE_JP} (ユーザーを追加できませんでした)"
                elif "user is already a member of the chat" in e.message.lower():
                    instruction_message_for_user = POST_CODE_ALREADY_MEMBER_MESSAGE_JP
                    user_is_member_of_channel = True
                    logger.info(f"User {user_telegram_id} is already a member of channel {CHANNEL_CHAT_ID}. (Caught BadRequest)")
                else:
                    logger.error(f"Specific BadRequest error when adding user {user_telegram_id} to channel {CHANNEL_CHAT_ID}: {e}")
                    instruction_message_for_user = f"{POST_CODE_FAIL_MESSAGE_JP} (エラー: {e.message})"
                
                if not user_is_member_of_channel: 
                    try:
                        invite_link_object = await context.bot.create_chat_invite_link(chat_id=CHANNEL_CHAT_ID, member_limit=1)
                        instruction_message_for_user += f"\n\n代わりに、このリンクから手動で参加してください: <a href='{invite_link_object.invite_link}'>チャンネルに参加</a>"
                    except Exception as link_e:
                        logger.error(f"Could not create invite link: {link_e}")
                        instruction_message_for_user += "\n\n(リンクを作成できませんでした)"

            except TelegramError as e: 
                logger.error(f"Telegram API Error adding user {user_telegram_id} to channel {CHANNEL_CHAT_ID}: {e}")
                instruction_message_for_user = f"{POST_CODE_FAIL_MESSAGE_JP} (エラー: {e.message})"
                try:
                    invite_link_object = await context.bot.create_chat_invite_link(chat_id=CHANNEL_CHAT_ID, member_limit=1)
                    instruction_message_for_user += f"\n\n代わりに、このリンクから手動で参加してください: <a href='{invite_link_object.invite_link}'>チャンネルに参加</a>"
                except Exception as link_e:
                    logger.error(f"Could not create invite link: {link_e}")
                    instruction_message_for_user += "\n\n(リンクを作成できませんでした)"

            except Exception as e: 
                logger.error(f"General Error adding user {user_telegram_id} to channel {CHANNEL_CHAT_ID}: {e}")
                instruction_message_for_user = f"{POST_CODE_FAIL_MESSAGE_JP} (一般的なエラー: {e})"
                try:
                    invite_link_object = await context.bot.create_chat_invite_link(chat_id=CHANNEL_CHAT_ID, member_limit=1)
                    instruction_message_for_user += f"\n\n代わりに、このリンクから手動で参加してください: <a href='{invite_link_object.invite_link}'>チャンネルに参加</a>"
                except Exception as link_e:
                    logger.error(f"Could not create invite link: {link_e}")
                    instruction_message_for_user += "\n\n(リンクを作成できませんでした)"
            
            try:
                await context.bot.send_message(
                    chat_id=CHANNEL_CHAT_ID,
                    text=message_to_channel,
                    parse_mode='Markdown'
                )
                logger.info(f"Sent code '{final_rep3_text}' for user {user_telegram_id} to channel {CHANNEL_CHAT_ID}.")

                if user_is_member_of_channel: 
                    # Kích hoạt việc lên lịch xóa, trừ khi user_telegram_id là ADMIN_CHAT_ID
                    asyncio.create_task(schedule_kick_user(context, CHANNEL_CHAT_ID, user_telegram_id))
                    logger.info(f"User {user_telegram_id} scheduled for kick from channel {CHANNEL_CHAT_ID}.")

            except Exception as e:
                logger.error(f"Failed to send message to channel {CHANNEL_CHAT_ID}: {e}")
                instruction_message_for_user += "\n\n(チャンネルに番号を送信できませんでした。)"
        else:
            logger.warning("CHANNEL_CHAT_ID is not set. Skipping adding user to channel and sending message to channel.")
            instruction_message_for_user = POST_CODE_NO_CONFIG_MESSAGE_JP
            
        full_instruction_and_wait_text = f"{instruction_message_for_user}\n\n{WAIT_TIME_MESSAGE_JP}"
        
        try:
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


# --- Flask Endpoint cho Health Check (và có thể các API khác) ---
@flask_app.route("/health", methods=["GET"])
def health_check():
    """Endpoint for Render's health checks."""
    return jsonify({"status": "ok"})


# --- Global Error Handler cho Application ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if ADMIN_CHAT_ID:
        try:
            error_message_for_admin = f"❌ Bot Error ❌\n\nUpdate: {update}\n\nError: {context.error}"
            if len(error_message_for_admin) > 4000:
                error_message_for_admin = error_message_for_admin[:3900] + "\n... (truncated)"
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=error_message_for_admin)
            logger.info(f"Error notification sent to ADMIN_CHAT_ID: {ADMIN_CHAT_ID}")
        except Exception as send_error:
            logger.error(f"Failed to send error notification to admin: {send_error}")


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

    # THAY ĐỔI LỚN TẠI ĐÂY:
    # Hypercorn sẽ phục vụ cả ứng dụng Telegram Bot (là một ASGI app)
    # và Flask app (cho health check).
    # Chúng ta cần một ứng dụng ASGI chính mà Hypercorn có thể chạy.
    # python-telegram-bot có thể tự tạo một ASGI application.

    # Tuy nhiên, cách tốt nhất để kết hợp Flask và PTB ASGI app là dùng một ASGI wrapper.
    # Nhưng vì bạn đã có Flask đang chạy trên cùng cổng, chúng ta sẽ để Flask là chính
    # và để PTB webhook chạy bên trong Flask.
    # Lỗi 'Event loop is closed' vẫn có thể xảy ra do cách Render quản lý process.

    # Cách tiếp cận trước đó của bạn (Flask nhận webhook và gọi process_update)
    # là cách phổ biến và thường hoạt động. Lỗi bạn gặp có thể do:
    # 1. Quản lý event loop của Render: Render có thể đóng process hoặc event loop
    #    mà không báo trước, gây ra lỗi.
    # 2. Xung đột ngầm giữa Flask/Hypercorn và PTB's asyncio usage.

    # Phương án an toàn hơn nếu muốn dùng run_webhook() là chạy PTB Application
    # như một ứng dụng độc lập trên một cổng khác, hoặc sử dụng một ASGI gateway
    # để định tuyến các request đến đúng ứng dụng.
    # Tuy nhiên, trên Render, việc chạy nhiều dịch vụ trên cùng một cổng phức tạp hơn.

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
    
    # Để giải quyết lỗi 'Event loop is closed', chúng ta sẽ cố gắng sử dụng
    # `application.run_webhook()` và phục vụ nó bằng Hypercorn.
    # Điều này sẽ bỏ qua Flask cho webhook xử lý.
    # Tuy nhiên, nếu bạn vẫn muốn /health check qua Flask, chúng ta cần một cơ chế khác.
    # Ví dụ: chạy Flask trên một cổng khác hoặc tạo một ASGI app tổng hợp.

    # Với lỗi bạn đang gặp, giải pháp khả dĩ nhất là **chuyển sang chỉ chạy PTB's webhook app**
    # nếu health check không quá quan trọng hoặc có thể được xử lý bởi Render.

    # Option 1: Chỉ chạy PTB Webhook (Nếu bạn không cần Flask API khác)
    # await serve(application.webhooks_app, config)
    # Option 2: Chạy Flask với webhook handler như bạn đã có (có thể gặp lỗi event loop)

    # Vấn đề là bạn đang muốn cả Flask và PTB cùng chia sẻ một Hypercorn server.
    # Điều này cần một ASGI wrapper như `Starlette` hoặc `FastAPI` để định tuyến request.
    # Nhưng để tối ưu và giảm thiểu lỗi, chúng ta sẽ thử **chạy riêng ứng dụng webhook của PTB**.
    # Điều này có nghĩa là endpoint `/webhook_telegram` sẽ được xử lý bởi PTB,
    # còn `/health` của Flask sẽ không thể truy cập được trên cùng cổng.

    # Để giữ health check, cách tốt nhất là có một ASGI app chung.
    # Nhưng để giảm lỗi, chúng ta sẽ tập trung vào việc xử lý webhook.

    # KHÔNG THỂ CHẠY CẢ FLASK VÀ PTB APPLICATION TRỰC TIẾP CÙNG LÚC TRÊN CÙNG CỔNG
    # BẰNG hypercorn.asyncio.serve() MÀ KHÔNG CÓ ASGI DISPATCHER.

    # Lỗi `RuntimeError('Event loop is closed')` thường xuất hiện khi Hypercorn
    # hoặc Flask cố gắng sử dụng một event loop đã bị đóng bởi một phần khác
    # của ứng dụng (hoặc bởi chính môi trường runtime của Render).
    # Điều này cho thấy có sự xung đột trong cách event loop được quản lý.

    # Giải pháp tối ưu nhất cho vấn đề này là để `python-telegram-bot` tự quản lý webhook
    # hoàn toàn, và nếu cần health check, hãy đặt nó trên một cổng khác hoặc dùng một công cụ khác.

    # Tuy nhiên, nếu bạn muốn giữ Flask cho health check và các mục đích khác,
    # chúng ta phải quay lại cách cũ (Flask nhận webhook và gọi process_update),
    # nhưng cố gắng làm cho nó robust hơn hoặc chấp nhận lỗi đó là do môi trường.

    # NHỮNG THAY ĐỔI HIỆN TẠI:
    # 1. Loại bỏ hàm `telegram_webhook` trong Flask.
    # 2. Để PTB Application tự chạy webhook listener của nó.
    # 3. Để Hypercorn phục vụ PTB Application (là một ASGI app).
    # 4. Health check của Flask sẽ không hoạt động trên cùng cổng nữa.

    # Để giải quyết lỗi `RuntimeError('Event loop is closed')` một cách triệt để
    # khi chạy cả Flask và python-telegram-bot webhooks trên **cùng một cổng**
    # với Hypercorn, chúng ta cần một **ASGI Router/Dispatcher**.

    # Một cách tiếp cận là sử dụng một thư viện ASGI nhỏ như `Starlette` hoặc `FastAPI`
    # làm bộ định tuyến chính, sau đó mount Flask app và PTB webhook app vào đó.
    # Tuy nhiên, việc này sẽ làm tăng độ phức tạp.

    # PHƯƠNG ÁN ĐƠN GIẢN HƠN: Chạy PTB Webhook Application.
    # Nếu bạn chỉ cần health check thì Render có thể kiểm tra cổng của app.
    # Bạn có thể bỏ `flask_app` và chỉ chạy `application.webhooks_app`

    # Thay đổi lại `run_full_application`
    # Chạy `application.run_webhook()` sẽ khởi tạo một ASGI application
    # mà bạn có thể truyền vào `hypercorn.asyncio.serve`.
    # Đây là cách chính thống để chạy PTB webhooks với ASGI.
    
    # Chúng ta sẽ truyền `application.webhooks_app` vào `serve`
    # và bỏ qua `flask_app` trong phần chính để tránh xung đột event loop.
    # Health check vẫn có thể hoạt động nếu Render kiểm tra endpoint mặc định của webhook.

    await serve(application.webhooks_app, config)
    # Lưu ý: Nếu bạn muốn chạy Flask cho /health và các API khác,
    # bạn cần một ASGI dispatcher để định tuyến requests.
    # Ví dụ với FastAPI/Starlette:
    # app = FastAPI()
    # @app.get("/health")
    # async def health_check_api(): return {"status": "ok"}
    # app.mount(WEBHOOK_PATH, WSGIMiddleware(flask_app)) # Không, PTB là ASGI.
    # app.mount(WEBHOOK_PATH, application.webhooks_app) # Đây là cách đúng
    # await serve(app, config)
    # Nhưng điều này phức tạp hơn. Hãy thử cách tối giản nhất để loại bỏ lỗi.


if __name__ == '__main__':
    try:
        asyncio.run(run_full_application())
    except Exception as e:
        logger.critical("Application stopped due to an unhandled error: %s", e)
