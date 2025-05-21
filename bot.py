from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import os
from flask import Flask, request, jsonify
import asyncio
from hypercorn.asyncio import serve
from hypercorn.config import Config
import logging

# --- Configuration ---
# Set up basic logging for better visibility
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize Flask app
flask_app = Flask(__name__)

# Fixed webhook path - consistent across Render and Telegram setWebhook
WEBHOOK_PATH = "/webhook_telegram"

# --- Bot Reply Data ---
TEMPLATE_REPLIES = {
    "東京都": "江東区\n江戸川区\n足立区",
    "江東区": "亀戸6-12-7 第2伸光マンション\n亀戸6丁目47-2 ウィンベル亀戸(MONTHLY亀戸1)",
    "江戸川区": "西小岩1丁目30-11",
}

# --- Telegram Bot Handlers ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responds to user messages based on TEMPLATE_REPLIES."""
    if update.message and update.message.text: # Safely check for message and text
        text = update.message.text.lower()
        reply = TEMPLATE_REPLIES.get(text, "何を言っているのか分かりません。もう一度お試しください。")
        await update.message.reply_text(reply)
    else:
        logger.warning("Received an update without message text: %s", update)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    await update.message.reply_text("三上はじめにへようこそ")

# --- Flask Endpoints ---
@flask_app.route("/health", methods=["GET"])
def health_check():
    """Endpoint for Render's health checks."""
    return jsonify({"status": "ok"})

# The Telegram webhook endpoint will be defined in main() once the application object is ready.
# This ensures 'application' is available when the route is registered.

# --- Main Application Logic ---
async def main():
    """Main function to initialize and run the Telegram bot and Flask server."""
    # Load environment variables
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

    # Build the Telegram Application
    application = ApplicationBuilder().token(TOKEN).build()

    # Initialize the application for webhook mode
    # This must be called before setting handlers or doing API calls
    await application.initialize()

    # Add handlers for commands and messages
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # --- Define Flask Webhook Route Dynamically ---
    # Define the webhook route *after* 'application' is initialized and ready.
    # This ensures 'application.bot' is available when process_update is called.
    @flask_app.route(WEBHOOK_PATH, methods=["POST"])
    async def telegram_webhook():
        """Handles incoming Telegram updates via webhook."""
        if request.method == "POST":
            try:
                # Ensure the JSON payload is correctly parsed
                json_data = request.get_json(force=True)
                if not json_data:
                    logger.warning("Received empty or invalid JSON payload from webhook.")
                    return "Bad Request", 400

                update = Update.de_json(json_data, application.bot)
                await application.process_update(update)
                return "ok", 200 # Return 200 OK for successful processing
            except Exception as e:
                logger.error("Error processing Telegram update: %s", e)
                # It's good practice to return 200 even on internal errors
                # to prevent Telegram from retrying endlessly.
                return "ok", 200
        return "Method Not Allowed", 405 # For non-POST requests

    # --- Set Telegram Webhook ---
    logger.info("Setting Telegram webhook to: %s", FULL_WEBHOOK_URL)
    try:
        await application.bot.set_webhook(url=FULL_WEBHOOK_URL)
        logger.info("Telegram webhook set successfully.")
    except Exception as e:
        logger.error("Error setting Telegram webhook: %s", e)
        # Depending on criticality, you might want to exit here if webhook setup is vital.
        # raise # Uncomment to stop execution if webhook fails to set

    # --- Run Hypercorn Server ---
    logger.info("Flask app listening on port %d", PORT)
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]

    # Schedule Hypercorn to run as a background task within the same event loop.
    server_task = asyncio.create_task(serve(flask_app, config))

    # Keep the main event loop running by awaiting the server task indefinitely.
    # This is crucial as it ensures the event loop doesn't close prematurely.
    await server_task

# --- Entry Point ---
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical("Application stopped due to an unhandled error: %s", e)
