import logging
import json
import os
import time
from datetime import datetime, timedelta
from urllib.parse import quote
import requests

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    ChatMember
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler
)
from telegram.error import Forbidden, BadRequest

# --- ⚙️ CONFIGURATION (Loaded from Render Environment Variables) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS_STR = os.environ.get("ADMIN_IDS", "").split(',')
ADMIN_IDS = [int(admin_id) for admin_id in ADMIN_IDS_STR if admin_id]
CHANNEL_ID = os.environ.get("CHANNEL_ID")


# API and Bot Settings
VIDEO_API_URL = "https://texttovideov2.alphaapi.workers.dev/api/"
DB_FILE = "users_data.json"
STARTING_CREDITS = 50
VIDEO_COST = 10
DAILY_BONUS = 20
REFERRAL_BONUS = 30

# --- 🪵 LOGGING SETUP ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Conversation States ---
(PROMPT_INPUT, BROADCAST_MESSAGE) = range(2)

# --- 🗂️ DATABASE MANAGEMENT (No changes needed here) ---
class UserDataManager:
    def __init__(self, file_path):
        self.file_path = file_path
        self.users_data = self._load_data()

    def _load_data(self):
        if not os.path.exists(self.file_path): return {}
        with open(self.file_path, 'r') as f: return json.load(f)

    def _save_data(self):
        with open(self.file_path, 'w') as f: json.dump(self.users_data, f, indent=4)

    def add_user(self, user_id):
        user_id_str = str(user_id)
        if user_id_str not in self.users_data:
            self.users_data[user_id_str] = {
                "credits": STARTING_CREDITS, "videos_created": 0,
                "last_bonus_claim": None, "referred_by": None
            }
            self._save_data()
            return True
        return False

    def get_user(self, user_id): return self.users_data.get(str(user_id))
    def get_all_user_ids(self): return list(self.users_data.keys())

    def update_credits(self, user_id, amount):
        user_id_str = str(user_id)
        if user_id_str in self.users_data:
            self.users_data[user_id_str]['credits'] += amount
            self._save_data()
            return self.users_data[user_id_str]['credits']

    def record_video_generation(self, user_id):
        user_id_str = str(user_id)
        if user_id_str in self.users_data:
            self.users_data[user_id_str]['videos_created'] += 1; self._save_data()

    def can_claim_bonus(self, user_id):
        user = self.get_user(user_id)
        if not user or not user.get('last_bonus_claim'): return True
        last_claim_time = datetime.fromisoformat(user['last_bonus_claim'])
        return datetime.utcnow() >= last_claim_time + timedelta(hours=24)

    def claim_bonus(self, user_id):
        user_id_str = str(user_id)
        if self.can_claim_bonus(user_id):
            self.users_data[user_id_str]['last_bonus_claim'] = datetime.utcnow().isoformat()
            self.update_credits(user_id, DAILY_BONUS); self._save_data()
            return True
        return False
        
    def set_referrer(self, user_id, referrer_id):
        user_id_str = str(user_id)
        if user_id_str in self.users_data and self.users_data[user_id_str].get("referred_by") is None:
             self.users_data[user_id_str]["referred_by"] = referrer_id; self._save_data()
             return True
        return False

db_manager = UserDataManager(DB_FILE)

# ---  HELPER FUNCTIONS ---
async def is_user_member_of_channel(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.CREATOR]
    except Exception as e:
        logger.error(f"Error checking membership for {user_id}: {e}")
        return False

# --- 🚀 GATE & START COMMAND ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    referrer_id = int(context.args[0]) if context.args and context.args[0].isdigit() else None

    if await is_user_member_of_channel(user.id, context):
        is_new_user = db_manager.add_user(user.id)
        if is_new_user:
            if referrer_id and referrer_id != user.id and db_manager.set_referrer(user.id, referrer_id):
                 db_manager.update_credits(referrer_id, REFERRAL_BONUS)
                 await context.bot.send_message(chat_id=referrer_id, text=f"🎉 A user you referred has joined! You've received {REFERRAL_BONUS} credits.")
            await send_admin_notification(user, context)
            await update.message.reply_text(
                f"🎉*Welcome to VisionCraft Elite!*\n\nI turn your imagination into video. You've been gifted `{STARTING_CREDITS}` credits to begin your journey.", parse_mode='Markdown'
            )
        await show_main_menu(update, context)
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➡️ Join Channel", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}")],
            [InlineKeyboardButton("✅ Verify Membership", callback_data=f"check_join_{referrer_id}")]
        ])
        await update.message.reply_text(
            "🔒 *ACCESS DENIED*\n\nWelcome! To unlock my creative powers, please join our official channel. This is a one-time step.",
            reply_markup=keyboard, parse_mode='Markdown'
        )

async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    referrer_id_str = query.data.split('_')[-1]
    referrer_id = int(referrer_id_str) if referrer_id_str != 'None' else None

    await query.answer("Verifying membership...")

    if await is_user_member_of_channel(user.id, context):
        await query.edit_message_text(f"✅ *Access Granted!* \n\nThank you for joining. You've received `{STARTING_CREDITS}` starting credits.", parse_mode='Markdown')
        is_new_user = db_manager.add_user(user.id)
        if is_new_user:
            if referrer_id and referrer_id != user.id and db_manager.set_referrer(user.id, referrer_id):
                db_manager.update_credits(referrer_id, REFERRAL_BONUS)
                try:
                    await context.bot.send_message(chat_id=referrer_id, text=f"🎉 A user you referred has joined! You've received *{REFERRAL_BONUS} credits*.", parse_mode='Markdown')
                except Forbidden: logger.warning(f"Could not notify referrer {referrer_id}.")
            await send_admin_notification(user, context)
        await show_main_menu(update, context)
    else:
        await query.answer("You haven't joined yet. Please join the channel and then click Verify.", show_alert=True)

async def send_admin_notification(user, context: ContextTypes.DEFAULT_TYPE):
    total_users = len(db_manager.get_all_user_ids())
    admin_message = (f"👤 *New User Joined*\n\n"
                     f"• *Name:* {user.first_name}\n"
                     f"• *Username:* @{user.username or 'N/A'}\n"
                     f"• *ID:* `{user.id}`\n\n"
                     f"Total users now: *{total_users}*")
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=admin_message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to send new user notification to admin {admin_id}: {e}")

# --- 🏛️ MAIN MENU ---
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["🎬 Generate Video", "👤 My Account"],
        ["🎁 Get Credits", "❓ Guide & Help"]
    ]
    main_menu = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    chat_id = update.effective_chat.id
    message_text = "🤖 *Main Menu*\n\nWhat would you like to create today?"
    
    if update.callback_query:
        await context.bot.send_message(chat_id=chat_id, text=message_text, reply_markup=main_menu, parse_mode='Markdown')
    else:
        await update.message.reply_text(text=message_text, reply_markup=main_menu, parse_mode='Markdown')

# --- 🎬 USER FEATURES ---
async def generate_video_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_data = db_manager.get_user(update.effective_user.id)
    if user_data['credits'] < VIDEO_COST:
        await update.message.reply_text(f"😕 *Not enough credits!*\n\nYou need `{VIDEO_COST}` credits but you only have `{user_data['credits']}`. Get more from the '🎁 Get Credits' menu.", parse_mode='Markdown')
        return ConversationHandler.END
    await update.message.reply_text("✍️ *Describe the video you want to create.*\n\n(e.g., `a majestic lion walking in the savanna at sunset`)", parse_mode='Markdown')
    return PROMPT_INPUT

async def process_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    prompt = update.message.text
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    processing_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ *Crafting your vision...*\n\nThe AI is generating your video. This can take up to a minute.", parse_mode='Markdown')
    
    try:
        full_api_url = f"{VIDEO_API_URL}?prompt={quote(prompt)}"
        response = requests.get(full_api_url, timeout=120)
        response.raise_for_status()
        data = response.json()
        
        if data.get("success"):
            db_manager.update_credits(user_id, -VIDEO_COST)
            db_manager.record_video_generation(user_id)
            await context.bot.send_video(
                chat_id=chat_id, video=data.get("url"),
                caption=f"✅ *Video Ready!*\n\n*Prompt:* _{prompt}_\n\n(`{VIDEO_COST}` credits used)",
                parse_mode='Markdown'
            )
        else:
            await context.bot.send_message(chat_id=chat_id, text="❌ The AI couldn't create a video for that prompt. Please try being more descriptive or try a different idea.")
    except requests.exceptions.Timeout:
         await context.bot.send_message(chat_id=chat_id, text="❌ The video generation timed out. The server may be busy. Please try again in a few minutes.")
    except Exception as e:
        logger.error(f"Video generation failed for prompt '{prompt}': {e}")
        await context.bot.send_message(chat_id=chat_id, text="❌ An unexpected error occurred. The developers have been notified.")
    finally:
        await context.bot.delete_message(chat_id=chat_id, message_id=processing_msg.message_id)
    return ConversationHandler.END

async def my_account_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_data = db_manager.get_user(user.id)
    await update.message.reply_text(
        f"👤 *My Account Profile*\n\n"
        f" • *User ID:* `{user.id}`\n"
        f" • *Credits:* `{user_data['credits']}` 💎\n"
        f" • *Videos Created:* `{user_data['videos_created']}` 🎬",
        parse_mode='Markdown'
    )

async def get_credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    bot_username = (await context.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start={user_id}"
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("☀️ Claim Daily Bonus", callback_data="claim_bonus")]])
    await update.message.reply_text(
        f"🎁 *Get More Credits*\n\n"
        f"1️⃣ *Daily Bonus*\nClaim your free `{DAILY_BONUS}` credits every 24 hours!\n\n"
        f"2️⃣ *Refer a Friend*\nShare your link below. You get `{REFERRAL_BONUS}` credits for each friend who joins!\n"
        f"`{referral_link}`",
        reply_markup=keyboard, parse_mode='Markdown', disable_web_page_preview=True
    )

async def claim_bonus_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if db_manager.claim_bonus(query.from_user.id):
        new_balance = db_manager.get_user(query.from_user.id)['credits']
        await query.answer(f"🎉 You claimed {DAILY_BONUS} credits! New balance: {new_balance} 💎", show_alert=True)
    else:
        await query.answer("😕 You've already claimed your bonus today. Try again tomorrow.", show_alert=True)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "❓ *VisionCraft Elite Guide*\n\n"
        "Here’s a full guide to all my features:\n\n"
        "🎬 *Generate Video*\n"
        "This is the core feature. Tap this button, and I'll ask you for a text prompt. Describe a scene, an action, or an idea, and my AI will create a short video based on it. Each video costs `10` credits.\n\n"
        "👤 *My Account*\n"
        "Check your profile. This shows your unique User ID, your current credit balance, and a count of all the videos you've ever created.\n\n"
        "🎁 *Get Credits*\n"
        "Need more credits? This is the place. You can claim a free daily bonus or use your unique referral link to invite friends. You get a big credit bonus for every friend that joins!\n\n"
        "If you have any other issues, please contact the bot admin.",
        parse_mode='Markdown'
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operation cancelled.")
    await show_main_menu(update, context)
    return ConversationHandler.END

# --- 👑 ADMIN FEATURES ---
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 Broadcast Message", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📊 Bot Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("🔙 Exit Admin Mode", callback_data="admin_exit")]
    ])
    await update.message.reply_text("👑 *Admin Panel*", reply_markup=keyboard, parse_mode='Markdown')

async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    total_users = len(db_manager.get_all_user_ids())
    await query.edit_message_text(f"📈 *Bot Statistics*\n\n*Total Unique Users:* `{total_users}`", parse_mode='Markdown', reply_markup=query.message.reply_markup)

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    await query.message.reply_text("Send the message to broadcast. It can be text, photo, video, etc.\n\nSend /cancel to abort.", reply_markup=ReplyKeyboardRemove())
    return BROADCAST_MESSAGE
    
async def admin_broadcast_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['broadcast_message'] = update.message
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm & Send", callback_data="broadcast_confirm")], [InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel")]])
    await update.message.reply_text(f"This will be sent to *{len(db_manager.get_all_user_ids())}* users. Confirm?", reply_markup=keyboard, parse_mode='Markdown')
    return ConversationHandler.END

async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Starting broadcast..."); await query.edit_message_text("⏳ Broadcasting...")
    sent, failed = 0, 0
    for user_id in db_manager.get_all_user_ids():
        try:
            await context.user_data['broadcast_message'].copy(chat_id=int(user_id)); sent += 1
        except (Forbidden, BadRequest): failed += 1
        time.sleep(0.1)
    report = f"✅ *Broadcast Complete!*\n\n*Sent:* `{sent}`\n*Failed:* `{failed}` (users blocked the bot)"
    await context.bot.send_message(chat_id=query.from_user.id, text=report, parse_mode='Markdown')
    await show_main_menu(query, context)

# --- 🚀 MAIN FUNCTION ---
def main() -> None:
    """Start the bot."""
    application = Application.builder().token(BOT_TOKEN).build()
    
    video_conv = ConversationHandler(entry_points=[MessageHandler(filters.Regex("^🎬 Generate Video$"), generate_video_command)], states={PROMPT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_prompt)]}, fallbacks=[CommandHandler("cancel", cancel)])
    broadcast_conv = ConversationHandler(entry_points=[CallbackQueryHandler(admin_broadcast_start, pattern='^admin_broadcast$')], states={BROADCAST_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, admin_broadcast_receive)]}, fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern='^broadcast_cancel$')])

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(check_join_callback, pattern=r'^check_join_'))
    application.add_handler(video_conv)
    application.add_handler(MessageHandler(filters.Regex("^👤 My Account$"), my_account_command))
    application.add_handler(MessageHandler(filters.Regex("^🎁 Get Credits$"), get_credits_command))
    application.add_handler(MessageHandler(filters.Regex("^❓ Guide & Help$"), help_command))
    application.add_handler(CallbackQueryHandler(claim_bonus_callback, pattern='^claim_bonus$'))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(admin_stats_callback, pattern='^admin_stats$'))
    application.add_handler(broadcast_conv)
    application.add_handler(CallbackQueryHandler(admin_broadcast_send, pattern='^broadcast_confirm$'))
    
    application.run_polling()


if __name__ == "__main__":
    main()
