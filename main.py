import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple
from dotenv import load_dotenv
import re
from telegram import Update, ChatPermissions, MessageEntity
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ChatJoinRequestHandler, ContextTypes, filters

warnings_store: Dict[Tuple[int, int], int] = {}

load_dotenv()

async def is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    member = await context.bot.get_chat_member(chat_id, user_id)
    return member.status in ("administrator", "creator")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        await update.message.reply_text("Bot active. Use /help for commands.")
    else:
        await update.message.reply_text("Add me to a group and make me admin.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Commands:\n"
        "/start – Activate bot\n"
        "/help – Show this help\n"
        "/status – Show bot permissions\n"
        "/ban (reply | ID | @username | mention) – Ban user\n"
        "/unban (reply | ID | @username | mention) – Unban user\n"
        "/mute (reply | ID | @username | mention) – Mute user\n"
        "/unmute (reply | ID | @username | mention) – Unmute user\n"
        "/warn (reply | ID | @username | mention) – Add warning, auto-mute at 3\n"
        "\n"
        "Auto actions:\n"
        "- Delete links in text/captions; warn non-admins, auto-mute at 3\n"
        "- Delete edited messages; warn non-admins, auto-mute at 3\n"
        "- Delete admin links/edits with notice\n"
        "- Welcome new members; approve join requests"
    )
    await update.message.reply_text(text)

def _target_from_reply(update: Update) -> int | None:
    if update.message and update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user.id
    return None

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id
    if not await is_admin(context, chat_id, admin_id):
        return
    target_id = await resolve_target_user_id(update, context)
    if not target_id:
        await update.message.reply_text("Provide target by reply, mention, or user ID.")
        return
    await context.bot.ban_chat_member(chat_id, target_id)
    await update.message.reply_text("User banned.")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id
    if not await is_admin(context, chat_id, admin_id):
        return
    target_id = await resolve_target_user_id(update, context)
    if not target_id:
        await update.message.reply_text("Provide target by reply, mention, or user ID.")
        return
    perms = ChatPermissions(can_send_messages=False, can_send_audios=False, can_send_documents=False, can_send_photos=False, can_send_videos=False, can_send_video_notes=False, can_send_voice_notes=False, can_send_polls=False, can_add_web_page_previews=False, can_change_info=False, can_invite_users=False, can_pin_messages=False, can_manage_topics=False)
    await context.bot.restrict_chat_member(chat_id, target_id, permissions=perms)
    await update.message.reply_text("User muted.")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id
    if not await is_admin(context, chat_id, admin_id):
        return
    target_id = await resolve_target_user_id(update, context)
    if not target_id:
        await update.message.reply_text("Provide target by reply, mention, or user ID.")
        return
    perms = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_add_web_page_previews=True)
    await context.bot.restrict_chat_member(chat_id, target_id, permissions=perms)
    await update.message.reply_text("User unmuted.")

async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id
    if not await is_admin(context, chat_id, admin_id):
        return
    target_id = await resolve_target_user_id(update, context)
    if not target_id:
        await update.message.reply_text("Provide target by reply, mention, or user ID.")
        return
    count, muted = await apply_warning(context, chat_id, target_id)
    admin_user = update.effective_user
    target_user = update.message.reply_to_message.from_user if update.message and update.message.reply_to_message else None
    admin_mention = f'<a href="tg://user?id={admin_user.id}">{admin_user.first_name}</a>'
    target_mention = f'<a href="tg://user?id={target_id}">{target_user.first_name if target_user else "user"}</a>'
    if muted:
        await context.bot.send_message(chat_id, f"{admin_mention} warned {target_mention}. Auto-muted for 24h.", parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(chat_id, f"{admin_mention} warned {target_mention}. Warnings: {count}/3", parse_mode=ParseMode.HTML)

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id
    if not await is_admin(context, chat_id, admin_id):
        return
    target_id = await resolve_target_user_id(update, context)
    if not target_id:
        await update.message.reply_text("Provide target by reply, mention, or user ID.")
        return
    try:
        await context.bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
        await update.message.reply_text("User unbanned.")
    except Exception:
        await update.message.reply_text("Failed to unban user.")

async def greet_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    for member in update.message.new_chat_members:
        await context.bot.send_message(chat.id, f"Welcome, {member.first_name}!")

async def delete_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return
    chat_id = msg.chat.id
    user_id = msg.from_user.id if msg.from_user else None
    if not user_id:
        return
    entities = list(msg.entities or ()) + list(msg.caption_entities or ())
    has_link = any(e.type in (MessageEntity.URL, MessageEntity.TEXT_LINK) for e in entities)
    if not has_link:
        text = (msg.text or "") + " " + (msg.caption or "")
        if text:
            url_regex = re.compile(r"(?i)(https?://|www\.)\S+|t\.me/\S+")
            has_link = bool(url_regex.search(text))
    if has_link:
        try:
            await msg.delete()
        except Exception:
            pass
        admin = await is_admin(context, chat_id, user_id)
        if not admin:
            count, muted = await apply_warning(context, chat_id, user_id)
            mention = f'<a href="tg://user?id={user_id}">{msg.from_user.first_name}</a>'
            if muted:
                await context.bot.send_message(chat_id, f"{mention} auto-muted for 24h due to sending links.", parse_mode=ParseMode.HTML)
                try:
                    await context.bot.send_message(user_id, "You have been auto-muted for 24h due to sending links.")
                except Exception:
                    pass
            else:
                await context.bot.send_message(chat_id, f"{mention} warned for sending links. Warnings: {count}/3", parse_mode=ParseMode.HTML)
                try:
                    await context.bot.send_message(user_id, f"Your message with a link was removed. Warnings: {count}/3")
                except Exception:
                    pass
        else:
            mention = f'<a href="tg://user?id={user_id}">{msg.from_user.first_name}</a>'
            await context.bot.send_message(chat_id, f"Admin link removed: {mention}", parse_mode=ParseMode.HTML)
            try:
                await context.bot.send_message(user_id, "Your message with a link was removed.")
            except Exception:
                pass

async def approve_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    req = update.chat_join_request
    if req:
        try:
            await req.approve()
        except Exception:
            pass

async def apply_warning(context: ContextTypes.DEFAULT_TYPE, chat_id: int, target_id: int) -> tuple[int, bool]:
    key = (chat_id, target_id)
    count = warnings_store.get(key, 0) + 1
    warnings_store[key] = count
    if count >= 3:
        until = datetime.now(timezone.utc) + timedelta(hours=24)
        perms = ChatPermissions(can_send_messages=False, can_send_audios=False, can_send_documents=False, can_send_photos=False, can_send_videos=False, can_send_video_notes=False, can_send_voice_notes=False, can_send_polls=False, can_add_web_page_previews=False)
        await context.bot.restrict_chat_member(chat_id, target_id, permissions=perms, until_date=until)
        warnings_store[key] = 0
        return 3, True
    return count, False

async def on_edited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.edited_message
    if not msg:
        return
    if msg.chat.type not in ("group", "supergroup"):
        return
    chat_id = msg.chat.id
    user_id = msg.from_user.id if msg.from_user else None
    if not user_id:
        return
    if msg.from_user and msg.from_user.is_bot:
        return
    admin = await is_admin(context, chat_id, user_id)
    try:
        await context.bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass
    if not admin:
        count, muted = await apply_warning(context, chat_id, user_id)
        if muted:
            mention = f'<a href="tg://user?id={user_id}">{msg.from_user.first_name}</a>'
            await context.bot.send_message(chat_id, f"{mention} auto-muted for 24h due to editing messages.", parse_mode=ParseMode.HTML)
            try:
                await context.bot.send_message(user_id, "You have been auto-muted for 24h due to editing messages.")
            except Exception:
                pass
        else:
            mention = f'<a href="tg://user?id={user_id}">{msg.from_user.first_name}</a>'
            await context.bot.send_message(chat_id, f"{mention} warned for editing. Warnings: {count}/3", parse_mode=ParseMode.HTML)
            try:
                await context.bot.send_message(user_id, f"Your edited message was removed. Warnings: {count}/3")
            except Exception:
                pass
    else:
        mention = f'<a href="tg://user?id={user_id}">{msg.from_user.first_name}</a>'
        await context.bot.send_message(chat_id, f"Admin warned for editing: {mention}", parse_mode=ParseMode.HTML)
        try:
            await context.bot.send_message(user_id, "Your edited message was removed.")
        except Exception:
            pass

async def resolve_target_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if update.message and update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user.id
    if update.message:
        entities = update.message.entities or []
        for e in entities:
            if e.type == MessageEntity.TEXT_MENTION and e.user:
                return e.user.id
    args = context.args or []
    chat_id = update.effective_chat.id
    if args:
        arg = args[0]
        if arg.isdigit():
            return int(arg)
        if arg.startswith("@"):
            uname = arg[1:].lower()
            try:
                admins = await context.bot.get_chat_administrators(chat_id)
                for cm in admins:
                    if cm.user.username and cm.user.username.lower() == uname:
                        return cm.user.id
            except Exception:
                pass
    if update.message:
        text = update.message.text or ""
        for e in update.message.entities or []:
            if e.type == MessageEntity.MENTION:
                mention_text = text[e.offset : e.offset + e.length]
                uname = mention_text.lstrip("@").lower()
                try:
                    admins = await context.bot.get_chat_administrators(chat_id)
                    for cm in admins:
                        if cm.user.username and cm.user.username.lower() == uname:
                            return cm.user.id
                except Exception:
                    pass
    return None

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    me = await context.bot.get_me()
    member = await context.bot.get_chat_member(chat.id, me.id)
    status = member.status
    lines = [f"Bot status: {status}"]
    fields = [
        ("can_delete_messages", "Delete messages"),
        ("can_restrict_members", "Restrict members"),
        ("can_invite_users", "Invite users"),
        ("can_pin_messages", "Pin messages"),
        ("can_manage_topics", "Manage topics"),
        ("can_change_info", "Change info"),
    ]
    for key, label in fields:
        val = getattr(member, key, None)
        if val is not None:
            lines.append(f"{label}: {'✅' if val else '❌'}")
    await context.bot.send_message(chat.id, "\n".join(lines))

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    # if not token:
    #     raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ban", ban, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("unban", unban, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("mute", mute, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("unmute", unmute, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("warn", warn, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("status", status_cmd, filters=filters.ChatType.GROUPS))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS & filters.ChatType.GROUPS, greet_new_members))
    app.add_handler(MessageHandler(filters.TEXT & (filters.Entity(MessageEntity.URL) | filters.Entity(MessageEntity.TEXT_LINK)) & filters.ChatType.GROUPS, delete_links))
    app.add_handler(MessageHandler(filters.CAPTION & (filters.CaptionEntity(MessageEntity.URL) | filters.CaptionEntity(MessageEntity.TEXT_LINK)) & filters.ChatType.GROUPS, delete_links))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, delete_links))
    app.add_handler(ChatJoinRequestHandler(approve_join))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED & filters.ChatType.GROUPS, on_edited))
    print("Application started")
    app.run_polling(allowed_updates=["message", "edited_message", "chat_member", "my_chat_member", "chat_join_request"])

if __name__ == "__main__":
    main()
