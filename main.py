import os
import random
import string
import re
import asyncio
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)
import database as db

# ==========================================================
# ⚙️ Settings & Credentials
# ==========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Topic ID သတ်မှတ်ချက်များ (General သီးသန့် Topic ရှိပါက ID ထည့်ပါ၊ မရှိပါက None)
GENERAL_TOPIC_ID = None
SPIN_TOPIC_ID = None

# စကားဝိုင်း ၁၀၀ ပြည့်လျှင် Token Drop ကျမည့် စနစ်
MESSAGE_TARGET = 100

chat_stats = {
    "count": 0,
    "last_user_id": None,
    "active_drop_code": None,
    "drop_message_id": None
}

# ==========================================================
# 🌐 Render Dummy Web Server (Port 8080)
# ==========================================================
async def dummy_health_check(request):
    return web.Response(text="Bot is running smoothly!")

async def start_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", dummy_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Server bound to port {port}")

# ==========================================================
# 🛠️ Helper Functions
# ==========================================================
def generate_drop_code() -> str:
    """English စာလုံး ၃ လုံး နှင့် နံပါတ် ၂ လုံး ရောနှောထုတ်ပေးခြင်း"""
    letters = random.choices(string.ascii_uppercase, k=3)
    digits = random.choices(string.digits, k=2)
    combined = letters + digits
    random.shuffle(combined)
    return "".join(combined)

def is_valid_message(text: str) -> bool:
    clean_text = text.strip()
    if len(clean_text) < 5:
        return False
    if len(clean_text.split()) < 2:
        return False
    if clean_text.isdigit():
        return False
    if re.fullmatch(r"[\W_]+", clean_text):
        return False
    return True

# ==========================================================
# 👥 လူ Add လျှင် Token ပေးသည့် စနစ်
# ==========================================================
async def handle_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    inviter = msg.from_user

    if not msg or not msg.new_chat_members or not inviter or inviter.is_bot:
        return

    for new_member in msg.new_chat_members:
        if new_member.is_bot or new_member.id == inviter.id:
            continue

        await db.get_or_create_user(inviter.id, inviter.username, inviter.first_name)
        awarded = await db.record_invitation(inviter.id, new_member.id)

        if awarded:
            try:
                inviter_mention = f"@{inviter.username}" if inviter.username else f"<a href='tg://user?id={inviter.id}'>{inviter.first_name}</a>"
                await msg.reply_text(
                    f"🎉 ဂုဏ်ယူပါတယ် {inviter_mention}!\n"
                    f"Member ၂ ယောက် ဖိတ်ခေါ်မှု ပြည့်မြောက်သဖြင့် 🎟️ <b>Spin Token ၁ ခု</b> ရရှိပါသည်!",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

# ==========================================================
# 💬 General Chat စကားဝိုင်းနှင့် Drop ကောက်ယူခြင်း
# ==========================================================
async def handle_general_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not msg or not user or user.is_bot:
        return

    msg_topic_id = getattr(msg, "message_thread_id", None)
    if SPIN_TOPIC_ID and msg_topic_id == SPIN_TOPIC_ID:
        return
    if GENERAL_TOPIC_ID is not None and msg_topic_id != GENERAL_TOPIC_ID:
        return

    text = msg.text or ""

    # Drop Code အား Reply ဖြင့် လုကောက်ခြင်း စစ်ဆေးမှု
    if chat_stats["active_drop_code"] and msg.reply_to_message:
        if msg.reply_to_message.message_id == chat_stats["drop_message_id"]:
            entered_code = text.strip().upper()
            if entered_code == chat_stats["active_drop_code"]:
                target_code = chat_stats["active_drop_code"]
                drop_msg_id = chat_stats["drop_message_id"]

                chat_stats["active_drop_code"] = None
                chat_stats["drop_message_id"] = None

                await db.get_or_create_user(user.id, user.username, user.first_name)
                total_tokens = await db.give_free_token(user.id)

                winner_mention = f"@{user.username}" if user.username else f"<a href='tg://user?id={user.id}'>{user.first_name}</a>"

                await context.bot.send_message(
                    chat_id=chat.id,
                    message_thread_id=GENERAL_TOPIC_ID,
                    text=(
                        f"⚡ <b>အမြန်ဆုံး ကောက်ယူသွားပါပြီ!</b>\n\n"
                        f"🎉 ဂုဏ်ယူပါတယ် {winner_mention}! Code <code>[{target_code}]</code> အား အမြန်ဆုံး အတိအကျ ရိုက်ထည့်နိုင်ခဲ့သဖြင့် "
                        f"🎟️ <b>Free Spin Token ၁ ခု</b> ရရှိသွားပါပြီ!\n\n"
                        f"👉 {winner_mention} ၏ လက်ကျန် Token: <b>{total_tokens} ခု</b>\n"
                        f"<i>(Spin Topic တွင် သွားရောက် ကံစမ်းနိုင်ပါပြီ)</i>"
                    ),
                    parse_mode=ParseMode.HTML
                )

                try:
                    await context.bot.edit_message_text(
                        chat_id=chat.id,
                        message_id=drop_msg_id,
                        text=(
                            f"🎁 <b>Token Drop ကောက်ယူပြီးစီးသွားပါပြီ!</b>\n\n"
                            f"🏆 ကံထူးရှင်: {winner_mention}\n"
                            f"🔑 Code: <code>{target_code}</code>"
                        ),
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass
                return

    # စာကြောင်းရေ ရေတွက်ခြင်း
    if not is_valid_message(text):
        return

    if chat_stats["last_user_id"] == user.id:
        return

    chat_stats["count"] += 1
    chat_stats["last_user_id"] = user.id

    # အစောင်ရေ ၁၀၀ ပြည့်လျှင် Drop ချခြင်း
    if chat_stats["count"] >= MESSAGE_TARGET and chat_stats["active_drop_code"] is None:
        chat_stats["count"] = 0
        drop_code = generate_drop_code()
        chat_stats["active_drop_code"] = drop_code

        drop_msg = await context.bot.send_message(
            chat_id=chat.id,
            message_thread_id=GENERAL_TOPIC_ID,
            text=(
                f"🎉 <b>စကားဝိုင်း အစောင်ရေ ၁၀၀ ပြည့်ပါပြီ!</b>\n\n"
                f"🎟️ <b>Free Spin Token ကောက်ယူရန်:</b>\n"
                f"ဤမက်ဆေ့ခ်ျအား <b>Reply</b> လုပ်ပြီး အောက်ပါ Code အား အမြန်ဆုံး ရိုက်ထည့်ပါ-\n\n"
                f"👉 👉 👉  <code>{drop_code}</code>  👈 👈 👈\n\n"
                f"<i>(အရင်ဆုံး အတိအကျ Reply ရိုက်ထည့်သူ ၁ ဦးသာ ရရှိပါမည်)</i>"
            ),
            parse_mode=ParseMode.HTML
        )
        chat_stats["drop_message_id"] = drop_msg.message_id

# ==========================================================
# 🛑 Deduct Command
# ==========================================================
async def deduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message

    if user.id != OWNER_ID:
        return

    args = context.args
    target_id = None
    target_name = ""

    # နည်းလမ်း (၁) : စာကို Reply လုပ်ပြီး နှုတ်ခြင်း (/deduct coin 500)
    if msg.reply_to_message:
        target_user = msg.reply_to_message.from_user
        if target_user.is_bot:
            await msg.reply_text("❌ Bot များထံမှ နှုတ်ယူ၍ မရနိုင်ပါ။")
            return
            
        target_id = target_user.id
        target_name = target_user.first_name

        if len(args) < 2:
            await msg.reply_text("⚠️ <b>Reply ဖြင့် နှုတ်နည်း:</b>\n• <code>/deduct coin 500</code>\n• <code>/deduct token 2</code>", parse_mode=ParseMode.HTML)
            return

        item_type = args[0].lower()
        amount_str = args[1]
    else:
        if len(args) < 3:
            await msg.reply_text("⚠️ <b>အသုံးပြုပုံ:</b>\n<code>/deduct &lt;ID/@username&gt; &lt;coin/token&gt; &lt;ပမာဏ&gt;</code>", parse_mode=ParseMode.HTML)
            return

        target_str, item_type, amount_str = args[0], args[1].lower(), args[2]

        if target_str.startswith("@") or not target_str.isdigit():
            target_id = await db.get_user_by_username(target_str)
            if not target_id:
                await msg.reply_text(f"❌ User <b>{target_str}</b> အား ရှာမတွေ့ပါ။", parse_mode=ParseMode.HTML)
                return
            target_name = target_str
        else:
            target_id = int(target_str)
            target_name = f"ID: {target_id}"

    if not amount_str.isdigit() or int(amount_str) <= 0:
        await msg.reply_text("❌ ပမာဏသည် 0 ထက်ကြီးသော ဂဏန်း ဖြစ်ရပါမည်။")
        return
    amount = int(amount_str)

    await db.get_or_create_user(target_id, "", "")

    if item_type in ["coin", "coins", "money", "mmk"]:
        new_balance = await db.deduct_coins(target_id, amount)
        await msg.reply_text(f"✅ <b>{target_name}</b> ထံမှ ဘေလ် <b>{amount} MMK</b> နှုတ်ယူပြီးပါပြီ!\n💰 လက်ကျန်ငွေ: <b>{new_balance} MMK</b>", parse_mode=ParseMode.HTML)
    elif item_type in ["token", "tokens"]:
        new_tokens = await db.deduct_tokens(target_id, amount)
        await msg.reply_text(f"✅ <b>{target_name}</b> ထံမှ Token <b>{amount} ခု</b> နှုတ်ယူပြီးပါပြီ!\n🎟️ လက်ကျန် Token: <b>{new_tokens} ခု</b>", parse_mode=ParseMode.HTML)
    else:
        await msg.reply_text("❌ <code>coin</code> သို့မဟုတ် <code>token</code> ဟု ရွေးချယ်ပေးပါ။", parse_mode=ParseMode.HTML)

# ==========================================================
# 🎰 Spin & Balance Buttons
# ==========================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.get_or_create_user(user.id, user.username, user.first_name)
    
    keyboard = [
        [InlineKeyboardButton("🎰 ကံစမ်းမဲ လှည့်မည်", callback_data="spin_wheel")],
        [InlineKeyboardButton("💰 လက်ကျန်ငွေ / Token စစ်မည်", callback_data="check_balance")]
    ]
    await update.effective_message.reply_text(
        f"မင်္ဂလာပါ {user.first_name}!\nLucky Spin Bot မှ ကြိုဆိုပါသည်ခင်ဗျာ။",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if query.data == "spin_wheel":
        is_owner = (user.id == OWNER_ID)
        prize, res = await db.spin_wheel(user.id, is_owner=is_owner)

        if prize == "MAX_BALANCE_REACHED":
            await query.message.reply_text("⚠️ သင့်လက်ကျန်ငွေ ၃,၀၀၀ ကျပ် ပြည့်နေပါသဖြင့် ဖုန်းဘေလ် ထုတ်ယူပြီးမှသာ ထပ်မံ လှည့်ပေးပါခင်ဗျာ။")
            return
        if prize == "NO_TOKEN":
            await query.message.reply_text("🎟️ သင့်ထံတွင် Spin Token မရှိတော့ပါ။ Group ထဲ သူငယ်ချင်းများ Add ၍ သို့မဟုတ် စကားပြော၍ Token ရယူပါခင်ဗျာ။")
            return

        coins, tokens = res
        if prize == 0:
            await query.message.reply_text(f"😢 စိတ်မကောင်းပါခင်ဗျာ၊ ယခုတစ်ကြိမ် မဲမပေါက်ပါ။\n🎟️ လက်ကျန် Token: {tokens}")
        else:
            await query.message.reply_text(
                f"🎉 ဂုဏ်ယူပါတယ်! <b>{prize} MMK</b> ကံထူးသွားပါသည်!\n"
                f"💰 စုစုပေါင်း ဘေလ်: <b>{coins} MMK</b>\n"
                f"🎟️ လက်ကျန် Token: <b>{tokens}</b>",
                parse_mode=ParseMode.HTML
            )

    elif query.data == "check_balance":
        user_data = await db.get_or_create_user(user.id, user.username, user.first_name)
        coins, tokens = user_data[3], user_data[4]
        await query.message.reply_text(
            f"👤 <b>{user.first_name} ၏ အကောင့်အခြေအနေ</b>\n\n"
            f"💰 စုစုပေါင်း ဘေလ်: <b>{coins} MMK</b>\n"
            f"🎟️ လက်ကျန် Token: <b>{tokens} ခု</b>",
            parse_mode=ParseMode.HTML
        )

# ==========================================================
# 🚀 Entrypoint
# ==========================================================
async def run_bot():
    await db.init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("deduct", deduct_command))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    
    # လူ Add လျှင် ဖမ်းယူမည့် Handler
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_members))
    
    # General Chat စာကြောင်းရေ စစ်ဆေးမည့် Handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_general_chat))

    # Render Port Binding အတွက် Dummy Server ဖွင့်လှစ်ခြင်း
    await start_dummy_server()

    print("🤖 Bot is starting polling...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(run_bot())
    
