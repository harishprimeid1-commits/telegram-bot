import asyncio
import json
import os
import re
import shutil
from html import escape
from pathlib import Path
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# SHADMAN-STYLE CHANNEL POST REMOVER
# One-file Telegram bot
#
# IMPORTANT:
# 1. Put your NEW BotFather token in BOT_TOKEN.
# 2. Put your Telegram numeric ID in ADMIN_ID.
# 3. Make the bot an administrator in monitored channels with
#    permission to delete messages.
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 8649761210

DATA_FILE = Path("channel_remover_data.json")

# Bangladesh time, matching the original file's night-mode setup.
BD_TZ = ZoneInfo("Asia/Dhaka")
NIGHT_START = time(23, 15)
NIGHT_END = time(9, 0)

DEFAULT_DATA = {
    "abuse_words": [],
    "channels": [],
    "force_join_channels": [],
    "permitted_users": [],
    "pending_chats": [],
    "maintenance": False,
    "locked": False,
    "post_delete_enabled": True,
    "total_deleted": 0,
    "total_seen": 0,
    "daily_user_posts": {},
    "daily_user_date": "",
    "daily_user_limit": 2,
    "channel_daily_limit": 0,
    "channel_daily_target": "",
    "daily_channel_posts": {},
    "daily_channel_date": "",
    "channel_admin_daily_limit": 2,
    "daily_channel_admin_posts": {},
    "daily_channel_admin_date": "",
    "managed_users": {},
    "admin_user_limits": {},
    "night_enabled": True,
    "night_start": "23:15",
    "night_end": "09:00",
    "night_channel": "",
    "night_message": "🟩 Night Mode is now active.",
    "night_last_sent_date": "",
    "night_last_message_id": None,
    "developer_name": "Harish",
    "developer_username": "",
    "created_at": "",
    "last_backup": "",
    "last_error": "",
    "limit_notice_message_ids": [],
}


def load_data():
    if not DATA_FILE.exists():
        data = dict(DEFAULT_DATA)
        data["created_at"] = datetime.now(BD_TZ).isoformat(timespec="seconds")
        return data
    try:
        loaded = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("Data file root must be an object")
        for key, value in DEFAULT_DATA.items():
            if key not in loaded:
                # Copy mutable defaults so they are not shared accidentally.
                loaded[key] = value.copy() if isinstance(value, (dict, list)) else value
        return loaded
    except Exception as exc:
        # Keep the broken file for recovery instead of silently destroying it.
        broken = DATA_FILE.with_suffix(".broken.json")
        try:
            shutil.copy2(DATA_FILE, broken)
        except Exception:
            pass
        print(f"[DATA LOAD ERROR] {exc}. Using defaults.")
        data = dict(DEFAULT_DATA)
        data["created_at"] = datetime.now(BD_TZ).isoformat(timespec="seconds")
        return data


data = load_data()
data.setdefault("post_delete_enabled", True)
data.setdefault("limit_notice_message_ids", [])


def save_data():
    # Atomic write: prevents a sudden Termux/process stop from leaving invalid JSON.
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(DATA_FILE)


# Fallback per-user state for environments where PTB user_data is unavailable.
USER_STATE = {}

def get_user_state(context, user_id):
    state = getattr(context, "user_data", None) if context is not None else None
    if isinstance(state, dict):
        return state
    return USER_STATE.setdefault(str(user_id), {})

# Migration for the per-admin channel posting limit.
# The old shared channel limit is disabled when this feature is first added,
# because the new rule is 2 posts PER ADMIN, not 2 posts for the whole channel.
if "channel_admin_limit_v1_applied" not in data:
    data["channel_admin_daily_limit"] = 2
    data["daily_channel_admin_posts"] = {}
    data["daily_channel_admin_date"] = ""
    data["channel_daily_limit"] = 0
    data["channel_admin_limit_v1_applied"] = True
    save_data()


def is_admin(user_id):
    return user_id == ADMIN_ID


def is_permitted(user_id):
    try:
        return int(user_id) in {int(x) for x in data.get("permitted_users", [])}
    except (TypeError, ValueError):
        return False


async def check_force_join(context, user_id):
    """Return channels the user has not joined yet."""
    missing = []
    for channel_id in data.get("force_join_channels", []):
        try:
            member = await context.bot.get_chat_member(str(channel_id), user_id)
            if member.status in ("left", "kicked"):
                missing.append(str(channel_id))
        except Exception as exc:
            # A bad/inaccessible force-join channel should not block everyone.
            print(f"[FORCE JOIN CHECK] {channel_id}: {exc}")
    return missing


async def force_join_keyboard(context, channel_ids):
    rows = []
    for channel_id in channel_ids:
        try:
            chat = await context.bot.get_chat(channel_id)
            username = getattr(chat, "username", None)
            if username:
                rows.append([InlineKeyboardButton(f"📢 Join {chat.title or username}", url=f"https://t.me/{username}")])
            else:
                rows.append([InlineKeyboardButton(f"📢 {chat.title or channel_id}", callback_data="forcejoin:info")])
        except Exception:
            rows.append([InlineKeyboardButton(f"📢 Channel {channel_id}", callback_data="forcejoin:info")])
    rows.append([InlineKeyboardButton("🔄 Verify Joined", callback_data="forcejoin:verify")])
    return InlineKeyboardMarkup(rows)


def normalize(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


def contains_abuse_word(text):
    lowered = normalize(text)
    for word in data["abuse_words"]:
        if word and word.lower() in lowered:
            return word
    return None


def get_night_time(key, fallback):
    value = str(data.get(key, fallback))
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return fallback


def is_night_mode():
    if not data.get("night_enabled", True):
        return False
    now = datetime.now(BD_TZ).time()
    start = get_night_time("night_start", "23:15")
    end = get_night_time("night_end", "09:00")
    return now >= start or now < end


def status(value, on="ON", off="OFF"):
    return on if value else off


def reset_daily_user_posts():
    today = datetime.now(BD_TZ).date().isoformat()
    if data.get("daily_user_date") != today:
        data["daily_user_date"] = today
        data["daily_user_posts"] = {}
        save_data()


def user_daily_post_count(user_id):
    reset_daily_user_posts()
    return int(data["daily_user_posts"].get(str(user_id), 0))


def increment_user_daily_posts(user_id):
    reset_daily_user_posts()
    key = str(user_id)
    data["daily_user_posts"][key] = user_daily_post_count(user_id) + 1
    save_data()


def reset_daily_channel_posts():
    today = datetime.now(BD_TZ).date().isoformat()
    if data.get("daily_channel_date") != today:
        data["daily_channel_date"] = today
        data["daily_channel_posts"] = {}
        save_data()


def channel_daily_post_count(channel_id):
    reset_daily_channel_posts()
    return int(data["daily_channel_posts"].get(str(channel_id), 0))


def increment_channel_daily_posts(channel_id):
    reset_daily_channel_posts()
    key = str(channel_id)
    data["daily_channel_posts"][key] = channel_daily_post_count(channel_id) + 1
    save_data()


def channel_limit_applies(channel_id):
    target = str(data.get("channel_daily_target", "")).strip()
    return not target or str(channel_id) == target


def reset_daily_channel_admin_posts():
    today = datetime.now(BD_TZ).date().isoformat()
    if data.get("daily_channel_admin_date") != today:
        data["daily_channel_admin_date"] = today
        data["daily_channel_admin_posts"] = {}
        save_data()


def channel_admin_post_count(channel_id, admin_key):
    reset_daily_channel_admin_posts()
    channel_key = str(channel_id)
    key = f"{channel_key}:{admin_key}"
    return int(data.get("daily_channel_admin_posts", {}).get(key, 0))


def increment_channel_admin_posts(channel_id, admin_key):
    reset_daily_channel_admin_posts()
    channel_key = str(channel_id)
    key = f"{channel_key}:{admin_key}"
    posts = data.setdefault("daily_channel_admin_posts", {})
    posts[key] = channel_admin_post_count(channel_id, admin_key) + 1
    save_data()


def channel_admin_identity(post):
    """Return (identity_key, display_name, has_user_id).

    Telegram normally exposes from_user for identifiable channel-post authors.
    For anonymous channel admins, author_signature is the only usable fallback.
    """
    user = getattr(post, "from_user", None)
    if user and getattr(user, "id", None):
        name = getattr(user, "full_name", None) or getattr(user, "username", None) or str(user.id)
        return f"user:{user.id}", name, True

    signature = getattr(post, "author_signature", None)
    if signature:
        return f"signature:{signature}", signature, False

    return None, "Unknown", False


async def channel_post_author_is_admin(context, post):
    """Check whether the identifiable channel-post author is an admin."""
    user = getattr(post, "from_user", None)
    if not user or not getattr(user, "id", None):
        # Anonymous channel-admin posts may only expose author_signature.
        # In that case Telegram does not provide a user ID to verify here.
        return bool(getattr(post, "author_signature", None))
    try:
        member = await context.bot.get_chat_member(post.chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception as exc:
        print(f"[CHANNEL ADMIN CHECK ERROR] Channel={post.chat.id} User={user.id}: {exc}")
        return False


def channel_admin_daily_status(channel_id):
    limit = int(data.get("channel_admin_daily_limit", 2) or 0)
    return limit


def channel_daily_status(channel_id):
    limit = int(data.get("channel_daily_limit", 0))
    count = channel_daily_post_count(channel_id) if channel_limit_applies(channel_id) else 0
    return limit, count


def reset_admin_user_limits():
    today = datetime.now(BD_TZ).date().isoformat()
    changed = False
    for key, item in data.get("admin_user_limits", {}).items():
        if item.get("date") != today:
            item["date"] = today
            item["count"] = 0
            changed = True
    if changed:
        save_data()


def admin_user_limit_for(user):
    if not user:
        return None, None
    reset_admin_user_limits()
    key = str(user.id)
    item = data.get("admin_user_limits", {}).get(key)
    if item is not None:
        item["user_id"] = user.id
        item["display_name"] = user.full_name or item.get("display_name") or key
        item["username"] = user.username or item.get("username") or ""
        return key, item
    return None, None


def admin_user_limit_count(item):
    reset_admin_user_limits()
    return int(item.get("count", 0))


def admin_user_limit_increment(item):
    reset_admin_user_limits()
    item["count"] = admin_user_limit_count(item) + 1
    item["date"] = datetime.now(BD_TZ).date().isoformat()
    save_data()


def normalize_username(value):
    value = (value or "").strip()
    if value.startswith("@"):
        value = value[1:]
    return value.lower()


def reset_managed_users():
    today = datetime.now(BD_TZ).date().isoformat()
    changed = False
    for key, item in data.get("managed_users", {}).items():
        if item.get("date") != today:
            item["date"] = today
            item["count"] = 0
            changed = True
    if changed:
        save_data()


def managed_user_for(user):
    if not user:
        return None, None
    reset_managed_users()
    username = normalize_username(user.username)
    for key, item in data.get("managed_users", {}).items():
        if (username and key == username) or (item.get("user_id") and int(item["user_id"]) == int(user.id)):
            item["user_id"] = user.id
            item["display_username"] = user.username or item.get("display_username") or key
            return key, item
    return None, None


def managed_user_count(item):
    reset_managed_users()
    return int(item.get("count", 0))


def managed_user_increment(item):
    reset_managed_users()
    item["count"] = managed_user_count(item) + 1
    item["date"] = datetime.now(BD_TZ).date().isoformat()
    save_data()



def menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["👑 Admin Panel", "💎 Board"],
            ["💠 Bot Stats", "💫 Help"],
            ["💎 About Bot", "👑 Developer Info"],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Select an option...",
    )

def admin_text():
    lock_text = "🔒 LOCKED" if data["locked"] else "🔓 UNLOCKED"
    night_text = "ON" if data.get("night_enabled", True) else "OFF"
    post_delete_text = "ON" if data.get("post_delete_enabled", True) else "OFF"
    channel_limit = int(data.get("channel_daily_limit", 0) or 0)
    channel_admin_limit = int(data.get("channel_admin_daily_limit", 2) or 0)
    user_limit = int(data.get("daily_user_limit", 2) or 0)

    return (
        "🔒 <b>ADMIN PANEL</b> 🔒\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 Pending Chats : {len(data['pending_chats'])}\n"
        f"✅ Approved Chats : {len(data['channels'])}\n"
        f"🌐 Force-Join Channels : {len(data['force_join_channels'])}\n"
        f"🏅 Permitted Users : {len(data['permitted_users'])}\n"
        f"🚨 Master Lock : {lock_text}\n"
        f"🟩 Night Mode : {night_text}\n"
        f"🗑️ Post Delete : {post_delete_text}\n"
        f"⏰ Night Time : {data.get('night_start', '23:15')} - {data.get('night_end', '09:00')}\n"
        f"📢 Night Channel : {data.get('night_channel') or 'Not set'}\n"
        f"📊 Channel Posts Today : {sum(int(v) for v in data.get('daily_channel_posts', {}).values())}\n"
        f"📢 Shared Channel Limit : {channel_limit if channel_limit > 0 else 'OFF'}\n"
        f"👮 Admin Post Limit : {channel_admin_limit if channel_admin_limit > 0 else 'OFF'} per admin/day\n"
        f"👤 User Daily Limit : {user_limit if user_limit > 0 else 'OFF'}\n"
        f"👑 Managed Admins : {len(data.get('admin_user_limits', {}))}\n"
        f"⚙️ Maintenance : {status(data['maintenance'])}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Select an option below."
    )


def main_keyboard():
    night_state = "ON" if data.get("night_enabled", True) else "OFF"
    post_state = "ON" if data.get("post_delete_enabled", True) else "OFF"
    user_limit = int(data.get("daily_user_limit", 2) or 0)
    channel_limit = int(data.get("channel_daily_limit", 0) or 0)
    channel_admin_limit = int(data.get("channel_admin_daily_limit", 2) or 0)

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Pending Chats", callback_data="pending"),
            InlineKeyboardButton("🔵 Approved Chats", callback_data="approved"),
        ],
        [
            InlineKeyboardButton("🟣 Force Join Channels", callback_data="forcejoin"),
            InlineKeyboardButton("🟡 Permission", callback_data="permission"),
        ],
        [InlineKeyboardButton("🔷 Channel Settings", callback_data="channels")],
        [InlineKeyboardButton(f"🌙 Night Mode: {night_state}", callback_data="nightmode")],
        [InlineKeyboardButton("🟠 Maintenance Mode", callback_data="maintenance")],
        [InlineKeyboardButton(
            f"🟩 User Daily Limit: {user_limit if user_limit > 0 else 'OFF'}",
            callback_data="daily_limit",
        )],
        [InlineKeyboardButton(
            f"🟩 Shared Channel Limit: {channel_limit if channel_limit > 0 else 'OFF'}",
            callback_data="channel_daily_limit",
        )],
        [InlineKeyboardButton(
            f"👮 Admin Post Limit: {channel_admin_limit if channel_admin_limit > 0 else 'OFF'}",
            callback_data="channel_admin_daily_limit",
        )],
        [InlineKeyboardButton("🟢 User Daily Limits", callback_data="user_limits")],
        [InlineKeyboardButton("🔵 Admin User Limits", callback_data="admin_user_limits")],
        [InlineKeyboardButton(f"🔴 Post Delete: {post_state}", callback_data="post_delete_toggle")],
        [InlineKeyboardButton("🛠️ System Tools", callback_data="system_tools")],
        [InlineKeyboardButton("🔒 Lock All Systems", callback_data="lock")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
    ])


def bottom_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👑 Admin Panel", callback_data="admin"),
            InlineKeyboardButton("💎 Board", callback_data="board"),
        ],
        [
            InlineKeyboardButton("💠 Bot Stats", callback_data="stats"),
            InlineKeyboardButton("💫 Help", callback_data="help"),
        ],
        [
            InlineKeyboardButton("💎 About Bot", callback_data="about"),
            InlineKeyboardButton("👑 Developer Info", callback_data="developer"),
        ],
    ])


async def send_admin_panel(update, context):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("❌ Admin only.")
        return

    await update.effective_message.reply_text(
        admin_text(),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def start(update, context):
    user = update.effective_user
    if not user or not update.effective_message:
        return

    if is_admin(user.id) or is_permitted(user.id) or not data.get("force_join_channels"):
        await update.effective_message.reply_text(
            "𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗧𝗢 𝗙𝗜𝗙 𝗛𝗔𝗥𝗜𝗦𝗛 𝗣𝗢𝗦𝗧 𝗥𝗘𝗠𝗢𝗩𝗘𝗥\n\n"
            "✅ You can use the bot.",
            reply_markup=menu_keyboard(),
        )
        return

    missing = await check_force_join(context, user.id)
    if missing:
        await update.effective_message.reply_text(
            "🔐 <b>JOIN REQUIRED</b>\n\n"
            "Please join the required channel(s), then tap <b>Verify Joined</b>.",
            parse_mode="HTML",
            reply_markup=await force_join_keyboard(context, missing),
        )
        return

    await update.effective_message.reply_text(
        "𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗧𝗢 𝗙𝗜𝗙 𝗛𝗔𝗥𝗜𝗦𝗛 𝗣𝗢𝗦𝗧 𝗥𝗘𝗠𝗢𝗩𝗘𝗥\n\n"
        "✅ Verification successful.",
        reply_markup=menu_keyboard(),
    )


async def admin_command(update, context):
    await send_admin_panel(update, context)


async def add_abuse_word(update, context):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/addabuseword WORD\n\n"
            "Example:\n/addabuseword example"
        )
        return

    word = " ".join(context.args).strip()
    if word.lower() in [x.lower() for x in data["abuse_words"]]:
        await update.message.reply_text("⚠️ Word is already in the filter.")
        return

    data["abuse_words"].append(word)
    save_data()
    await update.message.reply_text(
        f"✅ Added to the abuse-word filter: {word}"
    )


async def remove_abuse_word(update, context):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/removeabuseword WORD"
        )
        return

    word = " ".join(context.args).strip()
    old = len(data["abuse_words"])
    data["abuse_words"] = [
        x for x in data["abuse_words"]
        if x.lower() != word.lower()
    ]
    save_data()

    if len(data["abuse_words"]) < old:
        await update.message.reply_text(f"✅ Removed from filter: {word}")
    else:
        await update.message.reply_text("❌ Word not found.")


async def list_abuse_words(update, context):
    if not is_admin(update.effective_user.id):
        return

    words = data["abuse_words"]
    if not words:
        await update.message.reply_text("📭 Abuse-word filter is empty.")
        return

    text = "🚨 <b>ABUSE WORDS</b>\n\n"
    text += "\n".join(f"• {w}" for w in words)
    await update.message.reply_text(text, parse_mode="HTML")


async def add_channel(update, context):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/addchannel CHANNEL_ID\n\n"
            "Or use Admin Panel → Channel Settings → ➕ Add Channel and forward any message from the channel."
        )
        return

    channel_id = context.args[0]
    if channel_id not in data["channels"]:
        data["channels"].append(channel_id)
        save_data()

    await update.message.reply_text(
        f"✅ Approved/monitored channel added: {channel_id}\n"
        "Make sure the bot is an administrator in that channel."
    )


async def add_channel_forward_prompt(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.callback_query.answer()
    context.user_data["waiting_for_channel_forward"] = True
    await update.callback_query.message.reply_text(
        "📢 <b>ADD CHANNEL</b>\n\n"
        "Please forward any message from the channel to this bot.\n"
        "I will detect the channel automatically.",
        parse_mode="HTML",
    )


async def handle_forwarded_channel(update, context):
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not is_admin(user.id):
        return False
    if not context.user_data.get("waiting_for_channel_forward"):
        return False

    origin = getattr(message, "forward_origin", None)
    channel = getattr(origin, "chat", None) if origin else None
    if not channel or getattr(channel, "type", None) != "channel":
        await message.reply_text(
            "❌ That is not a forwarded channel message. Please forward a message directly from the channel."
        )
        return True

    channel_id = str(channel.id)
    title = channel.title or "Unnamed channel"
    username = getattr(channel, "username", None)
    display = f"@{username}" if username else title

    context.user_data["pending_channel_id"] = channel_id
    context.user_data["pending_channel_title"] = title
    context.user_data["pending_channel_username"] = username or ""
    context.user_data["waiting_for_channel_forward"] = False

    await message.reply_text(
        f"📢 <b>CHANNEL FOUND</b>\n\n"
        f"Name: <b>{title}</b>\n"
        f"Username: <b>{display}</b>\n"
        f"ID: <code>{channel_id}</code>\n\n"
        "Add this channel to Approved / Monitored Channels?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Add Channel", callback_data="channel:confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="channel:cancel"),
            ]
        ]),
    )
    return True


async def remove_channel(update, context):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/removechannel CHANNEL_ID"
        )
        return

    channel_id = context.args[0]
    data["channels"] = [
        x for x in data["channels"] if x != channel_id
    ]
    save_data()
    await update.message.reply_text(f"✅ Channel removed: {channel_id}")


async def list_channels(update, context):
    if not is_admin(update.effective_user.id):
        return

    if not data["channels"]:
        await update.message.reply_text("📭 No approved channels configured.")
        return

    await update.message.reply_text(
        "✅ <b>APPROVED / MONITORED CHANNELS</b>\n\n"
        + "\n".join(
            f"• <code>{x}</code>" for x in data["channels"]
        ),
        parse_mode="HTML",
    )


async def add_forcejoin(update, context):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/addforcejoin CHANNEL_ID"
        )
        return

    channel_id = context.args[0]
    if channel_id not in data["force_join_channels"]:
        data["force_join_channels"].append(channel_id)
        save_data()

    await update.message.reply_text(
        f"🌐 Force-join channel added: {channel_id}"
    )


async def remove_forcejoin(update, context):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/removeforcejoin CHANNEL_ID"
        )
        return

    channel_id = context.args[0]
    data["force_join_channels"] = [
        x for x in data["force_join_channels"]
        if x != channel_id
    ]
    save_data()
    await update.message.reply_text(
        f"✅ Force-join channel removed: {channel_id}"
    )


async def add_permission(update, context):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/addpermission USER_ID"
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ USER_ID must be numeric.")
        return

    if user_id not in data["permitted_users"]:
        data["permitted_users"].append(user_id)
        save_data()

    await update.message.reply_text(
        f"🏅 User permitted: {user_id}"
    )


async def remove_permission(update, context):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/removepermission USER_ID"
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ USER_ID must be numeric.")
        return

    data["permitted_users"] = [
        x for x in data["permitted_users"] if x != user_id
    ]
    save_data()
    await update.message.reply_text(
        f"✅ Permission removed: {user_id}"
    )


async def channel_status(update, context):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /channelstatus CHANNEL_ID\nExample: /channelstatus -1001234567890"
        )
        return
    channel_id = context.args[0].strip()
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(channel_id, me.id)
        await update.message.reply_text(
            "📢 <b>CHANNEL STATUS</b>\n\n"
            f"Channel: <code>{channel_id}</code>\n"
            f"Bot: <code>{me.id}</code>\n"
            f"Status: <b>{member.status}</b>\n"
            f"Can delete messages: <b>{getattr(member, 'can_delete_messages', False)}</b>\n\n"
            "The bot must be an administrator with Delete Messages permission.",
            parse_mode="HTML",
        )
    except Exception as exc:
        await update.message.reply_text(
            "❌ Cannot access this channel. Make sure the bot is an administrator and the CHANNEL_ID is correct.\n\n"
            f"Error: <code>{str(exc)[:800]}</code>",
            parse_mode="HTML",
        )


async def stats(update, context):
    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        "🏆 <b>BOT STATS</b>\n\n"
        f"👀 Posts checked : {data['total_seen']}\n"
        f"🗑️ Posts deleted : {data['total_deleted']}\n"
        f"🚨 Filter words : {len(data['abuse_words'])}\n"
        f"✅ Approved chats : {len(data['channels'])}\n"
        f"🌐 Force-join channels : {len(data['force_join_channels'])}\n"
        f"🏅 Permitted users : {len(data['permitted_users'])}",
        parse_mode="HTML",
    )



async def set_channel_target(update, context):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /setchanneltarget CHANNEL_ID\n\n"
            "Use /setchanneltarget off to apply the channel daily limit to all channels."
        )
        return
    value = context.args[0].strip()
    if value.lower() == "off":
        data["channel_daily_target"] = ""
    elif re.fullmatch(r"-100\\d{5,20}", value):
        data["channel_daily_target"] = value
    else:
        await update.effective_message.reply_text("❌ Invalid channel ID. Example: -1001234567890")
        return
    save_data()
    target = data["channel_daily_target"] or "ALL CHANNELS"
    await update.effective_message.reply_text(f"✅ Channel daily-limit target: {target}")


async def backup_command(update, context):
    if not is_admin(update.effective_user.id):
        return
    try:
        save_data()
        backup = DATA_FILE.with_name(
            f"{DATA_FILE.stem}_backup_{datetime.now(BD_TZ).strftime('%Y%m%d_%H%M%S')}.json"
        )
        shutil.copy2(DATA_FILE, backup)
        data["last_backup"] = datetime.now(BD_TZ).isoformat(timespec="seconds")
        save_data()
        with backup.open("rb") as fh:
            await update.effective_message.reply_document(
                document=fh,
                filename=backup.name,
                caption="✅ Backup created successfully."
            )
    except Exception as exc:
        data["last_error"] = str(exc)[:500]
        save_data()
        await update.effective_message.reply_text(f"❌ Backup failed: {str(exc)[:500]}")


async def reset_stats_command(update, context):
    if not is_admin(update.effective_user.id):
        return
    data["total_seen"] = 0
    data["total_deleted"] = 0
    save_data()
    await update.effective_message.reply_text("✅ Statistics reset successfully.")


async def status_command(update, context):
    if not is_admin(update.effective_user.id):
        return
    job_queue = getattr(context.application, "job_queue", None)
    await update.effective_message.reply_text(
        "🩺 <b>SYSTEM STATUS</b>\n\n"
        f"🤖 Bot process: <b>RUNNING</b>\n"
        f"💾 Data file: <b>{'OK' if DATA_FILE.exists() else 'MISSING'}</b>\n"
        f"⏱ Scheduler: <b>{'AVAILABLE' if job_queue else 'UNAVAILABLE'}</b>\n"
        f"🗑 Auto delete: <b>{'ON' if data.get('post_delete_enabled', True) else 'OFF'}</b>\n"
        f"🔒 Lock: <b>{'ON' if data.get('locked') else 'OFF'}</b>\n"
        f"⚙️ Maintenance: <b>{'ON' if data.get('maintenance') else 'OFF'}</b>\n"
        f"❗ Last error: <code>{escape(str(data.get('last_error') or 'None'))}</code>",
        parse_mode="HTML",
    )


async def menu_text(update, context):
    if not update.message or not update.message.text:
        return

    text = update.message.text

    if text == "👑 Admin Panel":
        await send_admin_panel(update, context)
    elif text == "💎 Board":
        await update.message.reply_text(
            panel_section("board"), parse_mode="HTML"
        )
    elif text == "💠 Bot Stats":
        await stats(update, context)
    elif text == "💫 Help":
        await update.message.reply_text(
            panel_section("help"), parse_mode="HTML"
        )
    elif text == "💎 About Bot":
        await update.message.reply_text(
            panel_section("about"), parse_mode="HTML"
        )
    elif text == "👑 Developer Info":
        await update.message.reply_text(
            panel_section("developer"), parse_mode="HTML"
        )

async def admin_user_id_input(update, context):
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not is_admin(user.id) or not context.user_data.get("waiting_for_admin_user_id"):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.reply_text("❌ Invalid User ID. Send the numeric Telegram User ID, for example 123456789.")
        return
    user_id = int(raw)
    reset_admin_user_limits()
    admins = data.setdefault("admin_user_limits", {})
    key = str(user_id)
    item = admins.get(key)
    if item is None:
        item = {
            "user_id": user_id,
            "display_name": key,
            "username": "",
            "limit": 2,
            "count": 0,
            "date": datetime.now(BD_TZ).date().isoformat(),
        }
        admins[key] = item
        save_data()
    context.user_data.pop("waiting_for_admin_user_id", None)
    count = admin_user_limit_count(item)
    limit = int(item.get("limit", 2))
    await message.reply_text(
        f"👑 <b>Admin User ID</b>\n\nID: <code>{user_id}</code>\n📊 Posts today: <b>{count}</b>\n🎯 Daily limit: <b>{limit if limit > 0 else 'OFF'}</b>\n\nChoose the daily limit:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("1", callback_data=f"adminuserlimit:{key}:1"), InlineKeyboardButton("2", callback_data=f"adminuserlimit:{key}:2"), InlineKeyboardButton("3", callback_data=f"adminuserlimit:{key}:3")],
            [InlineKeyboardButton("5", callback_data=f"adminuserlimit:{key}:5"), InlineKeyboardButton("10", callback_data=f"adminuserlimit:{key}:10"), InlineKeyboardButton("OFF", callback_data=f"adminuserlimit:{key}:0")],
            [InlineKeyboardButton("⬅️ Admin User Limits", callback_data="admin_user_limits")],
        ])
    )


async def username_limit_input(update, context):
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not is_admin(user.id) or not context.user_data.get("waiting_for_username"):
        return
    username = normalize_username(message.text)
    if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", username):
        await message.reply_text("❌ Invalid username. Send it like @username.")
        return
    reset_managed_users()
    users = data.setdefault("managed_users", {})
    item = users.get(username)
    if item is None:
        item = {
            "display_username": username,
            "user_id": None,
            "limit": 3,
            "count": 0,
            "date": datetime.now(BD_TZ).date().isoformat(),
        }
        users[username] = item
        save_data()
    context.user_data.pop("waiting_for_username", None)
    count = managed_user_count(item)
    limit = int(item.get("limit", 3))
    await message.reply_text(
        f"👤 <b>@{username}</b>\n\n📊 Posts today: <b>{count}</b>\n🎯 Daily limit: <b>{limit if limit > 0 else 'OFF'}</b>\n\nChoose a new daily limit:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("1", callback_data=f"userlimit:{username}:1"), InlineKeyboardButton("2", callback_data=f"userlimit:{username}:2"), InlineKeyboardButton("3", callback_data=f"userlimit:{username}:3")],
            [InlineKeyboardButton("5", callback_data=f"userlimit:{username}:5"), InlineKeyboardButton("10", callback_data=f"userlimit:{username}:10"), InlineKeyboardButton("OFF", callback_data=f"userlimit:{username}:0")],
            [InlineKeyboardButton("⬅️ User Limits", callback_data="user_limits")],
        ])
    )


async def night_setting_input(update, context):
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not is_admin(user.id):
        return
    state = get_user_state(context, user.id)
    key = state.get("night_setting")
    if not key:
        return
    value = (message.text or "").strip()
    if not value:
        return

    if key in ("night_start", "night_end"):
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            await message.reply_text("❌ Invalid time. Use HH:MM, for example 23:15.")
            return
        data[key] = value
    elif key == "night_channel":
        if not re.fullmatch(r"-100\d{5,20}", value):
            await message.reply_text("❌ Invalid channel ID. Example: -1001234567890")
            return
        data[key] = value
    else:
        data[key] = value[:4000]

    save_data()
    state.pop("night_setting", None)
    await message.reply_text(
        "✅ Night Mode setting saved.",
        reply_markup=nightmode_keyboard(),
    )


async def send_admin_limit_notice(context, post, admin_name, used_count, limit):
    """Send the limit notice and keep it visible for exactly 5 seconds."""
    display_admin = admin_name if admin_name and admin_name != "Unknown" else "N/A"
    notice = (
        "🔒 𝗙𝗜𝗙 𝗛𝗔𝗥𝗜𝗦𝗛 𝗦𝗘𝗖𝗨𝗥𝗜𝗧𝗬 🔒\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👁 𝗔𝗗𝗠𝗜𝗡 : {escape(str(display_admin))}\n\n"
        f"📊 𝗣𝗢𝗦𝗧 : {used_count}/{limit}\n\n"
        "🚫 𝗟𝗜𝗠𝗜𝗧 𝗥𝗘𝗔𝗖𝗛𝗘𝗗\n\n"
        "𝗣𝗟𝗘𝗔𝗦𝗘 𝗪𝗔𝗜𝗧 𝗣𝗔𝗧𝗜𝗘𝗡𝗧𝗟𝗬, 𝗧𝗛𝗘 𝗟𝗜𝗠𝗜𝗧 𝗪𝗜𝗟𝗟 𝗥𝗘𝗠𝗢𝗩𝗘 𝗦𝗢𝗢𝗡\n\n"
        "𝗥𝗘𝗦𝗘𝗧 𝗔𝗧 𝟭𝟮𝗔𝗠 𝗕𝗗 𝗧𝗜𝗠𝗘\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    try:
        sent = await context.bot.send_message(
            chat_id=post.chat.id,
            text=notice,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        # Mark this exact notice so the channel_post handler cannot delete it.
        ids = data.setdefault("limit_notice_message_ids", [])
        ids.append(int(sent.message_id))
        data["limit_notice_message_ids"] = ids[-100:]
        save_data()

        await asyncio.sleep(5.0)
        try:
            await context.bot.delete_message(
                chat_id=post.chat.id,
                message_id=sent.message_id,
            )
        except Exception as exc:
            print(f"[ADMIN LIMIT NOTICE DELETE ERROR] Channel={post.chat.id}: {exc}")
        finally:
            try:
                ids = data.get("limit_notice_message_ids", [])
                if sent.message_id in ids:
                    ids.remove(sent.message_id)
                    save_data()
            except Exception:
                pass
    except Exception as exc:
        print(f"[ADMIN LIMIT NOTICE ERROR] Channel={post.chat.id}: {exc}")


async def channel_post(update, context):
    post = update.channel_post
    if not post:
        return

    # Never process our temporary limit notice; it must remain visible for 3 seconds.
    if post.message_id in data.get("limit_notice_message_ids", []):
        return

    # Never count or delete posts created by this bot itself (including
    # the limit-reached notice sent below).
    author = getattr(post, "from_user", None)
    if author and getattr(author, "is_bot", False):
        return

    data["total_seen"] += 1
    save_data()

    chat_id = str(post.chat.id)

    # Master ON/OFF switch for all automatic channel-post deletion.
    if not data.get("post_delete_enabled", True):
        return

    # Night Mode is checked BEFORE the approved-channel filter. This is
    # intentional: a channel selected as the Night Mode target must be
    # protected even if it has not also been added to Approved Channels.
    # Telegram channel posts do not reliably expose the individual poster,
    # so Night Mode deletes every new post in the selected channel.
    night_channel = str(data.get("night_channel", "")).strip()
    if (
        data.get("post_delete_enabled", True)
        and not data["maintenance"]
        and not data["locked"]
        and is_night_mode()
        and night_channel
        and chat_id == night_channel
    ):
        # Do not delete the Night Mode announcement sent by this bot itself.
        if data.get("night_last_message_id") == post.message_id:
            return
        try:
            await context.bot.delete_message(
                chat_id=post.chat.id,
                message_id=post.message_id,
            )
            data["total_deleted"] += 1
            save_data()
            print(
                f"[NIGHT MODE DELETE] Channel={post.chat.id} "
                f"Message={post.message_id}"
            )
        except Exception as exc:
            print(f"[NIGHT MODE DELETE ERROR] {exc}")
        return

    # Locked/maintenance systems do not process normal filtering.
    if data["maintenance"] or data["locked"]:
        return

    # ------------------------------------------------------------
    # PER-ADMIN CHANNEL DAILY POST LIMIT
    # Each channel admin gets their own daily counter.
    # Example with limit=2: Admin A can make 2 posts and Admin B can
    # independently make 2 posts on the same day.
    # ------------------------------------------------------------
    try:
        admin_limit = int(data.get("channel_admin_daily_limit", 2))
    except (TypeError, ValueError):
        admin_limit = 2
        data["channel_admin_daily_limit"] = 2
        save_data()

    # Apply the per-admin limit independently to every configured channel.
    # Do not use the old single-channel `channel_daily_target` setting here.
    # If channels are configured, only those channels are protected; if the
    # list is empty, keep the handler available for all channels.
    admin_limit_applies = (not data.get("channels")) or (chat_id in {str(c) for c in data.get("channels", [])})
    if admin_limit > 0 and admin_limit_applies:
        admin_key, admin_name, has_user_id = channel_admin_identity(post)
        # In a Telegram channel, posts are published by channel admins.
        # For identifiable posts we use the author user ID; for anonymous
        # admins Telegram exposes author_signature. Do not call
        # get_chat_member here because channel posts commonly do not expose
        # the human poster as from_user.
        if admin_key:
            admin_count = channel_admin_post_count(chat_id, admin_key)
            print(
                f"[CHANNEL ADMIN LIMIT CHECK] Channel={chat_id} "
                f"Admin={admin_name} count={admin_count} limit={admin_limit} "
                f"message={post.message_id}"
            )
            if admin_count >= admin_limit:
                # Delete the blocked 3rd+ post immediately. The notice below
                # is the message that remains visible for 5 seconds.
                try:
                    await context.bot.delete_message(
                        chat_id=post.chat.id,
                        message_id=post.message_id,
                    )
                    data["total_deleted"] += 1
                    save_data()
                    print(
                        f"[CHANNEL ADMIN LIMIT DELETE] Channel={chat_id} "
                        f"Admin={admin_name} Message={post.message_id} "
                        f"count={admin_count + 1} limit={admin_limit}"
                    )
                except Exception as exc:
                    print(
                        f"[CHANNEL ADMIN LIMIT DELETE ERROR] Channel={chat_id} "
                        f"Message={post.message_id}: {exc}"
                    )

                # The blocked post is the next post after the allowed limit.
                # Example: limit=2 -> this blocked post is shown as 3/2.
                await send_admin_limit_notice(
                    context, post, admin_name, admin_count + 1, admin_limit
                )
                return

            increment_channel_admin_posts(chat_id, admin_key)
            print(
                f"[CHANNEL ADMIN LIMIT ALLOW] Channel={chat_id} "
                f"Admin={admin_name} new_count={admin_count + 1} limit={admin_limit}"
            )

    # Abuse-word filtering still respects the approved-channel list.
    if data["channels"] and chat_id not in data["channels"]:
        return

    text_parts = []
    if post.text:
        text_parts.append(post.text)
    if post.caption:
        text_parts.append(post.caption)

    content = "\n".join(text_parts)
    matched = contains_abuse_word(content)

    if not data.get("post_delete_enabled", True):
        return

    if not matched:
        return

    try:
        await context.bot.delete_message(
            chat_id=post.chat.id,
            message_id=post.message_id,
        )
        data["total_deleted"] += 1
        save_data()
        print(
            f"[DELETE] Channel={post.chat.id} "
            f"Message={post.message_id} Word={matched}"
        )
    except Exception as exc:
        print(f"[DELETE ERROR] {exc}")


async def user_message_limit(update, context):
    # Per-user limits apply to group/supergroup messages.
    # A username added in Admin Panel can have its own limit; otherwise the
    # normal global daily_user_limit is used.
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or user.is_bot or not chat:
        return
    if not data.get("post_delete_enabled", True) or data.get("maintenance") or data.get("locked"):
        return
    if chat.type not in ("group", "supergroup"):
        return
    if message.text and message.text.startswith("/"):
        return

    # Owner is always exempt. Added Admin User IDs have their own limit (default 2/day).
    if is_admin(user.id):
        return

    admin_key, admin_limit_item = admin_user_limit_for(user)
    if admin_limit_item is not None:
        limit = int(admin_limit_item.get("limit", 2))
        count = admin_user_limit_count(admin_limit_item)
        if limit <= 0:
            return
        if count >= limit:
            try:
                await message.delete()
                data["total_deleted"] += 1
                save_data()
                print(f"[ADMIN USER LIMIT] Deleted user={user.id} count={count + 1} limit={limit}")
            except Exception as exc:
                print(f"[ADMIN USER LIMIT DELETE ERROR] {exc}")
            return
        admin_user_limit_increment(admin_limit_item)
        return

    managed_key, managed = managed_user_for(user)
    if managed is not None:
        limit = int(managed.get("limit", 3))
        count = managed_user_count(managed)
        if limit <= 0:
            return
        if count >= limit:
            try:
                await message.delete()
                data["total_deleted"] += 1
                save_data()
                print(f"[USER LIMIT] Deleted @{managed_key} count={count + 1} limit={limit}")
            except Exception as exc:
                print(f"[USER LIMIT DELETE ERROR] {exc}")
            return
        managed_user_increment(managed)
        return

    limit = int(data.get("daily_user_limit", 2))
    if limit <= 0:
        return
    count = user_daily_post_count(user.id)
    if count >= limit:
        try:
            await message.delete()
            data["total_deleted"] += 1
            save_data()
            print(f"[DAILY LIMIT] Deleted user={user.id} count={count + 1}")
        except Exception as exc:
            print(f"[DAILY LIMIT DELETE ERROR] {exc}")
        return
    increment_user_daily_posts(user.id)


def panel_section(action):
    if action == "channels":
        return (
            "🔄 <b>CHANNEL SETTINGS</b>\n\n"
            "Tap ➕ Add Channel and forward any message from the channel.\n"
            "The bot will detect the channel ID automatically.\n\n"
            f"Approved / monitored channels: {len(data['channels'])}"
        )

    if action == "forcejoin":
        return (
            "🌐 <b>FORCE JOIN CHANNELS</b>\n\n"
            "/addforcejoin CHANNEL_ID\n"
            "/removeforcejoin CHANNEL_ID\n\n"
            f"Configured: {len(data['force_join_channels'])}"
        )

    if action == "permission":
        return (
            "🏅 <b>PERMISSION</b>\n\n"
            "/addpermission USER_ID\n"
            "/removepermission USER_ID\n\n"
            f"Permitted users: {len(data['permitted_users'])}\n\n"
            "The owner is controlled by ADMIN_ID."
        )

    if action == "nightmode":
        return (
            "🌙 <b>NIGHT MODE SETTINGS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 Switch : {"ON" if data.get("night_enabled", True) else "OFF"}\n"
            f"🕒 Current : {"ACTIVE" if is_night_mode() else "INACTIVE"}\n"
            f"⏰ Start : {data.get('night_start', '23:15')}\n"
            f"⏰ End : {data.get('night_end', '09:00')}\n"
            f"📢 Channel : {data.get('night_channel') or 'Not set'}\n"
            f"💬 Message : {data.get('night_message', '🟩 Night Mode is now active.')}\n\n"
            "Set these once. The schedule is saved and runs every day.\n\n"
            "Use the buttons below to change the settings."
        )

    if action == "board":
        return (
            "📋 <b>BOARD</b>\n\n"
            "Use /admin for the owner panel.\n"
            "Use /addabuseword WORD to add a filter.\n"
            "Use /listabusewords to view filters.\n"
            "Use /addchannel ID to monitor a channel."
        )

    if action == "help":
        return (
            "📨 <b>HELP</b>\n\n"
            "/admin\n"
            "/addabuseword WORD\n"
            "/removeabuseword WORD\n"
            "/listabusewords\n"
            "/addchannel ID\n"
            "/removechannel ID\n"
            "/channels\n"
            "/addforcejoin ID\n"
            "/removeforcejoin ID\n"
            "/addpermission USER_ID\n"
            "/removepermission USER_ID\n"
            "/stats\n"
            "/channelstatus CHANNEL_ID\n"
            "/setchanneltarget CHANNEL_ID|off\n"
            "/backup\n"
            "/resetstats\n"
            "/status\n"
        )

    if action == "about":
        return (
            "🌐 <b>ABOUT BOT</b>\n\n"
            "Telegram channel post remover with an "
            "admin panel, abuse-word filter, monitoring controls, "
            "maintenance mode, force-join verification, backups, "
            "daily limits and statistics."
        )

    if action == "developer":
        return (
            "👑 <b>DEVELOPER INFO</b>\n\n"
            f"Developer: <b>{escape(str(data.get('developer_name') or 'Not set'))}</b>\n"
            f"Username: <b>{escape(str(data.get('developer_username') or 'Not set'))}</b>"
        )

    if action == "pending":
        if not data["pending_chats"]:
            return "🎯 <b>PENDING CHATS</b>\n\nNo pending chats."
        return (
            "🎯 <b>PENDING CHATS</b>\n\n"
            + "\n".join(
                f"• <code>{x}</code>" for x in data["pending_chats"]
            )
        )

    if action == "approved":
        if not data["channels"]:
            return "✅ <b>APPROVED CHATS</b>\n\nNone configured."
        return (
            "✅ <b>APPROVED CHATS</b>\n\n"
            + "\n".join(
                f"• <code>{x}</code>" for x in data["channels"]
            )
        )

    return None


def nightmode_keyboard():
    enabled = data.get("night_enabled", True)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🌙 Night Mode: {'ON' if enabled else 'OFF'}",
            callback_data="night:toggle",
        )],
        [
            InlineKeyboardButton("⏰ Set Start Time", callback_data="night:set_start"),
            InlineKeyboardButton("⏰ Set End Time", callback_data="night:set_end"),
        ],
        [InlineKeyboardButton("📢 Set Channel", callback_data="night:set_channel")],
        [InlineKeyboardButton("💬 Set Message", callback_data="night:set_message")],
        [InlineKeyboardButton("🧪 Send Test Now", callback_data="night:test")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
    ])


def nightmode_text():
    enabled = data.get("night_enabled", True)
    return (
        "🌙 <b>NIGHT MODE SETTINGS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔘 Switch : <b>{'ON' if enabled else 'OFF'}</b>\n"
        f"📌 Schedule status : <b>{'ACTIVE' if is_night_mode() else 'INACTIVE'}</b>\n"
        f"⏰ Start : {data.get('night_start', '23:15')}\n"
        f"⏰ End : {data.get('night_end', '09:00')}\n"
        f"📢 Channel : {data.get('night_channel') or 'Not set'}\n"
        f"💬 Message : {data.get('night_message', '🟩 Night Mode is now active.')}\n\n"
        "When the switch is OFF, Night Mode will not send announcements or delete channel posts.\n"
        "When ON, the saved schedule is used every day."
    )


async def send_night_message(context, test=False):
    channel = str(data.get("night_channel", "")).strip()
    message = str(data.get("night_message", "🟩 Night Mode is now active.")).strip()
    if not channel or not message:
        return False
    try:
        sent = await context.bot.send_message(chat_id=channel, text=message)
        if not test:
            data["night_last_sent_date"] = datetime.now(BD_TZ).date().isoformat()
            data["night_last_message_id"] = sent.message_id
            save_data()
        return True
    except Exception as exc:
        print(f"[NIGHT MESSAGE ERROR] {exc}")
        return False


async def night_scheduler(context):
    now = datetime.now(BD_TZ)
    if not is_night_mode():
        return
    today = now.date().isoformat()
    if data.get("night_last_sent_date") == today:
        return
    start = get_night_time("night_start", "23:15")
    # Only send after the configured start time, not during the morning part of the window.
    if now.time() < start:
        return
    await send_night_message(context)


async def button(update, context):
    query = update.callback_query
    action = query.data

    # Force-join verification is intentionally available to normal users.
    if action in ("forcejoin:info", "forcejoin:verify"):
        if action == "forcejoin:info":
            await query.answer(
                "Ask the admin to configure a public username or invite link for this channel.",
                show_alert=True,
            )
            return
        await query.answer()
        missing = await check_force_join(context, query.from_user.id)
        if missing:
            await query.edit_message_text(
                "❌ <b>Not verified yet.</b>\n\nJoin all required channels and try again.",
                parse_mode="HTML",
                reply_markup=await force_join_keyboard(context, missing),
            )
        else:
            await query.edit_message_text(
                "✅ <b>Verification successful.</b>\n\nYou can now use the bot.",
                parse_mode="HTML",
                reply_markup=bottom_keyboard(),
            )
        return

    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    await query.answer()

    if action == "channel:add":
        await query.answer("Ask the admin to configure a public username or invite link for this channel.", show_alert=True)
        return

    if action == "forcejoin:verify":
        missing = await check_force_join(context, query.from_user.id)
        if missing:
            await query.edit_message_text(
                "❌ <b>Not verified yet.</b>\n\nJoin all required channels and try again.",
                parse_mode="HTML",
                reply_markup=await force_join_keyboard(context, missing),
            )
        else:
            await query.edit_message_text(
                "✅ <b>Verification successful.</b>\n\nYou can now use the bot.",
                parse_mode="HTML",
                reply_markup=bottom_keyboard(),
            )
        return

    if action == "channel:add":
        context.user_data["waiting_for_channel_forward"] = True
        await query.message.reply_text(
            "📢 <b>ADD CHANNEL</b>\n\n"
            "Please forward any message from the channel to this bot.\n"
            "I will detect the channel automatically.",
            parse_mode="HTML",
        )
        return

    if action == "channel:confirm":
        channel_id = context.user_data.get("pending_channel_id")
        if not channel_id:
            await query.answer("No pending channel found.", show_alert=True)
            return
        if channel_id not in data["channels"]:
            data["channels"].append(channel_id)
            save_data()
        title = context.user_data.get("pending_channel_title", channel_id)
        context.user_data.pop("pending_channel_id", None)
        context.user_data.pop("pending_channel_title", None)
        context.user_data.pop("pending_channel_username", None)
        await query.edit_message_text(
            f"✅ <b>Channel added</b>\n\n{title}\n<code>{channel_id}</code>\n\n"
            "Make sure the bot is an administrator in that channel with permission to delete messages.",
            parse_mode="HTML",
        )
        return

    if action == "channel:cancel":
        context.user_data.pop("pending_channel_id", None)
        context.user_data.pop("pending_channel_title", None)
        context.user_data.pop("pending_channel_username", None)
        context.user_data["waiting_for_channel_forward"] = False
        await query.edit_message_text("❌ Channel adding cancelled.")
        return

    if action in ("admin", "refresh"):
        await query.edit_message_text(
            admin_text(),
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    if action == "stats":
        await query.message.reply_text(
            "🏆 <b>BOT STATS</b>\n\n"
            f"👀 Posts checked : {data['total_seen']}\n"
            f"🗑️ Posts deleted : {data['total_deleted']}\n"
            f"🚨 Filter words : {len(data['abuse_words'])}\n"
            f"✅ Approved chats : {len(data['channels'])}\n"
            f"🌐 Force-join channels : {len(data['force_join_channels'])}\n"
            f"🏅 Permitted users : {len(data['permitted_users'])}",
            parse_mode="HTML",
        )
        return

    if action == "nightmode":
        await query.edit_message_text(
            nightmode_text(), parse_mode="HTML", reply_markup=nightmode_keyboard()
        )
        return

    if action == "channels":
        await query.edit_message_text(
            panel_section("channels"),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Channel", callback_data="channel:add")],
                [InlineKeyboardButton("📋 View Channels", callback_data="approved")],
                [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
            ]),
        )
        return

    if action == "night:toggle":
        data["night_enabled"] = not data.get("night_enabled", True)
        if not data["night_enabled"]:
            data["night_last_sent_date"] = ""
            data["night_last_message_id"] = None
        save_data()
        await query.edit_message_text(
            nightmode_text(), parse_mode="HTML", reply_markup=nightmode_keyboard()
        )
        return

    if action in ("night:set_start", "night:set_end", "night:set_channel", "night:set_message"):
        prompts = {
            "night:set_start": ("night_start", "Send the Night Mode start time as HH:MM (24-hour). Example: 23:15"),
            "night:set_end": ("night_end", "Send the Night Mode end time as HH:MM (24-hour). Example: 09:00"),
            "night:set_channel": ("night_channel", "Send the target channel ID, for example -1001234567890. The bot must be an administrator there."),
            "night:set_message": ("night_message", "Send the message the bot should send when Night Mode starts."),
        }
        key, prompt = prompts[action]
        state = get_user_state(context, query.from_user.id)
        state["night_setting"] = key
        await query.message.reply_text(prompt)
        return

    if action == "night:test":
        ok = await send_night_message(context, test=True)
        await query.answer("Test message sent." if ok else "Set a valid channel and make the bot an admin there.", show_alert=True)
        return

    if action == "admin_user_limits":
        admins = data.get("admin_user_limits", {})
        reset_admin_user_limits()
        if not admins:
            text = (
                "👑 <b>ADMIN USER LIMITS</b>\n\n"
                "No admin user IDs configured yet.\n\n"
                "Add a Telegram numeric user ID. Default limit: <b>2 posts/day</b>."
            )
        else:
            lines = ["👑 <b>ADMIN USER LIMITS</b>", ""]
            for key, item in admins.items():
                name = item.get("display_name") or key
                username = item.get("username")
                who = f"@{username}" if username else name
                count = int(item.get("count", 0))
                limit = int(item.get("limit", 2))
                lines.append(f"• <b>{who}</b> — ID: <code>{key}</code> — Today: <b>{count}</b>/<b>{limit if limit > 0 else 'OFF'}</b>")
            text = "\n".join(lines)
        await query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Admin User ID", callback_data="admin_user_limits:add")],
                [InlineKeyboardButton("🔄 Reset Today's Counts", callback_data="admin_user_limits:reset")],
                [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
            ])
        )
        return

    if action == "admin_user_limits:add":
        context.user_data["waiting_for_admin_user_id"] = True
        await query.message.reply_text(
            "👑 Send the admin's Telegram numeric User ID.\n\nExample: <code>123456789</code>",
            parse_mode="HTML",
        )
        return

    if action == "admin_user_limits:reset":
        today = datetime.now(BD_TZ).date().isoformat()
        for item in data.setdefault("admin_user_limits", {}).values():
            item["date"] = today
            item["count"] = 0
        save_data()
        await query.edit_message_text(
            "✅ <b>Today's admin-user counts have been reset.</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin User Limits", callback_data="admin_user_limits")]])
        )
        return

    if action.startswith("adminuserlimit:"):
        parts = action.split(":")
        if len(parts) != 3:
            return
        user_id = parts[1]
        try:
            new_limit = int(parts[2])
        except ValueError:
            await query.answer("Invalid limit.", show_alert=True)
            return
        item = data.get("admin_user_limits", {}).get(user_id)
        if not item:
            await query.answer("Admin user ID not found.", show_alert=True)
            return
        item["limit"] = new_limit
        save_data()
        count = admin_user_limit_count(item)
        name = item.get("display_name") or user_id
        await query.edit_message_text(
            f"👑 <b>{name}</b>\n\nID: <code>{user_id}</code>\n📊 Posts today: <b>{count}</b>\n🎯 Daily limit: <b>{new_limit if new_limit > 0 else 'OFF'}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1", callback_data=f"adminuserlimit:{user_id}:1"), InlineKeyboardButton("2", callback_data=f"adminuserlimit:{user_id}:2"), InlineKeyboardButton("3", callback_data=f"adminuserlimit:{user_id}:3")],
                [InlineKeyboardButton("5", callback_data=f"adminuserlimit:{user_id}:5"), InlineKeyboardButton("10", callback_data=f"adminuserlimit:{user_id}:10"), InlineKeyboardButton("OFF", callback_data=f"adminuserlimit:{user_id}:0")],
                [InlineKeyboardButton("⬅️ Admin User Limits", callback_data="admin_user_limits")],
            ])
        )
        return

    if action == "user_limits":
        users = data.get("managed_users", {})
        if not users:
            text = "👤 <b>USER DAILY LIMITS</b>\n\nNo users configured yet.\n\nSend a Telegram username such as <code>@john</code> to add one."
        else:
            reset_managed_users()
            lines = ["👤 <b>USER DAILY LIMITS</b>", ""]
            for key, item in users.items():
                username = item.get("display_username") or key
                limit = item.get("limit", 3)
                count = item.get("count", 0)
                lines.append(f"• @{username.lstrip('@')} — Today: <b>{count}</b> — Limit: <b>{limit if int(limit) > 0 else 'OFF'}</b>")
            lines += ["", "Send a username to add/view a user."]
            text = "\n".join(lines)
        await query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add / View Username", callback_data="user_limits:add")], [InlineKeyboardButton("⬅️ Back", callback_data="admin")]])
        )
        return

    if action == "user_limits:add":
        context.user_data["waiting_for_username"] = True
        await query.message.reply_text("👤 Send the Telegram username, for example: <code>@john</code>", parse_mode="HTML")
        return

    if action.startswith("userlimit:"):
        parts = action.split(":")
        if len(parts) != 3:
            return
        username = normalize_username(parts[1])
        try:
            new_limit = int(parts[2])
        except ValueError:
            await query.answer("Invalid limit.", show_alert=True)
            return
        item = data.get("managed_users", {}).get(username)
        if not item:
            await query.answer("User not found.", show_alert=True)
            return
        item["limit"] = new_limit
        save_data()
        count = managed_user_count(item)
        await query.edit_message_text(
            f"👤 <b>@{username}</b>\n\n📊 Posts today: <b>{count}</b>\n🎯 Daily limit: <b>{new_limit if new_limit > 0 else 'OFF'}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ User Limits", callback_data="user_limits")]])
        )
        return

    if action == "channel_admin_daily_limit":
        limit = int(data.get("channel_admin_daily_limit", 2))
        await query.edit_message_text(
            f"👮 <b>PER-ADMIN CHANNEL POST LIMIT</b>\n\nCurrent limit: <b>{limit if limit > 0 else 'OFF'}</b> posts per admin per day.\n\nChoose a new limit:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("1", callback_data="setadminchlimit:1"),
                    InlineKeyboardButton("2", callback_data="setadminchlimit:2"),
                    InlineKeyboardButton("3", callback_data="setadminchlimit:3"),
                    InlineKeyboardButton("5", callback_data="setadminchlimit:5"),
                ],
                [
                    InlineKeyboardButton("10", callback_data="setadminchlimit:10"),
                    InlineKeyboardButton("20", callback_data="setadminchlimit:20"),
                    InlineKeyboardButton("50", callback_data="setadminchlimit:50"),
                    InlineKeyboardButton("OFF", callback_data="setadminchlimit:0"),
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
            ]),
        )
        return

    if action.startswith("setadminchlimit:"):
        try:
            new_limit = int(action.split(":", 1)[1])
            if new_limit < 0 or new_limit > 1000:
                raise ValueError
        except ValueError:
            await query.answer("Invalid limit.", show_alert=True)
            return
        data["channel_admin_daily_limit"] = new_limit
        save_data()
        await query.edit_message_text(
            admin_text(),
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    if action == "channel_daily_limit":
        limit = int(data.get("channel_daily_limit", 0))
        await query.edit_message_text(
            f"📢 <b>CHANNEL DAILY POST LIMIT</b>\n\nCurrent limit: <b>{limit if limit > 0 else 'OFF'}</b> total posts per channel per day.\n\nChoose a new limit:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("1", callback_data="setchannelimit:1"),
                    InlineKeyboardButton("2", callback_data="setchannelimit:2"),
                    InlineKeyboardButton("3", callback_data="setchannelimit:3"),
                    InlineKeyboardButton("5", callback_data="setchannelimit:5"),
                ],
                [
                    InlineKeyboardButton("10", callback_data="setchannelimit:10"),
                    InlineKeyboardButton("20", callback_data="setchannelimit:20"),
                    InlineKeyboardButton("50", callback_data="setchannelimit:50"),
                    InlineKeyboardButton("OFF", callback_data="setchannelimit:0"),
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
            ]),
        )
        return

    if action.startswith("setchannelimit:"):
        try:
            new_limit = int(action.split(":", 1)[1])
            if new_limit < 0 or new_limit > 1000:
                raise ValueError
        except ValueError:
            await query.answer("Invalid limit.", show_alert=True)
            return
        data["channel_daily_limit"] = new_limit
        save_data()
        await query.edit_message_text(
            admin_text(),
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    if action == "resetchannelcount":
        data["daily_channel_date"] = datetime.now(BD_TZ).date().isoformat()
        data["daily_channel_posts"] = {}
        save_data()
        await query.edit_message_text(
            "✅ <b>Today's channel post count has been reset.</b>\n\n"
            f"📊 Limit: <b>{int(data.get('channel_daily_limit', 1)) if int(data.get('channel_daily_limit', 1)) > 0 else 'OFF'}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin")]])
        )
        return

    if action == "daily_limit":
        limit = int(data.get("daily_user_limit", 2))
        await query.edit_message_text(
            f"📊 <b>DAILY POST LIMIT</b>\n\nCurrent limit: <b>{limit if limit > 0 else 'OFF'}</b> posts per user per day\n\nChoose a new limit:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("1", callback_data="setlimit:1"),
                    InlineKeyboardButton("2", callback_data="setlimit:2"),
                    InlineKeyboardButton("3", callback_data="setlimit:3"),
                    InlineKeyboardButton("5", callback_data="setlimit:5"),
                ],
                [
                    InlineKeyboardButton("10", callback_data="setlimit:10"),
                    InlineKeyboardButton("20", callback_data="setlimit:20"),
                    InlineKeyboardButton("50", callback_data="setlimit:50"),
                    InlineKeyboardButton("OFF", callback_data="setlimit:0"),
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
            ]),
        )
        return

    if action.startswith("setlimit:"):
        try:
            new_limit = int(action.split(":", 1)[1])
            if new_limit < 0 or new_limit > 1000:
                raise ValueError
        except ValueError:
            await query.answer("Invalid limit.", show_alert=True)
            return
        data["daily_user_limit"] = new_limit
        save_data()
        await query.edit_message_text(
            admin_text(),
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    if action == "post_delete_toggle":
        data["post_delete_enabled"] = not data.get("post_delete_enabled", True)
        save_data()
        state = "ON" if data["post_delete_enabled"] else "OFF"
        await query.edit_message_text(
            f"🗑️ <b>POST DELETE: {state}</b>\n\n"
            + ("Automatic post deletion is enabled." if data["post_delete_enabled"] else "Automatic post deletion is completely disabled. The bot will not delete channel/group posts automatically."),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🔴 Post Delete: {state}", callback_data="post_delete_toggle")], [InlineKeyboardButton("⬅️ Back", callback_data="admin")]]),
        )
        return

    if action == "system_tools":
        await query.edit_message_text(
            "🛠️ <b>SYSTEM TOOLS</b>\n\n"
            "Use these tools to protect your settings and reset counters.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 Create Backup", callback_data="system:backup")],
                [InlineKeyboardButton("🔄 Reset Daily Counters", callback_data="system:reset_daily")],
                [InlineKeyboardButton("📊 Reset Statistics", callback_data="system:reset_stats")],
                [InlineKeyboardButton("🩺 System Status", callback_data="system:status")],
                [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
            ]),
        )
        return

    if action.startswith("system:"):
        if action == "system:backup":
            try:
                save_data()
                backup = DATA_FILE.with_name(
                    f"{DATA_FILE.stem}_backup_{datetime.now(BD_TZ).strftime('%Y%m%d_%H%M%S')}.json"
                )
                shutil.copy2(DATA_FILE, backup)
                data["last_backup"] = datetime.now(BD_TZ).isoformat(timespec="seconds")
                save_data()
                with backup.open("rb") as fh:
                    await query.message.reply_document(document=fh, filename=backup.name, caption="✅ Backup created.")
            except Exception as exc:
                data["last_error"] = str(exc)[:500]
                save_data()
                await query.answer("Backup failed.", show_alert=True)
            return

        if action == "system:reset_daily":
            today = datetime.now(BD_TZ).date().isoformat()
            data["daily_user_date"] = today
            data["daily_user_posts"] = {}
            data["daily_channel_date"] = today
            data["daily_channel_posts"] = {}
            for item in data.get("managed_users", {}).values():
                item["date"] = today
                item["count"] = 0
            for item in data.get("admin_user_limits", {}).values():
                item["date"] = today
                item["count"] = 0
            save_data()
            await query.answer("Daily counters reset.", show_alert=True)
            await query.edit_message_text(
                admin_text(), parse_mode="HTML", reply_markup=main_keyboard()
            )
            return

        if action == "system:reset_stats":
            data["total_seen"] = 0
            data["total_deleted"] = 0
            save_data()
            await query.answer("Statistics reset.", show_alert=True)
            await query.edit_message_text(
                admin_text(), parse_mode="HTML", reply_markup=main_keyboard()
            )
            return

        if action == "system:status":
            job_queue = getattr(context.application, "job_queue", None)
            await query.edit_message_text(
                "🩺 <b>SYSTEM STATUS</b>\n\n"
                f"🤖 Process: <b>RUNNING</b>\n"
                f"💾 Data: <b>{'OK' if DATA_FILE.exists() else 'MISSING'}</b>\n"
                f"⏱ Scheduler: <b>{'AVAILABLE' if job_queue else 'UNAVAILABLE'}</b>\n"
                f"🗑 Delete: <b>{'ON' if data.get('post_delete_enabled', True) else 'OFF'}</b>\n"
                f"🔒 Lock: <b>{'ON' if data.get('locked') else 'OFF'}</b>\n"
                f"⚙️ Maintenance: <b>{'ON' if data.get('maintenance') else 'OFF'}</b>\n"
                f"❗ Last error: <code>{escape(str(data.get('last_error') or 'None'))}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="system_tools")]]),
            )
            return

    if action == "maintenance":
        data["maintenance"] = not data["maintenance"]
        save_data()
        await query.edit_message_text(
            admin_text(),
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    if action == "lock":
        data["locked"] = not data["locked"]
        save_data()
        await query.edit_message_text(
            admin_text(),
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    if action == "approved" and data["channels"]:
        text = panel_section(action)
    else:
        text = panel_section(action)

    if text:
        await query.message.reply_text(text, parse_mode="HTML")
        return


async def error_handler(update, context):
    error = getattr(context, "error", None)
    if error and "Message is not modified" in str(error):
        return
    data["last_error"] = str(error)[:1000]
    try:
        save_data()
    except Exception:
        pass
    print(f"[ERROR] {error!r}")


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN environment variable is not set. Run: export BOT_TOKEN=\"YOUR_BOT_TOKEN\""
        )

    if ADMIN_ID == 123456789:
        raise SystemExit(
            "Set ADMIN_ID to your Telegram numeric user ID before starting."
        )

    async def post_init(application):
        await application.bot.set_my_commands([
            ("start", "Open welcome menu"),
            ("admin", "Open admin panel"),
            ("stats", "View bot statistics"),
            ("listabusewords", "View abuse-word filter"),
            ("channels", "View monitored channels"),
            ("help", "Show help"),
            ("backup", "Create settings backup"),
            ("status", "Check bot status"),
        ])

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # JobQueue is an optional python-telegram-bot extra.
    # The bot can still run without it; only automatic Night Mode scheduling is disabled.
    if getattr(app, "job_queue", None):
        app.job_queue.run_repeating(night_scheduler, interval=60, first=5)
    else:
        print("[WARNING] JobQueue is unavailable. Install: pip install \"python-telegram-bot[job-queue]\"")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("addabuseword", add_abuse_word))
    app.add_handler(CommandHandler("removeabuseword", remove_abuse_word))
    app.add_handler(CommandHandler("listabusewords", list_abuse_words))
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("removechannel", remove_channel))
    app.add_handler(CommandHandler("channels", list_channels))
    app.add_handler(CommandHandler("addforcejoin", add_forcejoin))
    app.add_handler(CommandHandler("removeforcejoin", remove_forcejoin))
    app.add_handler(CommandHandler("addpermission", add_permission))
    app.add_handler(CommandHandler("removepermission", remove_permission))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("channelstatus", channel_status))
    app.add_handler(CommandHandler("setchanneltarget", set_channel_target))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("resetstats", reset_stats_command))
    app.add_handler(CommandHandler("status", status_command))

    # Forwarded channel messages used by Admin Panel → Channel Settings → Add Channel.
    # Channel posts must be handled before broad ALL-message handlers.
    # Otherwise a broad handler can consume the update and channel_post()
    # never gets a chance to delete it.
    app.add_handler(
        MessageHandler(filters.UpdateType.CHANNEL_POST, channel_post),
        group=-2,
    )

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_forwarded_channel), group=-1)

    # Persistent bottom menu buttons
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, user_message_limit), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_user_id_input), group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, username_limit_input), group=3)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, night_setting_input), group=4)
    app.add_handler(CallbackQueryHandler(button))
    app.add_error_handler(error_handler)

    print("SHADMAN-STYLE CHANNEL POST REMOVER is running...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()

