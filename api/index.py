import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, status
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from pymongo import MongoClient

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURATION (Loaded from Vercel Environment Variables) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except ValueError:
    logger.error("ADMIN_ID environment variable must be a valid integer.")
    ADMIN_ID = 0

# Initialize MongoDB client globally for connection pooling
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["telegram_bot"]
mappings_col = db["message_mappings"]

# Initialize Telegram Application
ptb_app = Application.builder().token(BOT_TOKEN).build()

async def relay_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forwards incoming user messages to the configured Admin ID."""
    if not ADMIN_ID:
        logger.error("Admin ID is not configured.")
        return

    user = update.effective_user
    header = f"👤 <b>{user.full_name}</b> (@{user.username})\n🆔 #USER_{user.id}"
    
    # Send identification header followed by the actual message content
    await context.bot.send_message(chat_id=ADMIN_ID, text=header, parse_mode="HTML")
    admin_msg = await update.message.copy(chat_id=ADMIN_ID)
    
    # Save message link mapping permanently to MongoDB
    try:
        mappings_col.update_one(
            {"_id": admin_msg.message_id}, 
            {"$set": {"user_id": user.id}}, 
            upsert=True
        )
    except Exception as e:
        logger.error(f"MongoDB write error: {e}")
    
    await update.message.reply_text("Your message has been sent. Please wait for a reply.")

async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes Admin replies and routes them back to the original sender."""
    replied_msg = update.message.reply_to_message
    if not replied_msg:
        return 
        
    target_user_id = None
    
    # Query original sender's ID using the replied message ID
    try:
        record = mappings_col.find_one({"_id": replied_msg.message_id})
        if record:
            target_user_id = record["user_id"]
    except Exception as e:
        logger.error(f"MongoDB read error: {e}")
    
    if target_user_id:
        try:
            await update.message.copy(chat_id=target_user_id)
            await update.message.reply_text("✅ Reply delivered.")
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to send: {e}")
    else:
        await update.message.reply_text("❌ User ID not found in MongoDB database.")

# Add operational handlers to the pipeline
ptb_app.add_handler(MessageHandler(filters.ALL & ~filters.User(ADMIN_ID), relay_to_admin))
ptb_app.add_handler(MessageHandler(filters.ALL & filters.User(ADMIN_ID) & filters.REPLY, reply_to_user))

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles startup initialization and shutdown routines within the serverless scope."""
    await ptb_app.initialize()
    yield
    await ptb_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/api/webhook")
async def telegram_webhook(req: Request):
    """Vercel serverless endpoint acting as the target for Telegram Webhooks."""
    try:
        data = await req.json()
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error handling webhook data: {e}")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
