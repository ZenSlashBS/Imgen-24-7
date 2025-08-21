import sys
import asyncio
import logging
import sqlite3
import warnings
import uuid
import os
from pathlib import Path
from threading import Lock, Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from requests.exceptions import ConnectionError, RequestException
from httpx import ReadError
import requests
import time
import re
import http.server
import socketserver

# Suppress DeprecationWarning for event loop
warnings.filterwarnings("ignore", category=DeprecationWarning, message="There is no current event loop")

# Check Python version
if sys.version_info < (3, 7):
    raise RuntimeError("Python 3.7 or higher required.")

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
CONFIG = {
    "BOT_TOKEN": os.environ.get("BOT_TOKEN", "8109103757:AAF5fYbLO24zEk7WxSJufH1hORVMdwacMog"),
    "IMG_API_URL": os.environ.get("IMG_API_URL", "https://img-gen.hazex.workers.dev/"),
    "GROUP_CHAT_ID": int(os.environ.get("GROUP_CHAT_ID", -1002659852650)),
    "VERIFY_IMAGE_URL": os.environ.get("VERIFY_IMAGE_URL", "False").lower() == "true",
    "SUPPORT_URL": os.environ.get("SUPPORT_URL", "t.me/hazexpy"),
    "ADMIN_ID": int(os.environ.get("ADMIN_ID", 7067323341)),
    "DB_FILE": os.environ.get("DB_FILE", "/app/data/users.db"),
    "DATA_DIR": os.environ.get("DATA_DIR", "/app/data"),
    "USERS_FILE": os.environ.get("USERS_FILE", "users.txt"),
    "HTTP_PORT": int(os.environ.get("PORT", 8000))
}
CONFIG["BOT_USER_ID"] = int(CONFIG["BOT_TOKEN"].split(':')[0])

# Language settings
LANGUAGE_EN = {
    "code": "en",
    "name": "English",
    "welcome": "Welcome to AI Image Generator! 📸\nUse /gen to create images.",
    "prompt": "Enter a prompt (e.g., 'A futuristic city'). ✍️",
    "dimension": "Choose dimension: 📐",
    "improve": "Enable quality? 🤗",
    "generating": "Processing... ⏳",
    "error": "Error: {} 😞",
    "success": "( • ᴗ - ) ✧ Generated! 🎉 Prompt: {}\n(´｡• ◡ •｡`) ♡ Time: {}s\n╰┈➤ Size: {} KB",
    "invalid_prompt": "Invalid prompt. ❗",
    "invalid_image_url": "Image URL invalid. 🚫",
    "yes": "✅ Yes",
    "no": "❌ No",
    "wide": "Wide (1024x576) 🖼️",
    "tall": "Tall (576x1024) 🖼️",
    "square": "Square (768x768) 🖼️",
    "num_users": "📊 User Count: {} | IDs in users.txt 📊",
    "admin_only": "This command is for admins only! 🚫",
    "broadcast_approve": "Hi there! Approve this broadcast? 📬",
    "broadcast_success": "🎉 Broadcast sent to {count} users! 📬",
    "broadcast_canceled": "❌ Broadcast canceled.",
    "broadcast_error": "❌ Invalid command. Use: /bro <message> [-<btnname>:<btnlink>] [--<imagelink>]",
}

# User states
STATE_PROMPT = "awaiting_prompt"
STATE_DIMENSION = "awaiting_dimension"
STATE_IMPROVE = "awaiting_improve"

# Thread-safe lock for in-memory operations
DATA_LOCK = Lock()
USER_STATE = {}  # User states for conversation flow

# HTTP Server for Health Check
class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Hello World!")
        logger.info("Health check request received")

def run_http_server():
    """Run HTTP server for health checks."""
    try:
        server = socketserver.TCPServer(("", CONFIG["HTTP_PORT"]), HealthCheckHandler)
        server.allow_reuse_address = True
        server.server_bind()
        server.server_activate()
        logger.info(f"HTTP server started on port {CONFIG['HTTP_PORT']}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"HTTP server error: {e}")

# Database and file handling
def manage_user_data(user_id, update_usage=None, update_topic_id=None):
    """Manage user data in SQLite database."""
    try:
        with sqlite3.connect(CONFIG["DB_FILE"]) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT usage_count, topic_id FROM users WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            
            if result:
                usage_count, topic_id = result
            else:
                cursor.execute(
                    "INSERT INTO users (user_id, usage_count, topic_id) VALUES (?, ?, ?)",
                    (user_id, 0, None)
                )
                usage_count, topic_id = 0, None
                logger.info(f"Initialized user {user_id} in database")
            
            if update_usage is not None or update_topic_id is not None:
                updates = []
                params = []
                if update_usage is not None:
                    updates.append("usage_count = ?")
                    params.append(update_usage)
                if update_topic_id is not None:
                    updates.append("topic_id = ?")
                    params.append(update_topic_id)
                params.append(user_id)
                cursor.execute(
                    f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?",
                    params
                )
                logger.info(f"Updated user {user_id}: usage_count={update_usage}, topic_id={update_topic_id}")
            
            conn.commit()
            return {"usage_count": usage_count, "topic_id": topic_id}
    except Exception as e:
        logger.error(f"User data error for user {user_id}: {e}")
        return {"usage_count": 0, "topic_id": None}

def init_db():
    """Initialize SQLite database."""
    try:
        # Ensure data directory exists
        Path(CONFIG["DATA_DIR"]).mkdir(exist_ok=True)
        with sqlite3.connect(CONFIG["DB_FILE"]) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    usage_count INTEGER DEFAULT 0,
                    topic_id INTEGER
                )
            """)
            conn.commit()
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"Database init error: {e}")

def load_users_from_file():
    """Load user IDs from users.txt into the database if not already present."""
    filename = Path(CONFIG["DATA_DIR"]) / CONFIG["USERS_FILE"]
    if not filename.exists():
        logger.info("No users.txt file found, skipping load.")
        return

    try:
        with filename.open("r", encoding="utf-8") as f:
            lines = f.readlines()

        user_ids = []
        collecting = False
        for line in lines:
            stripped = line.strip()
            if stripped == "User IDs:":
                collecting = True
                continue
            if collecting and stripped:
                try:
                    user_ids.append(int(stripped))
                except ValueError:
                    logger.warning(f"Invalid user ID in file: {stripped}")

        with sqlite3.connect(CONFIG["DB_FILE"]) as conn:
            cursor = conn.cursor()
            for user_id in user_ids:
                cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
                if not cursor.fetchone():
                    cursor.execute(
                        "INSERT INTO users (user_id, usage_count, topic_id) VALUES (?, ?, ?)",
                        (user_id, 0, None)
                    )
                    logger.info(f"Loaded user {user_id} from users.txt into database")
            conn.commit()

        logger.info(f"Loaded {len(user_ids)} users from users.txt")
    except Exception as e:
        logger.error(f"Error loading users from file: {e}")

def get_all_users():
    """Get all users' data from database."""
    try:
        with sqlite3.connect(CONFIG["DB_FILE"]) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, usage_count, topic_id FROM users")
            return [{"user_id": row[0], "usage_count": row[1], "topic_id": row[2]} for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get all users error: {e}")
        return []

def get_user_by_topic(topic_id):
    """Get user_id by topic_id from database."""
    try:
        with sqlite3.connect(CONFIG["DB_FILE"]) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users WHERE topic_id = ?", (topic_id,))
            result = cursor.fetchone()
            return result[0] if result else None
    except Exception as e:
        logger.error(f"Get user by topic error: {e}")
        return None

async def forward_to_topic(update: Update, context: ContextTypes, topic_id):
    """Forward user's message to topic."""
    user_id = update.effective_user.id
    try:
        await context.bot.forward_message(
            chat_id=CONFIG["GROUP_CHAT_ID"],
            message_thread_id=topic_id,
            from_chat_id=user_id,
            message_id=update.message.message_id
        )
    except Exception as e:
        logger.error(f"Forward to topic {topic_id} error: {e}")

async def send_to_topic(context: ContextTypes, topic_id, text=None, photo=None, caption=None, reply_markup=None, parse_mode=None):
    """Send bot's reply to topic."""
    try:
        if photo:
            await context.bot.send_photo(
                chat_id=CONFIG["GROUP_CHAT_ID"],
                message_thread_id=topic_id,
                photo=photo,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
        else:
            await context.bot.send_message(
                chat_id=CONFIG["GROUP_CHAT_ID"],
                message_thread_id=topic_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
    except Exception as e:
        logger.error(f"Send to topic {topic_id} error: {e}")

async def create_user_topic(user, context: ContextTypes):
    """Create a topic for a user in the group and post their info."""
    user_id = user.id
    user_data = manage_user_data(user_id)
    
    if user_data["topic_id"]:
        # Verify topic exists in Telegram
        try:
            await context.bot.send_chat_action(
                chat_id=CONFIG["GROUP_CHAT_ID"],
                message_thread_id=user_data["topic_id"],
                action="typing"
            )
            logger.info(f"Reusing existing topic {user_data['topic_id']} for user {user_id}")
            return user_data["topic_id"]
        except Exception as e:
            logger.warning(f"Topic {user_data['topic_id']} for user {user_id} is invalid: {e}")
            # Clear invalid topic_id and create a new one
            manage_user_data(user_id, update_topic_id=None)
    
    try:
        # Create a new topic in the group
        topic = await context.bot.create_forum_topic(
            chat_id=CONFIG["GROUP_CHAT_ID"],
            name=f"User {user.full_name or user_id}"
        )
        topic_id = topic.message_thread_id
        manage_user_data(user_id, update_topic_id=topic_id)
        
        # Get user profile photo
        profile_photo = None
        try:
            photos = await context.bot.get_user_profile_photos(user_id, limit=1)
            if photos.photos:
                profile_photo = photos.photos[0][-1].file_id
        except Exception as e:
            logger.error(f"Error fetching profile photo for user {user_id}: {e}")
        
        # Post user info with hyperlink
        user_info = f"User Info:\nFull Name: {user.full_name}\n<a href=\"tg://user?id={user_id}\">User ID: {user_id}</a>"
        
        if profile_photo:
            await send_to_topic(context, topic_id, photo=profile_photo, caption=user_info, parse_mode=ParseMode.HTML)
        else:
            await send_to_topic(context, topic_id, text=user_info, parse_mode=ParseMode.HTML)
        
        logger.info(f"Created topic {topic_id} for user {user_id}")
        return topic_id
    except Exception as e:
        logger.error(f"Error creating topic for user {user_id}: {e}")
        return None

def get_user_language(user_id):
    """Return English settings."""
    return LANGUAGE_EN

async def start_command(update: Update, context: ContextTypes) -> None:
    """Handle /start."""
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    logger.info(f"User {user_id} started bot")
    
    try:
        manage_user_data(user_id)  # Ensure user is in db
        topic_id = await create_user_topic(update.effective_user, context)
        
        if not topic_id:
            raise Exception("Failed to create or reuse user topic")
        
        await forward_to_topic(update, context, topic_id)
        
        keyboard = [[InlineKeyboardButton("Support ⭐", url=CONFIG["SUPPORT_URL"])]]
        await update.message.reply_text(
            lang["welcome"],
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        await send_to_topic(context, topic_id, text=lang["welcome"], reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Error in start_command for user {user_id}: {e}")
        await update.message.reply_text(lang["error"].format("Failed to start bot, please try again later"))

async def users_command(update: Update, context: ContextTypes) -> None:
    """Handle /users to send users.txt with number of users and user IDs (admin only)."""
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    
    if user_id != CONFIG["ADMIN_ID"]:
        await update.message.reply_text(lang["admin_only"])
        return
    
    all_users = get_all_users()
    num_users = len(all_users)
    user_ids = "\n".join(str(user["user_id"]) for user in all_users)
    
    filename = Path(CONFIG["DATA_DIR"]) / CONFIG["USERS_FILE"]
    filename.parent.mkdir(exist_ok=True)
    with filename.open("w", encoding="utf-8") as f:
        f.write(f"Number of users: {num_users}\n\nUser IDs:\n{user_ids}")
    
    try:
        with filename.open("rb") as f:
            await context.bot.send_document(
                chat_id=user_id,
                document=f,
                caption=lang["num_users"].format(num_users)
            )
        logger.info(f"users.txt sent to admin {user_id}")
    except Exception as e:
        logger.error(f"Error sending users.txt: {e}")
        await update.message.reply_text(lang["error"].format("Failed to send users.txt"))

async def broadcast_command(update: Update, context: ContextTypes) -> None:
    """Handle /bro <message> [-<btnname>:<btnlink>] [--<imagelink>] (admin only)."""
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    
    if user_id != CONFIG["ADMIN_ID"]:
        await update.message.reply_text(lang["admin_only"])
        return
    
    command_text = " ".join(context.args)
    if not command_text:
        await update.message.reply_text(lang["broadcast_error"])
        return
    
    message = command_text
    btn_name = btn_link = image_url = None
    
    if "--" in message:
        message, image_url = message.split("--", 1)
        message = message.strip()
        image_url = image_url.strip() or None
    
    if "-" in message:
        message, btn_part = message.split("-", 1)
        message = message.strip()
        if ":" in btn_part:
            btn_name, btn_link = map(str.strip, btn_part.split(":", 1))
            if not btn_name or not btn_link:
                btn_name = btn_link = None
    
    if not message:
        await update.message.reply_text(lang["broadcast_error"])
        return
    
    if btn_link and not btn_link.startswith(("http://", "https://", "t.me/")):
        btn_link = f"https://{btn_link}"
    
    broadcast_id = str(uuid.uuid4())
    context.bot_data[broadcast_id] = {
        "message": message,
        "btn_name": btn_name,
        "btn_link": btn_link,
        "image_url": image_url,
        "admin_id": user_id
    }
    
    preview_keyboard = [[InlineKeyboardButton(btn_name, url=btn_link)]] if btn_name and btn_link else None
    if image_url:
        await update.message.reply_photo(
            photo=image_url,
            caption=message,
            reply_markup=InlineKeyboardMarkup(preview_keyboard) if preview_keyboard else None
        )
    else:
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(preview_keyboard) if preview_keyboard else None
        )
    
    approval_keyboard = [
        [
            InlineKeyboardButton(lang["yes"], callback_data=f"broadcast_yes_{broadcast_id}"),
            InlineKeyboardButton(lang["no"], callback_data=f"broadcast_no_{broadcast_id}")
        ]
    ]
    await update.message.reply_text(
        lang["broadcast_approve"],
        reply_markup=InlineKeyboardMarkup(approval_keyboard)
    )

async def handle_broadcast_callback(update: Update, context: ContextTypes) -> None:
    """Handle broadcast approval/cancellation."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = get_user_language(user_id)
    
    if user_id != CONFIG["ADMIN_ID"]:
        await query.message.reply_text(lang["admin_only"])
        return
    
    if not query.data.startswith("broadcast_"):
        return
    
    action, broadcast_id = query.data.split("_", 2)[1:]
    broadcast_data = context.bot_data.get(broadcast_id)
    if not broadcast_data:
        return
    
    if action == "no":
        del context.bot_data[broadcast_id]
        await query.message.edit_text(lang["broadcast_canceled"])
        return
    
    if action == "yes":
        all_users = get_all_users()
        count = 0
        keyboard = [[InlineKeyboardButton(broadcast_data["btn_name"], url=broadcast_data["btn_link"])]] if broadcast_data["btn_name"] and broadcast_data["btn_link"] else None
        
        for user in all_users:
            try:
                if broadcast_data["image_url"]:
                    await context.bot.send_photo(
                        chat_id=user["user_id"],
                        photo=broadcast_data["image_url"],
                        caption=broadcast_data["message"],
                        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
                    )
                else:
                    await context.bot.send_message(
                        chat_id=user["user_id"],
                        text=broadcast_data["message"],
                        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
                    )
                count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Broadcast failed for user {user['user_id']}: {e}")
        
        del context.bot_data[broadcast_id]
        await query.message.edit_text(lang["broadcast_success"].format(count=count))

async def gen_command(update: Update, context: ContextTypes) -> None:
    """Handle /gen."""
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    logger.info(f"User {user_id} initiated /gen")
    
    manage_user_data(user_id)  # Ensure user is in db
    topic_id = await create_user_topic(update.effective_user, context)
    
    await forward_to_topic(update, context, topic_id)
    
    USER_STATE[user_id] = STATE_PROMPT
    await update.message.reply_text(lang["prompt"])
    await send_to_topic(context, topic_id, text=lang["prompt"])

async def handle_message(update: Update, context: ContextTypes) -> None:
    """Handle messages."""
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    
    manage_user_data(user_id)  # Ensure user is in db
    topic_id = await create_user_topic(update.effective_user, context)
    
    await forward_to_topic(update, context, topic_id)
    
    if user_id not in USER_STATE or USER_STATE[user_id] != STATE_PROMPT:
        return
    
    prompt = update.message.text.strip()
    if not prompt:
        return
    
    context.user_data["prompt"] = prompt
    USER_STATE[user_id] = STATE_DIMENSION
    keyboard = [
        [InlineKeyboardButton(lang["wide"], callback_data="dim_wide")],
        [InlineKeyboardButton(lang["tall"], callback_data="dim_tall")],
        [InlineKeyboardButton(lang["square"], callback_data="dim_square")],
    ]
    await update.message.reply_text(
        lang["dimension"],
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    await send_to_topic(context, topic_id, text=lang["dimension"], reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_callback_query(update: Update, context: ContextTypes) -> None:
    """Handle callbacks."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = get_user_language(user_id)
    
    user_data = manage_user_data(user_id)
    topic_id = user_data["topic_id"]
    if not topic_id:
        topic_id = await create_user_topic(update.effective_user, context)
    
    if query.data.startswith("broadcast_"):
        await handle_broadcast_callback(update, context)
        return
    
    if query.data.startswith("dim_"):
        dimension = query.data.split("_")[1]
        await send_to_topic(context, topic_id, text=f"Selected dimension: {dimension}")
        context.user_data["dimension"] = dimension
        USER_STATE[user_id] = STATE_IMPROVE
        keyboard = [
            [InlineKeyboardButton(lang["yes"], callback_data="imp_true")],
            [InlineKeyboardButton(lang["no"], callback_data="imp_false")],
        ]
        await query.message.edit_text(
            lang["improve"],
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    if query.data.startswith("imp_"):
        improve = query.data.split("_")[1] == "true"
        await send_to_topic(context, topic_id, text=f"Selected improve: {improve}")
        context.user_data["improve"] = improve
        del USER_STATE[user_id]
        
        prompt = context.user_data.get("prompt")
        dimension = context.user_data.get("dimension")
        
        manage_user_data(user_id, update_usage=user_data["usage_count"] + 1)
        generating_message = await query.message.edit_text(lang["generating"])
        await send_to_topic(context, topic_id, text=lang["generating"])
        
        try:
            start_time = time.time()
            params = {
                "prompt": prompt,
                "improve": str(improve).lower(),
                "format": dimension,
            }
            for attempt in range(3):
                try:
                    response = requests.get(CONFIG["IMG_API_URL"], params=params, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    logger.info(f"API response for user {user_id}: {data}")
                    break
                except (ReadError, ConnectionError, RequestException) as e:
                    logger.warning(f"API retry {attempt + 1}/3 failed for user {user_id}: {e}")
                    if attempt == 2:
                        await query.message.reply_text(lang["error"].format("Image generation unavailable"))
                        await send_to_topic(context, topic_id, text=lang["error"].format("Image generation unavailable"))
                        await generating_message.delete()
                        return
                    await asyncio.sleep(1)
            
            end_time = time.time()
            time_taken = round(end_time - start_time, 2)
            image_url = data.get("image_url", "")
            image_size = convert_size_to_bytes(data.get("image_size", "0 KB"))
            if image_size == 0 and image_url:
                image_size = estimate_image_size(image_url)
            image_size = round(image_size, 2)
            
            if not image_url:
                await query.message.reply_text(lang["error"].format("No image URL"))
                await send_to_topic(context, topic_id, text=lang["error"].format("No image URL"))
                await generating_message.delete()
                return
            
            if CONFIG["VERIFY_IMAGE_URL"]:
                try:
                    img_response = requests.head(image_url, timeout=5)
                    url_valid = img_response.status_code == 200
                except:
                    url_valid = False
            else:
                url_valid = True
            
            if url_valid:
                keyboard = [[InlineKeyboardButton("Download Image 💾", url=image_url)]]
                success_message = lang["success"].format(prompt, time_taken, image_size)
                
                await query.message.reply_photo(
                    photo=image_url,
                    caption=success_message,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                await send_to_topic(context, topic_id, photo=image_url, caption=success_message, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await query.message.reply_text(lang["invalid_image_url"])
                await send_to_topic(context, topic_id, text=lang["invalid_image_url"])
            
            await generating_message.delete()
        
        except Exception as e:
            logger.error(f"Image generation error for user {user_id}: {e}")
            await query.message.reply_text(lang["error"].format("Unexpected error"))
            await send_to_topic(context, topic_id, text=lang["error"].format("Unexpected error"))
            await generating_message.delete()

async def handle_admin_message_in_topic(update: Update, context: ContextTypes) -> None:
    """Handle admin messages in group topics and send to user."""
    if update.effective_user.id != CONFIG["ADMIN_ID"]:
        return
    topic_id = update.message.message_thread_id
    if not topic_id:
        return
    user_id = get_user_by_topic(topic_id)
    if user_id:
        try:
            await context.bot.send_message(chat_id=user_id, text=update.message.text)
        except Exception as e:
            logger.error(f"Error sending admin message to user {user_id}: {e}")

def convert_size_to_bytes(size_str):
    """Convert size string to KB."""
    try:
        if not size_str or size_str.lower() == "unknown":
            return 0
        match = re.match(r"(\d+\.?\d*)\s*(KB|MB|GB)", size_str.strip(), re.IGNORECASE)
        if not match:
            return 0
        value, unit = float(match.group(1)), match.group(2).upper()
        multipliers = {"KB": 1, "MB": 1024, "GB": 1024 * 1024}
        return value * multipliers[unit]
    except Exception as e:
        logger.error(f"Size conversion error: {e}")
        return 0

def estimate_image_size(image_url):
    """Estimate image size from Content-Length header."""
    try:
        response = requests.head(image_url, timeout=5)
        if response.status_code == 200 and "Content-Length" in response.headers:
            return round(int(response.headers["Content-Length"]) / 1024, 2)
        return 0
    except Exception as e:
        logger.error(f"Image size estimation error: {e}")
        return 0

async def error_handler(update: Update, context: ContextTypes) -> None:
    """Handle errors."""
    error = context.error
    logger.error(f"Update {update} caused error {type(error).__name__}: {error}")
    if isinstance(error, ReadError):
        logger.warning(f"ReadError details: {error.request.url if hasattr(error, 'request') else 'No request info'}")

def main():
    """Run bot and HTTP server."""
    init_db()
    load_users_from_file()
    logger.info("Starting bot and HTTP server")
    
    # Start HTTP server in a separate thread
    http_thread = Thread(target=run_http_server, daemon=True)
    try:
        http_thread.start()
        logger.info("Health check server thread started")
    except Exception as e:
        logger.error(f"Failed to start HTTP server thread: {e}")
        return
    
    # Create new event loop for bot
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Initialize and run bot
    try:
        application = Application.builder().token(CONFIG["BOT_TOKEN"]).build()
        
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("gen", gen_command))
        application.add_handler(CommandHandler("users", users_command))
        application.add_handler(CommandHandler("bro", broadcast_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_message))
        application.add_handler(CallbackQueryHandler(handle_callback_query))
        application.add_handler(MessageHandler(filters.Chat(CONFIG["GROUP_CHAT_ID"]) & filters.User(CONFIG["ADMIN_ID"]), handle_admin_message_in_topic))
        application.add_error_handler(error_handler)
        
        logger.info("Starting bot polling")
        loop.run_until_complete(application.run_polling())
    except Exception as e:
        logger.error(f"Bot initialization or polling error: {e}")
    finally:
        loop.close()
        logger.info("Event loop closed")

if __name__ == "__main__":
    main()
