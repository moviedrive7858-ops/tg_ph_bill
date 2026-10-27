import os
import time
import asyncio
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, ChatMemberUpdatedFilter, KICKED, LEFT, RESTRICTED, MEMBER, ADMINISTRATOR, CREATOR
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, ChatMemberUpdated
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import database as db

# ==========================================================
# ⚙️ Render Environment Variables မှ လျှို့ဝှက်တန်ဖိုးများ ဆွဲယူခြင်း
# ==========================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
SPIN_GROUP_ID = int(os.environ.get("SPIN_GROUP_ID", 0))      # Main Group ID
SPIN_TOPIC_ID = int(os.environ.get("SPIN_TOPIC_ID", 0))      # Topic ID
ADMIN_LOG_GROUP_ID = int(os.environ.get("ADMIN_LOG_GROUP_ID", 0))  # Private Admin Group ID

if not BOT_TOKEN:
    raise ValueError("ERROR: Render Environment Variables တွင် BOT_TOKEN မသတ်မှတ်ရသေးပါ။")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
user_cooldowns = {}

class RedeemState(StatesGroup):
    waiting_for_operator = State()
    waiting_for_phone = State()

# ==========================================================
# 🌐 UptimeRobot အတွက် Health Check Web Server
# ==========================================================
async def health_check(request):
    return web.Response(text="Bot is running active!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ==========================================================
# 📦 Daily Database Auto-Backup (သီးသန့် Admin Group သို့ ပို့မည်)
# ==========================================================
async def auto_backup_task():
    while True:
        await asyncio.sleep(86400)
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            if os.path.exists(db.DB_NAME) and ADMIN_LOG_GROUP_ID != 0:
                backup_file = FSInputFile(db.DB_NAME, filename=f"backup_{today}.db")
                await bot.send_document(
                    chat_id=ADMIN_LOG_GROUP_ID,
                    document=backup_file,
                    caption=f"📦 **Daily Database Backup** ({today})\n\nစနစ်ပျက်စီးခဲ့ပါက ဤဖိုင်ကို ပြန်လည်အသုံးပြုနိုင်ပါသည်။"
                )
        except Exception as e:
            print(f"Backup Error: {e}")

# ==========================================================
# 👥 Contact မှတစ်ဆင့် Add Member လုပ်ခြင်းကို ထောက်လှမ်းသည့်စနစ်
# ==========================================================
# နည်းလမ်း (၁): Telegram Event အသစ် (ChatMemberUpdated) ဖြင့် ဖမ်းယူခြင်း
@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=(LEFT | KICKED | RESTRICTED) >> (MEMBER | ADMINISTRATOR | CREATOR)))
async def on_user_join(event: ChatMemberUpdated):
    # သတ်မှတ်ထားသော Main Group ဖြစ်ရမည်
    if SPIN_GROUP_ID != 0 and event.chat.id != SPIN_GROUP_ID:
        return

    inviter = event.from_user
    new_member = event.new_chat_member.user

    # Bot ကိုယ်တိုင် ဖြစ်နေပါက သို့မဟုတ် Spam Bot ဖြစ်ပါက ကျော်မည်
    if new_member.is_bot or inviter.is_bot:
        return

    # ကိုယ့်ဖာသာ Link နှိပ်ပြီး ဝင်လာသူမဟုတ်ဘဲ သူတစ်ပါးက Contact မှ Add ပေးမှသာ Token ပေးမည်
    if inviter.id != new_member.id:
        await db.get_or_create_user(inviter.id, inviter.username, inviter.first_name)
        await db.get_or_create_user(new_member.id, new_member.username, new_member.first_name)
        await db.record_invitation(inviter.id, new_member.id)

# နည်းလမ်း (၂): Service Message (F.new_chat_members) ဖြင့် ထပ်ဆင့်ဖမ်းယူခြင်း
@dp.message(F.new_chat_members)
async def new_member_message_handler(message: types.Message):
    if SPIN_GROUP_ID != 0 and message.chat.id != SPIN_GROUP_ID:
        return

    inviter = message.from_user
    for new_member in message.new_chat_members:
        if new_member.is_bot or inviter.is_bot:
            continue
        # ကိုယ်တိုင် Link ဖြင့် ဝင်လာသူကို Token မပေးပါ
        if inviter.id != new_member.id:
            await db.get_or_create_user(inviter.id, inviter.username, inviter.first_name)
            await db.get_or_create_user(new_member.id, new_member.username, new_member.first_name)
            await db.record_invitation(inviter.id, new_member.id)

# ==========================================================
# 🎮 Lucky Spin Menu (Link ခလုတ် မပါတော့ပါ)
# ==========================================================
def get_spin_keyboard(user_id: int, name: str) -> InlineKeyboardMarkup:
    display_name = name[:15] + "..." if len(name) > 15 else name
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎰 {display_name} ၏ ကံစမ်းမဲ လှည့်မည်", callback_data=f"spin_{user_id}")],
        [InlineKeyboardButton(text="🎁 ဖုန်းဘေလ် လဲမည် (1,000 Coins)", callback_data=f"redeem_{user_id}")]
    ])

# Lucky Spin Topic ထဲတွင် Menu ဖွင့်ခြင်း
@dp.message(Command("spin"))
async def open_spin_menu(message: types.Message):
    if SPIN_GROUP_ID != 0 and message.chat.id != SPIN_GROUP_ID:
        return
    if SPIN_TOPIC_ID != 0 and message.message_thread_id != SPIN_TOPIC_ID:
        return
    
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    kb = get_spin_keyboard(message.from_user.id, message.from_user.first_name)
    sent_msg = await message.reply(
        f"🎰 **Lucky Spin Menu**\n"
        f"👤 ပိုင်ရှင်: {message.from_user.first_name}\n"
        f"🪙 Coins: **{user[3]}** | 🎟️ Tokens: **{user[4]}**\n\n"
        f"*(၂ မိနစ်အတွင်း ဆက်မလုပ်ပါက အလိုအလျောက် ပျက်ပြယ်ပါမည်)*",
        reply_markup=kb
    )
    
    await asyncio.sleep(120)
    try:
        await sent_msg.delete()
    except Exception:
        pass

# မဲလှည့်ခြင်း Callback
@dp.callback_query(lambda c: c.data.startswith("spin_"))
async def handle_spin(callback: types.CallbackQuery):
    orig_user_id = int(callback.data.split("_")[1])
    
    if callback.from_user.id != orig_user_id:
        await callback.answer("⚠️ ဤခလုတ်သည် သင့်အတွက် မဟုတ်ပါ။ /spin ဖြင့် ကိုယ်တိုင် စတင်ပါ!", show_alert=True)
        return

    current_time = time.time()
    last_time = user_cooldowns.get(orig_user_id, 0)
    if current_time - last_time < 5:
        rem = int(5 - (current_time - last_time))
        await callback.answer(f"⏳ ခဏစောင့်ပါဦး ({rem} စက္ကန့် ကျန်ပါသည်)", show_alert=True)
        return

    user_cooldowns[orig_user_id] = current_time
    await callback.answer()

    result, data = await db.spin_wheel(orig_user_id)
    if result == "NO_TOKEN":
        await callback.message.edit_text("❌ သင့်တွင် Token မရှိတော့ပါ။ Group ထဲသို့ Contact မှ သူငယ်ချင်းများကို Add ပေးပါ!")
        return
    if result == "LIMIT_REACHED":
        await callback.message.edit_text("⚠️ ယနေ့အတွက် မဲလှည့်ခွင့် ၁၀ ကြိမ် ပြည့်သွားပါပြီ။ မနက်ဖြန်မှ ထပ်မံကြိုးစားပါ!")
        return

    prize_text = f"🎉 သင် **{result} Coins** ကံထူးသွားပါပြီ! 🪙" if result > 0 else "😢 စိတ်မကောင်းပါဘူး! ယခုအလှည့်တွင် Coin မဲမပေါက်ပါ။"
    await callback.message.edit_text(
        f"🎰 @{callback.from_user.username or callback.from_user.first_name} မဲလှည့်လိုက်ပါပြီ!\n\n"
        f"{prize_text}\n"
        f"💰 လက်ကျန် Coins: **{data[0]}** | 🎟️ Tokens: **{data[1]}**\n\n"
        f"*(ဤစာသည် ၁၅ စက္ကန့်အကြာတွင် အလိုအလျောက် ပျက်သွားပါမည်)*"
    )

    await asyncio.sleep(15)
    try:
        await callback.message.delete()
    except Exception:
        pass

# ဖုန်းဘေလ် လဲလှယ်တောင်းဆိုခြင်း ခလုတ်
@dp.callback_query(lambda c: c.data.startswith("redeem_"))
async def handle_redeem_btn(callback: types.CallbackQuery):
    orig_user_id = int(callback.data.split("_")[1])
    if callback.from_user.id != orig_user_id:
        await callback.answer("⚠️ သင့်အတွက် မဟုတ်ပါ!", show_alert=True)
        return

    user = await db.get_or_create_user(orig_user_id, "", "")
    if user[3] < 1000:
        await callback.answer(f"❌ အနည်းဆုံး 1,000 Coins ရှိမှသာ ဖုန်းဘေလ် လဲနိုင်ပါသည်! (လက်ရှိ: {user[3]} Coins)", show_alert=True)
        return

    me = await bot.get_me()
    await callback.answer(url=f"https://t.me/{me.username}?start=bill")

# ==========================================================
# 📱 /bill Command (Private Chat တွင် ဖုန်းဘေလ် တောင်းယူခြင်း)
# ==========================================================
@dp.message(Command("bill"))
@dp.message(Command("start"), F.text == "/start bill")
async def bill_cmd(message: types.Message, state: FSMContext):
    if message.chat.type != "private":
        await message.reply("💡 ဖုန်းဘေလ် လဲလှယ်ရန် Bot ၏ Private Chat တွင် `/bill` ဟု ရိုက်ပေးပါ။")
        return

    user = await db.get_or_create_user(message.from_user.id, "", "")
    if user[3] < 1000:
        await message.reply(f"❌ သင့်တွင် Coins 1,000 မပြည့်သေးပါ (လက်ရှိ: {user[3]} Coins)။")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="MPT", callback_data="op_MPT"), InlineKeyboardButton(text="ATOM", callback_data="op_ATOM")],
        [InlineKeyboardButton(text="Ooredoo", callback_data="op_Ooredoo"), InlineKeyboardButton(text="Mytel", callback_data="op_Mytel")]
    ])
    await message.reply("📱 သင့်ဖုန်း အော်ပရေတာကို ရွေးချယ်ပါ:", reply_markup=kb)
    await state.set_state(RedeemState.waiting_for_operator)

@dp.callback_query(RedeemState.waiting_for_operator, lambda c: c.data.startswith("op_"))
async def select_operator(callback: types.CallbackQuery, state: FSMContext):
    op = callback.data.split("_")[1]
    await state.update_data(operator=op)
    await callback.message.edit_text(f"📱 အော်ပရေတာ: **{op}**\n\nဘေလ်ထည့်လိုသော ဖုန်းနံပါတ် (ဥပမာ- 09xxxxxxxxx) ကို ရိုက်ပို့ပေးပါ:")
    await state.set_state(RedeemState.waiting_for_phone)

@dp.message(RedeemState.waiting_for_phone)
async def submit_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    data = await state.get_data()
    operator = data.get("operator")

    await db.add_coins(message.from_user.id, -1000)

    if ADMIN_LOG_GROUP_ID != 0:
        admin_log = (
            f"🔔 **[ဖုန်းဘေလ် လဲလှယ်မှု အော်ဒါအသစ်]**\n\n"
            f"👤 အကောင့်: @{message.from_user.username or message.from_user.first_name} (`{message.from_user.id}`)\n"
            f"📱 အော်ပရေတာ: **{operator}**\n"
            f"📞 ဖုန်းနံပါတ်: `{phone}`\n"
            f"🪙 ကုန်ကျစရိတ်: 1,000 Coins (ဖြတ်ပြီး)\n"
            f"📅 အချိန်: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await bot.send_message(chat_id=ADMIN_LOG_GROUP_ID, text=admin_log)

    await message.reply("✅ တောင်းဆိုမှု အောင်မြင်ပါသည်။ Admin မှ စစ်ဆေး၍ ဖုန်းဘေလ် ဖြည့်ပေးပါလိမ့်မည်။")
    await state.clear()

# ==========================================================
# 👑 Owner သီးသန့် Admin Commands (/givebill, /detail, etc.)
# ==========================================================

# /givebill <ပမာဏ> (Coin မုန့်ဖိုးပေးခြင်း)
@dp.message(Command("givebill"))
async def cmd_givebill(message: types.Message):
    # Owner စစ်ဆေးမှု (Integer အချင်းချင်း တိုက်စစ်သည်)
    if int(message.from_user.id) != OWNER_ID:
        return

    # Reply လုပ်ထားသော User ရှာဖွေခြင်း
    target_user = None
    if message.reply_to_message:
        target_user = message.reply_to_message.from_user

    # အကယ်၍ Reply မလုပ်ဘဲ /givebill @username 50 သို့မဟုတ် /givebill [ID] 50 ဟု ရိုက်ပါက
    args = message.text.split()
    amount = 0

    if target_user and len(args) > 1 and args[1].isdigit():
        amount = int(args[1])
    elif len(args) > 2 and args[2].isdigit():
        amount = int(args[2])
        param = args[1]
        async with aiosqlite.connect(db.DB_NAME) as conn:
            if param.isdigit():
                cursor = await conn.execute("SELECT user_id, first_name FROM users WHERE user_id = ?", (int(param),))
            elif param.startswith("@"):
                cursor = await conn.execute("SELECT user_id, first_name FROM users WHERE username = ?", (param[1:],))
            row = await cursor.fetchone()
            if row:
                class DummyUser:
                    id = row[0]
                    first_name = row[1]
                target_user = DummyUser()

    if not target_user or amount <= 0:
        await message.reply("💡 **အသုံးပြုနည်း:**\n၁။ User စာကို Reply ပြန်ပြီး `/givebill 50`\n၂။ `/givebill @username 50`\n၃။ `/givebill [ID] 50`")
        return

    new_c = await db.add_coins(target_user.id, amount)
    await message.reply(f"🎁 **{target_user.first_name}** အား **+{amount} Coins** မုန့်ဖိုးပေးလိုက်ပါပြီ! (လက်ရှိစုစုပေါင်း: {new_c} Coins)")

# /givetoken <ပမာဏ> (မဲလှည့်ခွင့် Token ပေးခြင်း)
@dp.message(Command("givetoken"))
async def cmd_givetoken(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return

    target_user = message.reply_to_message.from_user if message.reply_to_message else None
    args = message.text.split()
    if target_user and len(args) > 1 and args[1].isdigit():
        amount = int(args[1])
        new_t = await db.add_tokens(target_user.id, amount)
        await message.reply(f"🎟️ **{target_user.first_name}** အား **+{amount} Token** ပေးလိုက်ပါပြီ! (လက်ရှိ: {new_t} ခု)")
    else:
        await message.reply("💡 User စာကို Reply လုပ်၍ `/givetoken 1` ဟု ရိုက်ပေးပါ။")

# /top15 (ထိပ်သီး ၁၅ ဦး စစ်ဆေးခြင်း)
@dp.message(Command("top15"))
async def cmd_top15(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return
    async with aiosqlite.connect(db.DB_NAME) as conn:
        cursor = await conn.execute("SELECT first_name, username, coins, tokens FROM users ORDER BY coins DESC LIMIT 15")
        rows = await cursor.fetchall()
    if not rows:
        await message.reply("စနစ်ထဲတွင် မှတ်တမ်းမရှိသေးပါ။")
        return
    text = "🏆 **Top 15 ဒင်္ဂါးအများဆုံး စာရင်း**\n\n"
    for i, (fn, un, c, t) in enumerate(rows, 1):
        u_str = f"@{un}" if un else fn
        text += f"{i}. {u_str} — **{c} Coins** 🪙 (Token: {t})\n"
    await message.reply(text)

# /detail (User အချက်အလက် စစ်ဆေးခြင်း)
@dp.message(Command("detail"))
async def cmd_detail(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return
    target_id = message.reply_to_message.from_user.id if message.reply_to_message else None
    if not target_id:
        args = message.text.split()
        if len(args) > 1 and args[1].isdigit():
            target_id = int(args[1])
    if not target_id:
        await message.reply("💡 User ၏ စာကို Reply လုပ်၍ `/detail` ဟု ရိုက်ပါ သို့မဟုတ် `/detail [ID]` ဟု ရိုက်ပါ။")
        return

    async with aiosqlite.connect(db.DB_NAME) as conn:
        cursor = await conn.execute("SELECT * FROM users WHERE user_id = ?", (target_id,))
        u = await cursor.fetchone()
        c_cursor = await conn.execute("SELECT COUNT(*) FROM invited_members WHERE inviter_id = ?", (target_id,))
        inv_count = (await c_cursor.fetchone())[0]

    if not u:
        await message.reply("❌ မှတ်တမ်း မတွေ့ပါ။")
        return
    await message.reply(
        f"🔍 **အကောင့် စစ်ဆေးချက်:**\n"
        f"👤 အမည်: {u[2]} (ID: `{u[0]}`)\n"
        f"🪙 Coins: **{u[3]}** | 🎟️ Tokens: **{u[4]}**\n"
        f"👥 ခေါ်ထားသူ: **{inv_count} ယောက်**\n"
        f"🎰 ယနေ့မဲလှည့်ပြီး: **{u[6]}/10 ကြိမ်**"
    )

# ==========================================================
# 🚀 Run Application (allowed_updates တွင် chat_member ကို တိုက်ရိုက်ခွင့်ပြုခြင်း)
# ==========================================================
async def main():
    await db.init_db()
    asyncio.create_task(start_web_server())
    asyncio.create_task(auto_backup_task())
    print("Bot is successfully running...")
    
    # ⚠️ အရေးကြီးသည်: allowed_updates တွင် chat_member ကို ထည့်ပေးမှသာ Add Member event ကို အပြည့်အဝ ဖမ်းယူနိုင်မည်
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member"])

if __name__ == "__main__":
    asyncio.run(main())
