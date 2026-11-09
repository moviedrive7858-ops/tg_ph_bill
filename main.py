import os
import random
import string
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters
)
import database as db

# Bot Token နှင့် Owner ID
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # သင့် Telegram User ID ထည့်ပါ

# Topic ID သတ်မှတ်ချက်များ (General က Main Chat ဖြစ်ပါက None ထားပါ)
GENERAL_TOPIC_ID = None
SPIN_TOPIC_ID = None

# Chat Activity Drop Settings (စာအစောင်ရေ ၁၀၀ ပြည့်လျှင် ၁ ခုကျမည်)
MESSAGE_TARGET = 100

chat_stats = {
    "count": 0,
    "last_user_id": None,
    "active_drop_code": None,
    "drop_message_id": None
}

def generate_drop_code() -> str:
    """English စာလုံး ၃ လုံး နှင့် နံပါတ် ၂ လုံး ရောနှောထုတ်ပေးခြင်း (ဥပမာ- K7B2M)"""
    letters = random.choices(string.ascii_uppercase, k=3)
    digits = random.choices(string.digits, k=2)
    combined = letters + digits
    random.shuffle(combined)
    return "".join(combined)

def is_valid_message(text: str) -> bool:
    clean_text = text.strip()
    # စာလုံးရေ ၅ လုံးထက် မနည်းရ
    if len(clean_text) < 5:
        return False
    # စကားလုံး ၂ လုံးထက် မနည်းရ
    if len(clean_text.split()) < 2:
        return False
    # ဂဏန်းသီးသန့် မဖြစ်ရ
    if clean_text.isdigit():
        return False
    # သင်္ကေတ / အစက် သီးသန့် မဖြစ်ရ
    if re.fullmatch(r"[\W_]+", clean_text):
        return False
    return True

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

async def handle_chat_member_updated(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Member ၂ ယောက် Add လျှင် Token ၁ ခု ပေးခြင်း"""
    result = update.chat_member
    if not result:
        return

    if result.old_chat_member.status in ["left", "kicked"] and result.new_chat_member.status == "member":
        inviter = result.from_user
        new_member = result.new_chat_member.user

        if inviter and not inviter.is_bot and not new_member.is_bot and inviter.id != new_member.id:
            await db.get_or_create_user(inviter.id, inviter.username, inviter.first_name)
            awarded = await db.record_invitation(inviter.id, new_member.id)
            if awarded:
                try:
                    await context.bot.send_message(
                        chat_id=inviter.id,
                        text="🎉 သင့်ဖိတ်ခေါ်မှုကြောင့် Member ၂ ယောက် ပြည့်မြောက်ပြီး 🎟️ <b>Spin Token ၁ ခု</b> ရရှိပါသည်!",
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass

async def handle_general_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """General ခန်း စကားဝိုင်းအား စစ်ဆေးပြီး Drop ချပေးခြင်း နှင့် ကောက်ယူမှု စစ်ဆေးခြင်း"""
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

    # ------------------------------------------------------
    # 🎯 အပိုင်း (က) : Drop Code ကို Reply ဖြင့် အရင်ဆုံး လုယူခြင်း
    # ------------------------------------------------------
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

                # 📢 General ခန်းထဲသို့ Winner အား Mention ခေါ်၍ ကြေငြာပေးခြင်း
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

    # ------------------------------------------------------
    # 📊 အပိုင်း (ခ) : စာကြောင်းရေ စစ်ဆေးခြင်း
    # ------------------------------------------------------
    if not is_valid_message(text):
        return

    # လူတစ်ယောက်တည်း ဆက်တိုက်ပို့ခြင်းကို မရေတွက်ပါ
    if chat_stats["last_user_id"] == user.id:
        return

    chat_stats["count"] += 1
    chat_stats["last_user_id"] = user.id

    # ------------------------------------------------------
    # 🎁 အပိုင်း (ဂ) : စာကြောင်းရေ ၁၀၀ ပြည့်ပါက Drop ချပေးခြင်း
    # ------------------------------------------------------
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
# 🛑 Admin Only: Token နှင့် Coin ပြန်လည်နှုတ်ယူခြင်း Command
# ==========================================================
async def deduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message

    if user.id != OWNER_ID:
        return

    args = context.args
    target_id = None
    target_name = ""

    # နည်းလမ်း (၁) : စာကို Reply လုပ်ပြီး နှုတ်ခြင်း
    if msg.reply_to_message:
        target_user = msg.reply_to_message.from_user
        if target_user.is_bot:
            await msg.reply_text("❌ Bot များထံမှ နှုတ်ယူ၍ မရနိုင်ပါ။")
            return
            
        target_id = target_user.id
        target_name = target_user.first_name

        if len(args) < 2:
            await msg.reply_text(
                "⚠️ <b>Reply ဖြင့် နှုတ်ယူနည်း:</b>\n"
                "• <code>/deduct coin 500</code>\n"
                "• <code>/deduct token 2</code>",
                parse_mode=ParseMode.HTML
            )
            return

        item_type = args[0].lower()
        amount_str = args[1]

    # နည်းလမ်း (၂) : ID သို့မဟုတ် Username ရိုက်ထည့်ပြီး နှုတ်ခြင်း
    else:
        if len(args) < 3:
            await msg.reply_text(
                "⚠️ <b>အသုံးပြုပုံ မမှန်ကန်ပါ!</b>\n\n"
                "👉 <b>Reply ဖြင့် သုံးလိုပါက:</b>\n"
                "User ၏ စာကို Reply လုပ်၍ <code>/deduct coin 500</code>\n\n"
                "👉 <b>ID/Username ဖြင့် သုံးလိုပါက:</b>\n"
                "<code>/deduct &lt;ID/@username&gt; &lt;coin/token&gt; &lt;ပမာဏ&gt;</code>",
                parse_mode=ParseMode.HTML
            )
            return

        target_str, item_type, amount_str = args[0], args[1].lower(), args[2]

        if target_str.startswith("@") or not target_str.isdigit():
            target_id = await db.get_user_by_username(target_str)
            if not target_id:
                await msg.reply_text(f"❌ User <b>{target_str}</b> အား Database တွင် ရှာမတွေ့ပါ။", parse_mode=ParseMode.HTML)
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
        await msg.reply_text(
            f"✅ <b>{target_name}</b> ထံမှ ဘေလ် <b>{amount} MMK</b> အောင်မြင်စွာ နှုတ်ယူပြီးပါပြီ!\n"
            f"💰 လက်ကျန်ငွေ: <b>{new_balance} MMK</b>",
            parse_mode=ParseMode.HTML
        )
    elif item_type in ["token", "tokens"]:
        new_tokens = await db.deduct_tokens(target_id, amount)
        await msg.reply_text(
            f"✅ <b>{target_name}</b> ထံမှ Token <b>{amount} ခု</b> အောင်မြင်စွာ နှုတ်ယူပြီးပါပြီ!\n"
            f"🎟️ လက်ကျန် Token: <b>{new_tokens} ခု</b>",
            parse_mode=ParseMode.HTML
        )
    else:
        await msg.reply_text("❌ <code>coin</code> သို့မဟုတ် <code>token</code> ဟု ရွေးချယ်ပေးပါ။", parse_mode=ParseMode.HTML)

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

def main():
    import asyncio
    asyncio.run(db.init_db())

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("deduct", deduct_command))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(ChatMemberHandler(handle_chat_member_updated, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_general_chat))

    print("🤖 Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
    
