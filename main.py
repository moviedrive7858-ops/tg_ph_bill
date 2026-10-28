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
SPIN_GROUP_ID = int(os.environ.get("SPIN_GROUP_ID", 0))              # Main Group ID
SPIN_TOPIC_ID = int(os.environ.get("SPIN_TOPIC_ID", 0))              # Lucky Spin Topic ID
ADMIN_LOG_GROUP_ID = int(os.environ.get("ADMIN_LOG_GROUP_ID", 0))    # Private Admin Group ID

if not BOT_TOKEN:
    raise ValueError("ERROR: Render Environment Variables တွင် BOT_TOKEN မသတ်မှတ်ရသေးပါ။")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
user_cooldowns = {}
user_active_menu = {}
BOT_USERNAME = ""

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
# 📦 Daily Database Auto-Backup
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
                    caption=f"📦 Daily Database Backup ({today})\n\nစနစ်ပျက်စီးခဲ့ပါက ဤဖိုင်ကို ပြန်လည်အသုံးပြုနိုင်ပါသည်။"
                )
        except Exception as e:
            print(f"Backup Error: {e}")

# ==========================================================
# 👥 Contact မှတစ်ဆင့် Add Member စစ်ဆေးခြင်း
# ==========================================================
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

# ==========================================================
# 🎮 Lucky Spin Menu
# ==========================================================
def get_spin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    redeem_url = f"https://t.me/{BOT_USERNAME}?start=bill"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ကံစမ်းမဲ လှည့်မည်", callback_data=f"spin_{user_id}")],
        [InlineKeyboardButton(text="ဖုန်းဘေလ်တောင်းဆိုထားသောစာရင်း", callback_data=f"pending_{user_id}")],
        [InlineKeyboardButton(text="ဖုန်းဘေလ် လဲလှည်မည်", url=redeem_url)]
    ])

# ⏳ မသုံးဘဲ ပစ်ထားသော Menu ကို အလိုအလျောက် ပြန်ဖျက်ပေးသည့် Background Task
async def auto_delete_idle_menu(chat_id: int, message_id: int, user_id: int, delay: int = 30):
    await asyncio.sleep(delay)
    if user_active_menu.get(user_id) == message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
        user_active_menu.pop(user_id, None)

@dp.message(Command("spin"))
async def open_spin_menu(message: types.Message):
    if SPIN_GROUP_ID != 0 and message.chat.id != SPIN_GROUP_ID:
        return
    if SPIN_TOPIC_ID != 0 and message.message_thread_id != SPIN_TOPIC_ID:
        return
    
    uid = message.from_user.id
    
    # Topic မရှုပ်စေရန် User ရိုက်သော /spin command စာကို ချက်ချင်း ဖျက်ပေးခြင်း
    try:
        await message.delete()
    except Exception:
        pass

    # ယခင်က ဖွင့်ထားပြီး မဖျက်ရသေးသော Menu အဟောင်းရှိပါက အရင်ဖျက်ပေးခြင်း
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
    
    sent_msg = await bot.send_message(
        chat_id=message.chat.id,
        message_thread_id=message.message_thread_id,
        text=menu_text,
        reply_markup=kb
    )
    user_active_menu[uid] = sent_msg.message_id

    # ၃၀ စက္ကန့်အတွင်း ခလုတ်မနှိပ်ပါက အလိုအလျောက် ပြန်ဖျက်မည့် Task စတင်ခြင်း
    asyncio.create_task(auto_delete_idle_menu(message.chat.id, sent_msg.message_id, uid, delay=30))

# ==========================================================
# 🎰 မဲလှည့်ခြင်း Callback
# ==========================================================
@dp.callback_query(lambda c: c.data.startswith("spin_"))
async def handle_spin(callback: types.CallbackQuery):
    orig_user_id = int(callback.data.split("_")[1])
    is_owner = (orig_user_id == OWNER_ID)
    
    if callback.from_user.id != orig_user_id:
        await callback.answer("ဤခလုတ်သည် သင့်အတွက် မဟုတ်ပါ။ /spin ဖြင့် ကိုယ်တိုင် စတင်ပါ!", show_alert=True)
        return

    # Owner မဟုတ်မှသာ Cooldown ၇ စက္ကန့် စစ်မည်
    if not is_owner:
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

    # Database မှ မဲရလဒ် တွက်ထုတ်ခြင်း
    result, data = await db.spin_wheel(orig_user_id, is_owner=is_owner)

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

    if result > 0:
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

    # 📢 မဲပေါက်ပါက အောက်တွင် Topic ကြေငြာချက် သီးသန့်ပို့ပေးခြင်း (အမြဲတမ်း ချန်ထားမည်၊ Pin မထိုးပါ)
    if result > 0:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=f"🎉 ဂုဏ်ယူပါတယ်! {callback.from_user.first_name} သည် **{result} Bill** ကံထူးသွားပါသည်!"
        )

    # ⏳ အပေါ်က ကိုယ်ပိုင် အချက်အလက်စာကို ၁၅ စက္ကန့်အကြာတွင် အလိုအလျောက် ပြန်ဖျက်ပေးခြင်း
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

    pending_list = await db.get_user_pending_red
