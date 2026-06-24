import os
import logging
import certifi
from fastapi import FastAPI, Request, Response, status
from telegram import Update, Bot
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

app = FastAPI()

# Safe Lazy Initialization (Exactly like your OSINT bot)
bot = None
mappings_col = None

def init_services():
    global bot, mappings_col
    if bot is None:
        bot = Bot(token=BOT_TOKEN)
        client = AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        db = client.support_bot_db 
        mappings_col = db.relay_message_mappings

async def process_message(update: Update):
    """Processes the message synchronously so Vercel does not freeze."""
    if not update.message: return

    user = update.effective_user
    user_id = user.id

    # --- 1. ADMIN REPLYING ---
    if user_id == ADMIN_ID and update.message.reply_to_message:
        replied_msg = update.message.reply_to_message
        
        # Look up original sender in MongoDB
        record = await mappings_col.find_one({"_id": replied_msg.message_id})
        
        if record:
            target_user_id = record["user_id"]
            try:
                await update.message.copy(chat_id=target_user_id)
                await update.message.reply_text("✅ Reply delivered.")
            except Exception as e:
                logger.error(f"Reply Error: {e}")
                await update.message.reply_text(f"❌ Failed to send: {e}")
        else:
            await update.message.reply_text("❌ User ID not found in MongoDB.")
        return # Stop processing

    # --- 2. NORMAL USER MESSAGING THE BOT ---
    if user_id != ADMIN_ID:
        if not ADMIN_ID: 
            logger.error("Admin ID not configured.")
            return
            
        header = f"👤 <b>{user.full_name}</b> (@{user.username})\n🆔 #USER_{user.id}"
        
        try:
            # Send header, then forward actual message
            await bot.send_message(chat_id=ADMIN_ID, text=header, parse_mode="HTML")
            admin_msg = await update.message.copy(chat_id=ADMIN_ID)
            
            # Save mapping permanently to MongoDB
            await mappings_col.update_one(
                {"_id": admin_msg.message_id}, 
                {"$set": {"user_id": user.id}}, 
                upsert=True
            )
            await update.message.reply_text("Your message has been sent to the admin. Please wait for a reply.")
        except Exception as e:
            logger.error(f"Relay Error: {e}")

@app.post("/api/webhook")
async def telegram_webhook(req: Request):
    """Listens for Telegram messages."""
    try:
        init_services()
        data = await req.json()
        update = Update.de_json(data, bot)
        
        # Vercel is now FORCED to wait until this finishes
        await process_message(update) 
        
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

@app.get("/")
def read_root():
    return {"status": "Relay Bot is Active"}
