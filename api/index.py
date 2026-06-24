import os
import logging
import certifi
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, status
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from motor.motor_asyncio import AsyncIOMotorClient

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURATION (Loaded from Vercel Environment Variables) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MONGO_URI = os.environ.get("MONGO_URI", "").strip()

try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "0").strip())
except ValueError:
    ADMIN_ID = 0

# Globals
ptb_app = None
mappings_col = None

async def relay_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggers when a normal user messages the support bot."""
    if not ADMIN_ID: 
        logger.error("Admin ID not configured.")
        return

    user = update.effective_user
    header = f"👤 <b>{user.full_name}</b> (@{user.username})\n🆔 #USER_{user.id}"
    
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=header, parse_mode="HTML")
        admin_msg = await update.message.copy(chat_id=ADMIN_ID)
        
        # PERMANENT SAVE: Insert into a SAFE, SEPARATE MongoDB collection
        await mappings_col.update_one(
            {"_id": admin_msg.message_id}, 
            {"$set": {"user_id": user.id}}, 
            upsert=True
        )
        await update.message.reply_text("Your message has been sent to the admin. Please wait for a reply.")
    except Exception as e:
        logger.error(f"Relay Error: {e}")

async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggers when YOU reply to a forwarded message."""
    replied_msg = update.message.reply_to_message
    if not replied_msg: return 
        
    try:
        # Look up the user ID from the database
        record = await mappings_col.find_one({"_id": replied_msg.message_id})
        
        if record:
            target_user_id = record["user_id"]
            await update.message.copy(chat_id=target_user_id)
            await update.message.reply_text("✅ Reply delivered.")
        else:
            await update.message.reply_text("❌ User ID not found in MongoDB.")
    except Exception as e:
        logger.error(f"Reply Error: {e}")
        await update.message.reply_text(f"❌ Failed to send: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global ptb_app, mappings_col
    
    # 1. Connect to MongoDB safely
    client = AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client.support_bot_db # Using a separate database name for ultimate safety
    mappings_col = db.relay_message_mappings

    # 2. Start Telegram Bot properly for Serverless
    ptb_app = Application.builder().token(BOT_TOKEN).build()
    ptb_app.add_handler(MessageHandler(filters.ALL & ~filters.User(ADMIN_ID), relay_to_admin))
    ptb_app.add_handler(MessageHandler(filters.ALL & filters.User(ADMIN_ID) & filters.REPLY, reply_to_user))
    
    await ptb_app.initialize()
    await ptb_app.start() # <-- CRITICAL FIX FOR VERCEL
    yield
    await ptb_app.stop()  # <-- CRITICAL FIX FOR VERCEL
    await ptb_app.shutdown()
    client.close()

app = FastAPI(lifespan=lifespan)

@app.post("/api/webhook")
async def telegram_webhook(req: Request):
    """Listens for Telegram messages."""
    try:
        data = await req.json()
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

@app.get("/")
def read_root():
    return {"status": "Relay Bot is Active"}
