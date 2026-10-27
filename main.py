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
user_active_menu = {}
BOT_USERNAME = ""  # startup တွင် ဖြည့်ပါမည်

class RedeemState(StatesGroup):
    waiting_for_operator = State()
    waiting_for_phone = State()

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
                    caption=f"📦 Daily Database Backup ({today})\n\nစနစ်ပျက်စီးခဲ့ပါက ဤဖိုင်ကို ပြန်လည်အသုံးပြုနိုင်ပါသည်။"
                )
        except Exception as e:
            print(f"Backup Error: {e}")

# --- Contact Add Member Detection ---
@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=(LEFT | KICKED | RESTRICTED) >> (MEMBER | ADMINISTRATOR | CREATOR)))
async def on_user_join(event: ChatMemberUpdated):
    if SPIN_GROUP_ID != 0 and event.chat.id != SPIN_GROUP_ID:
        return
    inviter = event.from_user
    new_member = event.new_chat_member.user
    if new_member.is_bot or inviter.is_bot:
        return
    if inviter.id != new_member.id:
        await db.get_or_create_user(inviter.id, inviter.username, inviter.first_name)
        await db.get_or_create_user(new_member.id, new_member.username, new_member.first_name)
        await db.record_invitation(inviter.id, new_member.id)

@dp.message(F.new_chat_members)
async def new_member_message_handler(message: types.Message):
    if SPIN_GROUP_ID != 0 and message.chat.id != SPIN_GROUP_ID:
        return
    inviter = message.from_user
    for new_member in message.new_chat_members:
        if new_member.is_bot or inviter.is_bot:
            continue
        if inviter.id != new_member.id:
            await db.get_or_create_user(inviter.id, inviter.username, inviter.first_name)
            await db.get_or_create_user(new_member.id, new_member.username, new_member.first_name)
            await db.record_invitation(inviter.id, new_member.id)

# --- Spin Menu Keyboard (ဖုန်းဘေလ်လဲရန် Direct URL Button ထည့်သွင်းခြင်း) ---
def get_spin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    redeem_url = f"https://t.me/{BOT_USERNAME}?start=bill"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ကံစမ်းမဲ လှည့်မည်", callback_data=f"spin_{user_id}")],
        [InlineKeyboardButton(text="ဖုန်းဘေလ်တောင်းဆိုထားသောစာရင်း", callback_data=f"pending_{user_id}")],
        [InlineKeyboardButton(text="ဖုန်းဘေလ် လဲလှည်မည်", url=redeem_url)]
    ])

async def unpin_after_one_day(chat_id: int, message_id: int):
    await asyncio.sleep(86400)
    try:
        await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

@dp.message(Command("spin"))
async def open_spin_menu(message: types.Message):
    if SPIN_GROUP_ID != 0 and message.chat.id != SPIN_GROUP_ID:
        return
    if SPIN_TOPIC_ID != 0 and message.message_thread_id != SPIN_TOPIC_ID:
        return
    
    uid = message.from_user.id
    if uid in user_active_menu:
        old_msg_id = user_active_menu[uid]
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=old_msg_id)
        except Exception:
            pass

    user = await db.get_or_create_user(uid, message.from_user.username, message.from_user.first_name)
    kb = get_spin_keyboard(uid)

    menu_text = (
        f"{message.from_user.first_name}\n"
        f"Bill လက်ကျန် - {user[3]} ကျပ်\n"
        f"Spin လှည့်ခွင့် - {user[4]} ကြိမ်"
    )
    sent_msg = await message.reply(menu_text, reply_markup=kb)
    user_active_menu[uid] = sent_msg.message_id

    await asyncio.sleep(120)
    try:
        if user_active_menu.get(uid) == sent_msg.message_id:
            await sent_msg.delete()
            del user_active_menu[uid]
    except Exception:
        pass

# --- မဲလှည့်ခြင်း Callback ---
@dp.callback_query(lambda c: c.data.startswith("spin_"))
async def handle_spin(callback: types.CallbackQuery):
    orig_user_id = int(callback.data.split("_")[1])
    
    if callback.from_user.id != orig_user_id:
        await callback.answer("ဤခလုတ်သည် သင့်အတွက် မဟုတ်ပါ။ /spin ဖြင့် ကိုယ်တိုင် စတင်ပါ!", show_alert=True)
        return

    current_time = time.time()
    last_time = user_cooldowns.get(orig_user_id, 0)
    if current_time - last_time < 7:
        rem = int(7 - (current_time - last_time))
        await callback.answer(f"ခဏစောင့်ပါဦး ({rem} စက္ကန့် ကျန်ပါသည်)", show_alert=True)
        return

    user_cooldowns[orig_user_id] = current_time
    await callback.answer()

    chat_id = callback.message.chat.id
    thread_id = callback.message.message_thread_id

    try:
        await callback.message.delete()
    except Exception:
        pass
    user_active_menu.pop(orig_user_id, None)

    temp_msg = await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=f"🎰 {callback.from_user.first_name} ကံစမ်းမဲ လှည့်နေပါသည်..."
    )

    await asyncio.sleep(2.5)

    try:
        await temp_msg.delete()
    except Exception:
        pass

    result, data = await db.spin_wheel(orig_user_id)

    if result == "NO_TOKEN":
        msg = await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=f"{callback.from_user.first_name}\nSpin လှည့်ခွင့် မရှိတော့ပါ။ Group ထဲသို့ Contact မှ သူငယ်ချင်းများကို Add ပေးပါ။"
        )
        await asyncio.sleep(15)
        try:
            await msg.delete()
        except Exception:
            pass
        return

    if result == "LIMIT_REACHED":
        msg = await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=f"{callback.from_user.first_name}\nယနေ့အတွက် မဲလှည့်ခွင့် ၁၀ ကြိမ် ပြည့်သွားပါပြီ။ မနက်ဖြန်မှ ထပ်မံကြိုးစားပါ။"
        )
        await asyncio.sleep(15)
        try:
            await msg.delete()
        except Exception:
            pass
        return

    # မဲပေါက်စာသား သတ်မှတ်ခြင်း (၁,၀၀၀ ကျပ် ပေါက်ပါက အထူးစာတန်း)
    if result == 1000:
        prize_text = "💥💥 အမြင့်ဆုံးဆုကြီး ၁,၀၀၀ ကျပ် ကံထူးသွားပါပြီ! 💥💥"
    elif result > 0:
        prize_text = f"သင် {result} Bill ကံထူးသွားပါပြီ!"
    else:
        prize_text = "ယခုအကြိမ်တွင် ကံစမ်းမဲ မပေါက်ပါ ။"

    result_display = (
        f"{callback.from_user.first_name}\n"
        f"{prize_text}\n"
        f"Bill လက်ကျန် - {data[0]} ကျပ်\n"
        f"Spin လှည့်ခွင့် - {data[1]} ကြိမ်"
    )

    spin_again_kb = None
    if data[1] > 0:
        spin_again_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 ထပ်မံလှည့်မည်", callback_data=f"spin_{orig_user_id}")]
        ])

    sent_result = await bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=result_display,
        reply_markup=spin_again_kb
    )

    # ၁,၀၀၀ ကျပ် ပေါက်ပါက သီးသန့် အသိပေးကြေငြာချက် ပို့ပြီး ၁ ရက် Pin ထိုးပေးခြင်း
    if result == 1000:
        announce_msg = await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=(
                f"🎉🎉 **[အမြင့်ဆုံးဆု ကံထူးရှင် ထွက်ပေါ်လာပါပြီ!]** 🎉🎉\n\n"
                f"👤 ကံထူးရှင်: {callback.from_user.first_name}\n"
                f"🎁 ဆုပမာဏ: **၁,၀၀၀ ကျပ် ဖုန်းဘေလ်** 💥\n\n"
                f"ဂုဏ်ယူပါသည်! ချက်ချင်း ဖုန်းဘေလ် လဲလှယ်နိုင်ပါပြီ။"
            )
        )
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=announce_msg.message_id, disable_notification=False)
            asyncio.create_task(unpin_after_one_day(chat_id, announce_msg.message_id))
        except Exception as e:
            print(f"Pin Error: {e}")

    # မဲမပေါက်ပါက ၁၅ စက္ကန့်အကြာတွင် ဖျက်မည် (ပေါက်ပါက Group ထဲတွင် အမြဲချန်ထားမည်)
    if result == 0:
        await asyncio.sleep(15)
        try:
            await sent_result.delete()
        except Exception:
            pass

# --- Pending စာရင်း ကြည့်ရှုသည့် Callback ---
@dp.callback_query(lambda c: c.data.startswith("pending_"))
async def handle_pending_btn(callback: types.CallbackQuery):
    orig_user_id = int(callback.data.split("_")[1])
    if callback.from_user.id != orig_user_id:
        await callback.answer("သင့်အတွက် မဟုတ်ပါ!", show_alert=True)
        return

    pending_list = await db.get_user_pending_redemptions(orig_user_id)
    if not pending_list:
        await callback.answer("လွှဲရန်ကျန်ရှိသော ဖုန်းဘေလ်တောင်းဆိုမှု မရှိသေးပါ။", show_alert=True)
        return

    text = "⏳ ဖုန်းဘေလ်တောင်းဆိုထားသောစာရင်း:\n\n"
    for op, ph, dt in pending_list:
        text += f"• {op} ({ph})\n"
    
    await callback.answer(text, show_alert=True)

# --- /bill Command (Private Chat တွင် ဘေလ်တောင်းယူခြင်း) ---
@dp.message(Command("bill"))
@dp.message(Command("start"), F.text.startswith("/start bill"))
async def bill_cmd(message: types.Message, state: FSMContext):
    if message.chat.type != "private":
        await message.reply("ဖုန်းဘေလ် လဲလှယ်ရန် Bot ၏ Private Chat တွင် /bill ဟု ရိုက်ပေးပါ။")
        return

    user = await db.get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if user[3] < 1000:
        await message.reply(f"သင့်တွင် Bill 1,000 မပြည့်သေးပါ (လက်ရှိ: {user[3]} ကျပ်)။")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="MPT", callback_data="op_MPT"), InlineKeyboardButton(text="ATOM", callback_data="op_ATOM")],
        [InlineKeyboardButton(text="Ooredoo", callback_data="op_Ooredoo"), InlineKeyboardButton(text="Mytel", callback_data="op_Mytel")]
    ])
    await message.reply("သင့်ဖုန်း အော်ပရေတာကို ရွေးချယ်ပါ:", reply_markup=kb)
    await state.set_state(RedeemState.waiting_for_operator)

@dp.callback_query(RedeemState.waiting_for_operator, lambda c: c.data.startswith("op_"))
async def select_operator(callback: types.CallbackQuery, state: FSMContext):
    op = callback.data.split("_")[1]
    await state.update_data(operator=op)
    await callback.message.edit_text(f"အော်ပရေတာ: {op}\n\nဘေလ်ထည့်လိုသော ဖုန်းနံပါတ် (ဥပမာ- 09xxxxxxxxx) ကို ရိုက်ပို့ပေးပါ:")
    await state.set_state(RedeemState.waiting_for_phone)

@dp.message(RedeemState.waiting_for_phone)
async def submit_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    data = await state.get_data()
    operator = data.get("operator")

    await db.add_coins(message.from_user.id, -1000)
    redemption_id = await db.create_redemption(message.from_user.id, operator, phone)

    if ADMIN_LOG_GROUP_ID != 0:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ ဖြည့်ပေးပြီးပြီ", callback_data=f"approve_{redemption_id}")]
        ])
        admin_log = (
            f"🔔 [ဖုန်းဘေလ် လဲလှယ်မှု အော်ဒါအသစ်]\n\n"
            f"အကောင့်: {message.from_user.first_name} (`{message.from_user.id}`)\n"
            f"အော်ပရေတာ: {operator}\n"
            f"ဖုန်းနံပါတ်: `{phone}`\n"
            f"ကုန်ကျစရိတ်: 1,000 Bill (ဖြတ်ပြီး)\n"
            f"အခြေအနေ: ⏳ Pending\n"
            f"အချိန်: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await bot.send_message(chat_id=ADMIN_LOG_GROUP_ID, text=admin_log, reply_markup=admin_kb)

    await message.reply("တောင်းဆိုမှု အောင်မြင်ပါသည်။ Admin မှ စစ်ဆေး၍ ဖုန်းဘေလ် ဖြည့်ပေးပါလိမ့်မည်။")
    await state.clear()

# --- Owner Approve Button ---
@dp.callback_query(lambda c: c.data.startswith("approve_"))
async def handle_approve_btn(callback: types.CallbackQuery):
    if int(callback.from_user.id) != OWNER_ID:
        await callback.answer("Owner သာ နှိပ်ခွင့်ရှိပါသည်!", show_alert=True)
        return

    redemption_id = int(callback.data.split("_")[1])
    res = await db.approve_redemption(redemption_id)
    
    if not res:
        await callback.answer("ပြီးစီးပြီးဖြစ်သည် သို့မဟုတ် မှတ်တမ်းမရှိပါ!", show_alert=True)
        return

    uid, operator, phone, _ = res
    user_info = await db.get_or_create_user(uid, "", "")
    user_name = user_info[2] or "User"

    updated_text = (
        f"✅ [ဘေလ်ဖြည့်ပြီးပါပြီ]\n\n"
        f"အကောင့်: {user_name} (`{uid}`)\n"
        f"အော်ပရေတာ: {operator}\n"
        f"ဖုန်းနံပါတ်: `{phone}`\n"
        f"ပမာဏ: ၁,၀၀၀ ကျပ်\n"
        f"အခြေအနေ: ဖြည့်သွင်းပြီးစီး"
    )
    await callback.message.edit_text(updated_text)
    await callback.answer("ဘေလ်ဖြည့်ပြီးကြောင်း မှတ်တမ်းတင်ပြီးပါပြီ။")

    if SPIN_GROUP_ID != 0 and SPIN_TOPIC_ID != 0:
        announce_text = f"🎉 ဂုဏ်ယူပါတယ်! {user_name} သို့ ၁,၀၀၀ ကျပ် ဖုန်းဘေလ် ({operator}) ထည့်ပေးပြီးပါပြီ!"
        try:
            await bot.send_message(
                chat_id=SPIN_GROUP_ID,
                message_thread_id=SPIN_TOPIC_ID,
                text=announce_text
            )
        except Exception as e:
            print(f"Announcement Error: {e}")

    try:
        await bot.send_message(
            chat_id=uid,
            text=f"✅ သင့်ထံသို့ ၁,၀၀၀ ကျပ် ဖုန်းဘေလ် ({operator}) ထည့်ပေးပြီးပါပြီ။ ကျေးဇူးတင်ပါသည်!"
        )
    except Exception:
        pass

# ==========================================================
# 👑 Owner Commands (/top10, /detail, /backup, /restore, /givebill, /givetoken)
# ==========================================================

# 🏆 /top10 — ထိပ်သီး ၁၀ ဦး စာရင်း
@dp.message(Command("top10"))
async def cmd_top10(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return
    
    rows = await db.get_top_10()
    if not rows:
        await message.reply("မှတ်တမ်းမရှိသေးပါ။")
        return

    text = "🏆 **Top 10 အများဆုံး စာရင်း**\n\n"
    for i, (fn, un, c, t) in enumerate(rows, 1):
        u_str = f"@{un}" if un else fn
        text += f"{i}. {u_str} — **{c} ကျပ်** (Spin: {t} ကြိမ်)\n"
    await message.reply(text)

# 🔍 /detail — User အချက်အလက် စစ်ဆေးခြင်း
@dp.message(Command("detail"))
async def cmd_detail(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return

    target_id = None
    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
    else:
        args = message.text.split()
        if len(args) > 1:
            param = args[1]
            if param.isdigit():
                target_id = int(param)
            elif param.startswith("@"):
                target_id = await db.get_user_by_username(param)

    if not target_id:
        await message.reply("💡 **အသုံးပြုနည်း:**\n၁။ User စာကို Reply လုပ်၍ `/detail`\n၂။ `/detail @username`\n၃။ `/detail 123456789`")
        return

    data = await db.get_user_detail(target_id)
    if not data:
        await message.reply("❌ စနစ်ထဲတွင် ဤ User မှတ်တမ်း မတွေ့ပါ။")
        return

    u, inv_count = data
    await message.reply(
        f"🔍 **အကောင့် စစ်ဆေးချက်:**\n\n"
        f"👤 အမည်: {u[2]} (@{u[1] or 'none'})\n"
        f"🆔 User ID: `{u[0]}`\n"
        f"💰 Bill လက်ကျန်: **{u[3]} ကျပ်**\n"
        f"🎟️ Spin လှည့်ခွင့်: **{u[4]} ကြိမ်**\n"
        f"👥 Add ခဲ့သူ: **{inv_count} ယောက်**\n"
        f"🎰 ယနေ့မဲလှည့်ပြီး: **{u[6]}/10 ကြိမ်**"
    )

# 📦 /backup
@dp.message(Command("backup"))
async def cmd_manual_backup(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return
    if not os.path.exists(db.DB_NAME):
        await message.reply("❌ Database ဖိုင် မရှိသေးပါ။")
        return
    if ADMIN_LOG_GROUP_ID == 0:
        await message.reply("❌ ADMIN_LOG_GROUP_ID သတ်မှတ်မထားပါ။")
        return

    try:
        now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_file = FSInputFile(db.DB_NAME, filename=f"backup_manual_{now_str}.db")
        await bot.send_document(
            chat_id=ADMIN_LOG_GROUP_ID,
            document=backup_file,
            caption=f"📦 **Update Database Backup**\n\n📅 အချိန်: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n👤 ထုတ်ယူသူ: Owner"
        )
        await message.reply("✅ Update Database ဖိုင်ကို Admin Group ထံသို့ ပို့ပေးလိုက်ပါပြီ။")
    except Exception as e:
        await message.reply(f"❌ Backup Error: {e}")

# 🔄 /restore
@dp.message(Command("restore"))
async def cmd_restore(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return
    if not message.reply_to_message or not message.reply_to_message.document:
        await message.reply("💡 Restore လုပ်ရန် Admin Group သို့ ရောက်နေသော `.db` ဖိုင်ကို Reply ထောက်၍ `/restore` ဟု ရိုက်ပေးပါ။")
        return

    doc = message.reply_to_message.document
    if not doc.file_name.endswith(".db"):
        await message.reply("❌ `.db` ဖိုင်အမျိုးအစားသာ ဖြစ်ရပါမည်။")
        return

    status_msg = await message.reply("⏳ Database ဖိုင်ကို ပြန်လည် ထည့်သွင်းနေပါသည်...")
    try:
        file_info = await bot.get_file(doc.file_id)
        await bot.download_file(file_info.file_path, destination=db.DB_NAME)
        await status_msg.edit_text("✅ **Database Restore အောင်မြင်ပါပြီ!**\n\nမှတ်တမ်းဟောင်းများ အားလုံး အောင်မြင်စွာ ပြန်လည် အစားထိုးပြီးပါပြီ။")
    except Exception as e:
        await status_msg.edit_text(f"❌ Restore Error: {e}")

# /givebill
@dp.message(Command("givebill"))
async def cmd_givebill(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return
    target_id = None
    target_name = "User"
    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.first_name

    args = message.text.split()
    amount = 0

    if target_id and len(args) > 1 and args[1].isdigit():
        amount = int(args[1])
    elif len(args) > 2 and args[2].isdigit():
        amount = int(args[2])
        param = args[1]
        if param.isdigit():
            target_id = int(param)
        elif param.startswith("@"):
            target_id = await db.get_user_by_username(param)

    if not target_id or amount <= 0:
        await message.reply("အသုံးပြုနည်း:\n၁။ Reply ထောက်ပြီး /givebill 50\n၂။ /givebill @username 50\n၃။ /givebill [ID] 50")
        return

    new_c = await db.add_coins(target_id, amount)
    await message.reply(f"🎁 {target_name} (`{target_id}`) သို့ +{amount} Bill ထည့်ပေးလိုက်ပါပြီ! (လက်ရှိ: {new_c} ကျပ်)")

# /givetoken
@dp.message(Command("givetoken"))
async def cmd_givetoken(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return
    target_id = None
    target_name = "User"
    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.first_name

    args = message.text.split()
    amount = 0
    if target_id and len(args) > 1 and args[1].isdigit():
        amount = int(args[1])
    elif len(args) > 2 and args[2].isdigit():
        amount = int(args[2])
        param = args[1]
        if param.isdigit():
            target_id = int(param)
        elif param.startswith("@"):
            target_id = await db.get_user_by_username(param)

    if not target_id or amount <= 0:
        await message.reply("အသုံးပြုနည်း:\n၁။ Reply ထောက်ပြီး /givetoken 1\n၂။ /givetoken @username 1\n၃။ /givetoken [ID] 1")
        return

    new_t = await db.add_tokens(target_id, amount)
    await message.reply(f"🎟️ {target_name} (`{target_id}`) သို့ +{amount} Spin လှည့်ခွင့် ထည့်ပေးလိုက်ပါပြီ! (လက်ရှိ: {new_t} ကြိမ်)")

# ==========================================================
# 🚀 Startup & Polling
# ==========================================================
async def main():
    global BOT_USERNAME
    await db.init_db()
    bot_info = await bot.get_me()
    BOT_USERNAME = bot_info.username
    asyncio.create_task(start_web_server())
    asyncio.create_task(auto_backup_task())
    print(f"Bot @{BOT_USERNAME} is successfully running...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member"])

if __name__ == "__main__":
    asyncio.run(main())
