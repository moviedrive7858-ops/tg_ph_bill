import os
import time
import asyncio
import random
import string
import re
import html
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import database as db

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
ADMIN_LOG_GROUP_ID = int(os.environ.get("ADMIN_LOG_GROUP_ID", 0))

SPIN_GROUP_ID = -1002672484902
GENERAL_TOPIC_ID = 1
SPIN_TOPIC_ID = 1328866

if not BOT_TOKEN:
    raise ValueError("ERROR: Render Environment Variables တွင် BOT_TOKEN မသတ်မှတ်ရသေးပါ။")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
user_cooldowns = {}
user_active_menu = {}
BOT_USERNAME = ""

claim_lock = asyncio.Lock()
duel_lock = asyncio.Lock()

active_duels = {}
last_duel_created_time = 0

MESSAGE_TARGET = 100

chat_stats = {
    "count": 0,
    "last_user_id": None,
    "user_consecutive_counts": 0,
    "active_drop_code": None,
    "drop_type": None,
    "drop_message_id": None,
    "claimed_drop_msg_id": None
}

def clean_name(name: str) -> str:
    if not name:
        return "User"
    return html.escape(str(name))

def generate_drop_code() -> str:
    letters = random.choices(string.ascii_uppercase, k=3)
    digits = random.choices(string.digits, k=2)
    combined = letters + digits
    random.shuffle(combined)
    return "".join(combined)

def get_random_drop_reward() -> tuple:
    reward_types = ["token", "bill_50", "bill_100"]
    reward_weights = [92.0, 6.0, 2.0]
    chosen = random.choices(reward_types, weights=reward_weights, k=1)[0]
    if chosen == "token":
        return "token", "🎟️ Free Spin Token ၁ ခု"
    elif chosen == "bill_50":
        return "bill_50", "💰 ဖုန်းဘေလ် ၅၀ ကျပ်"
    else:
        return "bill_100", "💎 ဖုန်းဘေလ် ၁၀၀ ကျပ်"

def is_valid_chat_message(text: str) -> bool:
    clean_text = text.strip()
    if len(clean_text) < 3 or clean_text.isdigit() or re.fullmatch(r"[\W_]+", clean_text):
        return False
    return True

class RedeemState(StatesGroup):
    waiting_for_reward_choice = State()
    waiting_for_operator = State()
    waiting_for_phone = State()
    waiting_for_gift_username = State()

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
                    caption=f"📦 Daily Database Backup ({today})"
                )
        except Exception as e:
            print(f"Auto Backup Error: {e}")

async def expire_drop_code_task(drop_code: str, chat_id: int, message_id: int, delay: int = 180):
    await asyncio.sleep(delay)
    async with claim_lock:
        if chat_stats["active_drop_code"] == drop_code:
            chat_stats["active_drop_code"] = None
            chat_stats["drop_type"] = None
            chat_stats["drop_message_id"] = None
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass

# ==========================================================
# 🃏 Shan Koe Mee Helper Functions
# ==========================================================
SUITS = ["♣️", "♦️", "♥️", "♠️"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

def get_card_value(rank: str) -> int:
    if rank == "A":
        return 1
    if rank in ["10", "J", "Q", "K"]:
        return 0
    return int(rank)

def calculate_hand_score(hand: list) -> int:
    return sum(get_card_value(card[0]) for card in hand) % 10

def get_hand_title(hand: list) -> str:
    total_score = calculate_hand_score(hand)
    if len(hand) == 2 and total_score == 9:
        return "🔥 ရှမ်းကိုး (၉)"
    elif len(hand) == 2 and total_score == 8:
        return "⚡ ရှစ်ဒေါင်း (၈)"
    elif total_score == 0:
        return "💀 ဘူး (၀)"
    return f"{total_score} မှတ်"

def compare_shan_hands(p1_hand: list, p2_hand: list) -> str:
    s1 = calculate_hand_score(p1_hand)
    s2 = calculate_hand_score(p2_hand)
    if s1 > s2:
        return "p1"
    if s2 > s1:
        return "p2"
    if len(p1_hand) < len(p2_hand):
        return "p1"
    if len(p2_hand) < len(p1_hand):
        return "p2"

    def card_val(c):
        return (RANKS.index(c[0]) * 4) + SUITS.index(c[1])

    return "p1" if max(card_val(c) for c in p1_hand) > max(card_val(c) for c in p2_hand) else "p2"

@dp.message(F.new_chat_members)
async def new_member_message_handler(message: types.Message):
    if message.chat.id != SPIN_GROUP_ID:
        return
    inviter = message.from_user
    if not inviter or inviter.is_bot:
        return

    await db.get_or_create_user(inviter.id, inviter.username, inviter.first_name)
    for new_member in message.new_chat_members:
        if new_member.is_bot or inviter.id == new_member.id:
            continue
        await db.get_or_create_user(new_member.id, new_member.username, new_member.first_name)
        await db.record_invitation(inviter.id, new_member.id)

# ==========================================================
# 🎁 Gift Transfer (/gf)
# ==========================================================
@dp.message(Command("gf"))
async def cmd_gift_coin(message: types.Message):
    if message.chat.id != SPIN_GROUP_ID or message.message_thread_id != SPIN_TOPIC_ID:
        return

    sender = message.from_user
    if not message.reply_to_message or not message.reply_to_message.from_user:
        return

    receiver = message.reply_to_message.from_user
    if receiver.is_bot or receiver.id == sender.id:
        return

    await db.get_or_create_user(sender.id, sender.username, sender.first_name)
    await db.get_or_create_user(receiver.id, receiver.username, receiver.first_name)

    success, reason, balance = await db.transfer_gift_coins(sender.id, receiver.id)

    if not success:
        if reason == "INSUFFICIENT_BALANCE":
            err_msg = await message.reply("❌ လက်ကျန်ငွေ ၃၀၀ အောက်ရောက်မည်ဖြစ်၍ လွှဲ၍မရပါ (အနည်းဆုံး ၃၅၀ ကျပ် ရှိရပါမည်)။")
        elif reason == "COOLDOWN_48H":
            err_msg = await message.reply("❌ ဤ Member ထံသို့ ၄၈ နာရီအတွင်း ၁ ကြိမ်သာ ငွေလွှဲခွင့်ရှိပါသည်။")
        else:
            err_msg = await message.reply("❌ တစ်ရက်လျှင် လူ ၃ ဦးထံသာ လွှဲခွင့်ရှိပါသည်။")
        await asyncio.sleep(5)
        try:
            await err_msg.delete()
            await message.delete()
        except Exception:
            pass
        return

    s_mention = f"@{sender.username}" if sender.username else f'<a href="tg://user?id={sender.id}">{clean_name(sender.first_name)}</a>'
    r_mention = f"@{receiver.username}" if receiver.username else f'<a href="tg://user?id={receiver.id}">{clean_name(receiver.first_name)}</a>'

    await bot.send_message(
        chat_id=message.chat.id,
        message_thread_id=SPIN_TOPIC_ID,
        text=f"🎁 {s_mention} မှ {r_mention} ထံသို့ <b>၄၀ ကျပ်</b> လက်ဆောင် ပေးပို့လိုက်ပါပြီ! (လွှဲခ ၁၀ ကျပ်) 🎉",
        parse_mode="HTML"
    )

# ==========================================================
# 🃏 Shan Koe Mee Duel (/lucky)
# ==========================================================
async def auto_delete_duel_result(chat_id: int, message_id: int, delay: int = 30):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def auto_resolve_duel(match_id: str):
    try:
        await asyncio.sleep(20)
        async with duel_lock:
            if match_id in active_duels:
                await finish_duel(match_id)
    except asyncio.CancelledError:
        pass

async def finish_duel(match_id: str):
    duel = active_duels.pop(match_id, None)
    if not duel:
        return

    timer_task = duel.get("timer_task")
    if timer_task and not timer_task.done():
        timer_task.cancel()

    p1_id, p2_id = duel["p1_id"], duel["p2_id"]
    p1_name, p2_name = duel["p1_name"], duel["p2_name"]
    p1_hand, p2_hand = duel["p1_hand"], duel["p2_hand"]
    deck = duel["deck"]

    if len(p1_hand) == 2 and calculate_hand_score(p1_hand) <= 4:
        p1_hand.append(deck.pop())
    if len(p2_hand) == 2 and calculate_hand_score(p2_hand) <= 4:
        p2_hand.append(deck.pop())

    winner = compare_shan_hands(p1_hand, p2_hand)
    if winner == "p1":
        w_id, l_id = p1_id, p2_id
        w_name = p1_name
    else:
        w_id, l_id = p2_id, p1_id
        w_name = p2_name

    try:
        await db.record_duel_match(w_id, l_id, p1_id, p2_id)
    except Exception as e:
        print(f"Record Duel Error: {e}")

    try:
        await bot.delete_message(chat_id=duel["chat_id"], message_id=duel["msg_id"])
    except Exception:
        pass

    p1_cards_str = " ".join([f"[{c[0]}{c[1]}]" for c in p1_hand])
    p2_cards_str = " ".join([f"[{c[0]}{c[1]}]" for c in p2_hand])

    result_text = (
        f"🃏 <b>ရှမ်းကိုးမီး ရလဒ်:</b>\n"
        f"👤 {clean_name(p1_name)}: {p1_cards_str} ({get_hand_title(p1_hand)})\n"
        f"👤 {clean_name(p2_name)}: {p2_cards_str} ({get_hand_title(p2_hand)})\n\n"
        f"🎉 <b>{clean_name(w_name)}</b> မှ <b>+၄၀ ကျပ်</b> အနိုင်ရရှိသွားပါသည်! 👑"
    )

    try:
        result_msg = await bot.send_message(
            chat_id=duel["chat_id"],
            message_thread_id=SPIN_TOPIC_ID,
            text=result_text,
            parse_mode="HTML"
        )
        asyncio.create_task(auto_delete_duel_result(duel["chat_id"], result_msg.message_id, delay=30))
    except Exception as e:
        print(f"Send Duel Result Error: {e}")

@dp.message(Command("lucky"))
async def cmd_lucky_duel(message: types.Message):
    global last_duel_created_time

    if message.chat.id != SPIN_GROUP_ID or message.message_thread_id != SPIN_TOPIC_ID:
        return

    challenger = message.from_user
    is_owner = (challenger.id == OWNER_ID)

    now = time.time()
    if not is_owner:
        if now - last_duel_created_time < 15:
            rem = int(15 - (now - last_duel_created_time))
            warn = await message.reply(f"⏳ ကစားပွဲတစ်ခု ဖွင့်ထားပြီးဖြစ်ပါသည် (သို့မဟုတ် ခဏစောင့်ပါဦး: {rem} စက္ကန့်)")
            await asyncio.sleep(4)
            try:
                await warn.delete()
                await message.delete()
            except Exception:
                pass
            return

    for c_id, d_info in list(active_duels.items()):
        if d_info.get("challenger_id") == challenger.id:
            msg = await message.reply("⚠️ သင်ဖွင့်ထားသော ကစားပွဲ ရှိနေဆဲဖြစ်ပါသည်!")
            await asyncio.sleep(4)
            await msg.delete()
            return

    user_data = await db.get_or_create_user(challenger.id, challenger.username, challenger.first_name)
    if not is_owner and user_data[3] - 50 < 300:
        msg = await message.reply("❌ လက်ကျန်ငွေ ၃၀၀ အောက်ရောက်မည်ဖြစ်၍ ကစားခွင့်မပြုပါ (အနည်းဆုံး ၃၅၀ ကျပ် ရှိရပါမည်)။")
        await asyncio.sleep(5)
        await msg.delete()
        return

    last_duel_created_time = now
    challenge_id = f"{challenger.id}_{int(time.time())}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🃏 စိန်ခေါ်မှု လက်ခံမည် (၅၀ ကျပ်)", callback_data=f"accept_duel_{challenge_id}")]
    ])

    sent_msg = await bot.send_message(
        chat_id=message.chat.id,
        message_thread_id=SPIN_TOPIC_ID,
        text=f"🃏 <b>{clean_name(challenger.first_name)}</b> က ရှမ်းကိုးမီးကစားရန် စိန်ခေါ်ထားပါသည်!",
        reply_markup=kb,
        parse_mode="HTML"
    )

    active_duels[challenge_id] = {
        "challenger_id": challenger.id,
        "challenger_name": challenger.first_name,
        "msg_id": sent_msg.message_id,
        "chat_id": message.chat.id
    }

@dp.callback_query(lambda c: c.data.startswith("accept_duel_"))
async def handle_accept_duel(callback: types.CallbackQuery):
    challenge_id = callback.data.replace("accept_duel_", "")
    accepter = callback.from_user

    async with duel_lock:
        if challenge_id not in active_duels:
            await callback.answer("စိန်ခေါ်မှု မရှိတော့ပါ!", show_alert=True)
            return

        duel_data = active_duels[challenge_id]
        challenger_id = duel_data["challenger_id"]
        challenger_name = duel_data["challenger_name"]

        if accepter.id == challenger_id:
            await callback.answer("မိမိစိန်ခေါ်မှုကို မိမိ ပြန်လက်ခံ၍ မရပါ!", show_alert=True)
            return

        await db.get_or_create_user(accepter.id, accepter.username, accepter.first_name)
        eligible, reason = await db.check_duel_eligibility(challenger_id, accepter.id, owner_id=OWNER_ID)
        
        if not eligible:
            if reason in ["P1_INSUFFICIENT", "P2_INSUFFICIENT"]:
                await callback.answer("ကစားသူတစ်ဦးတွင် လက်ကျန် ၃၅၀ မပြည့်တော့ပါ!", show_alert=True)
            elif reason == "PAIR_COOLDOWN_10M":
                await callback.answer("ဤ Player နှင့် ၃ ပွဲဆော့ပြီးသွားသဖြင့် ၁၀ မိနစ် Cooldown စောင့်ပါ!", show_alert=True)
            return

        del active_duels[challenge_id]

    await callback.answer()

    deck = [(r, s) for r in RANKS for s in SUITS]
    random.shuffle(deck)

    p1_hand = [deck.pop(), deck.pop()]
    p2_hand = [deck.pop(), deck.pop()]
    match_id = f"m_{challenger_id}_{accepter.id}_{int(time.time())}"

    p1_auto = calculate_hand_score(p1_hand) >= 7
    p2_auto = calculate_hand_score(p2_hand) >= 7

    match_dict = {
        "p1_id": challenger_id,
        "p2_id": accepter.id,
        "p1_name": challenger_name,
        "p2_name": accepter.first_name,
        "p1_hand": p1_hand,
        "p2_hand": p2_hand,
        "deck": deck,
        "p1_action": p1_auto,
        "p2_action": p2_auto,
        "chat_id": callback.message.chat.id,
        "msg_id": callback.message.message_id,
        "timer_task": None
    }
    active_duels[match_id] = match_dict

    if p1_auto and p2_auto:
        await finish_duel(match_id)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁️ မိမိဖဲချပ်ကို ကြည့်မည်", callback_data=f"peek_{match_id}")],
        [
            InlineKeyboardButton(text="📥 ဖဲ ၁ ချပ် ဆွဲမည်", callback_data=f"hit_{match_id}"),
            InlineKeyboardButton(text="✋ နေမည် (မဆွဲပါ)", callback_data=f"stand_{match_id}")
        ]
    ])

    await callback.message.edit_text(
        f"🃏 <b>ရှမ်းကိုးမီး ပွဲစဉ် စတင်ပါပြီ!</b>\n\n"
        f"👤 {clean_name(challenger_name)}: [ 🂠 🂠 ]\n"
        f"👤 {clean_name(accepter.first_name)}: [ 🂠 🂠 ]\n\n"
        f"⏳ <i>၂၀ စက္ကန့်အတွင်း မရွေးချယ်ပါက အော်တိုဖဲလှန်ပါမည်။</i>",
        reply_markup=kb,
        parse_mode="HTML"
    )

    task = asyncio.create_task(auto_resolve_duel(match_id))
    match_dict["timer_task"] = task

@dp.callback_query(lambda c: c.data.startswith("peek_"))
async def handle_peek_card(callback: types.CallbackQuery):
    match_id = callback.data.replace("peek_", "")
    duel = active_duels.get(match_id)
    if not duel:
        await callback.answer("ပွဲစဉ်ပြီးဆုံးသွားပါပြီ!", show_alert=True)
        return

    uid = callback.from_user.id
    if uid == duel["p1_id"]:
        hand = duel["p1_hand"]
    elif uid == duel["p2_id"]:
        hand = duel["p2_hand"]
    else:
        await callback.answer("❌ လက်မသရမ်းနဲ့ !", show_alert=True)
        return

    cards_str = " ".join([f"[{c[0]}{c[1]}]" for c in hand])
    score_title = get_hand_title(hand)
    await callback.answer(f"🎴 သင့်ဖဲချပ်: {cards_str}\n👉 ရလဒ်: {score_title}", show_alert=True)

@dp.callback_query(lambda c: c.data.startswith("hit_"))
async def handle_hit_card(callback: types.CallbackQuery):
    match_id = callback.data.replace("hit_", "")
    duel = active_duels.get(match_id)
    if not duel:
        await callback.answer("ပွဲစဉ်ပြီးဆုံးသွားပါပြီ!", show_alert=True)
        return

    uid = callback.from_user.id
    if uid not in [duel["p1_id"], duel["p2_id"]]:
        await callback.answer("❌ သင်သည် ဤပွဲတွင် ပါဝင်သူမဟုတ်ပါ!", show_alert=True)
        return

    player_key = "p1" if uid == duel["p1_id"] else "p2"
    hand = duel[f"{player_key}_hand"]

    if duel[f"{player_key}_action"]:
        await callback.answer("သင် ရွေးချယ်ပြီးပါပြီ!", show_alert=True)
        return

    initial_score = calculate_hand_score(hand)
    if initial_score >= 7:
        await callback.answer("၇ နှင့်အထက် ဖြစ်၍ ဖဲထပ်ဆွဲ၍ မရပါ", show_alert=True)
        duel[f"{player_key}_action"] = True
    else:
        if len(hand) == 2:
            hand.append(duel["deck"].pop())
        duel[f"{player_key}_action"] = True
        cards_str = " ".join([f"[{c[0]}{c[1]}]" for c in hand])
        await callback.answer(f"📥 ဖဲ ၁ ချပ်ဆွဲလိုက်ပါပြီ!\nလက်ရှိ: {cards_str} ({get_hand_title(hand)})", show_alert=True)

    if duel["p1_action"] and duel["p2_action"]:
        async with duel_lock:
            await finish_duel(match_id)

@dp.callback_query(lambda c: c.data.startswith("stand_"))
async def handle_stand_card(callback: types.CallbackQuery):
    match_id = callback.data.replace("stand_", "")
    duel = active_duels.get(match_id)
    if not duel:
        await callback.answer("ပွဲစဉ်ပြီးဆုံးသွားပါပြီ!", show_alert=True)
        return

    uid = callback.from_user.id
    if uid not in [duel["p1_id"], duel["p2_id"]]:
        await callback.answer("❌ သင်သည် ဤပွဲတွင် ပါဝင်သူမဟုတ်ပါ!", show_alert=True)
        return

    player_key = "p1" if uid == duel["p1_id"] else "p2"
    if duel[f"{player_key}_action"]:
        await callback.answer("သင် ရွေးချယ်ပြီးပါပြီ!", show_alert=True)
        return

    duel[f"{player_key}_action"] = True
    await callback.answer("✋ နေလိုက်ပါပြီ (ဖဲမဆွဲပါ)။", show_alert=True)

    if duel["p1_action"] and duel["p2_action"]:
        async with duel_lock:
            await finish_duel(match_id)

# ==========================================================
# 🎰 Spin Menu (/spin)
# ==========================================================
def get_spin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    redeem_url = f"https://t.me/{BOT_USERNAME}?start=bill"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ကံစမ်းမဲ လှည့်မည်", callback_data=f"spin_{user_id}")],
        [InlineKeyboardButton(text="လဲလှယ်တောင်းဆိုထားသောစာရင်း", callback_data=f"pending_{user_id}")],
        [InlineKeyboardButton(text="ဖုန်းဘေလ် / Gift လဲလှယ်မည်", url=redeem_url)]
    ])

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
    if message.chat.id != SPIN_GROUP_ID or message.message_thread_id != SPIN_TOPIC_ID:
        return
    
    uid = message.from_user.id
    try:
        await message.delete()
    except Exception:
        pass

    if uid in user_active_menu:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=user_active_menu[uid])
        except Exception:
            pass

    user = await db.get_or_create_user(uid, message.from_user.username, message.from_user.first_name)
    kb = get_spin_keyboard(uid)

    menu_text = (
        f"👤 <b>{clean_name(message.from_user.first_name)}</b>\n"
        f"💰 Bill လက်ကျန် - {user[3]} ကျပ်\n"
        f"🎟️ Spin လှည့်ခွင့် - {user[4]} ကြိမ်"
    )
    
    sent_msg = await bot.send_message(
        chat_id=message.chat.id,
        message_thread_id=SPIN_TOPIC_ID,
        text=menu_text,
        reply_markup=kb,
        parse_mode="HTML"
    )
    user_active_menu[uid] = sent_msg.message_id
    asyncio.create_task(auto_delete_idle_menu(message.chat.id, sent_msg.message_id, uid, delay=30))

@dp.callback_query(lambda c: c.data.startswith("spin_"))
async def handle_spin(callback: types.CallbackQuery):
    orig_user_id = int(callback.data.split("_")[1])
    is_owner = (orig_user_id == OWNER_ID)
    
    if callback.from_user.id != orig_user_id:
        await callback.answer("ဤခလုတ်သည် သင့်အတွက် မဟုတ်ပါ။ /spin ဖြင့် စတင်ပါ!", show_alert=True)
        return

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
    thread_id = SPIN_TOPIC_ID

    try:
        await callback.message.edit_text(f"🎰 <b>{clean_name(callback.from_user.first_name)}</b> ကံစမ်းမဲ လှည့်နေပါသည်...", parse_mode="HTML")
    except Exception:
        pass

    await asyncio.sleep(2.0)
    result, data = await db.spin_wheel(orig_user_id, is_owner=is_owner)
    user_active_menu.pop(orig_user_id, None)

    if result == "MAX_BALANCE_REACHED":
        try:
            await callback.message.edit_text(f"⚠️ <b>{clean_name(callback.from_user.first_name)}</b> သင့်အကောင့်ထဲတွင် Bill ၃,၀၀၀ ကျပ် ပြည့်နေပါပြီ။", parse_mode="HTML")
        except Exception:
            pass
        return

    if result == "NO_TOKEN":
        try:
            await callback.message.edit_text(f"<b>{clean_name(callback.from_user.first_name)}</b>\nSpin လှည့်ခွင့် မရှိတော့ပါ။ Group ထဲသို့ Contact မှ သူငယ်ချင်းများကို Add ပေးပါ။", parse_mode="HTML")
        except Exception:
            pass
        return

    prize_text = f"သင် <b>{result} Bill</b> ကံထူးသွားပါပြီ!" if result > 0 else "ယခုအကြိမ်တွင် ကံစမ်းမဲ မပေါက်ပါ။"
    result_display = (
        f"👤 <b>{clean_name(callback.from_user.first_name)}</b>\n"
        f"{prize_text}\n"
        f"💰 Bill လက်ကျန် - {data[0]} ကျပ်\n"
        f"🎟️ Spin လှည့်ခွင့် - {data[1]} ကြိမ်"
    )

    spin_again_kb = None
    if data[1] > 0:
        spin_again_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 ထပ်မံလှည့်မည်", callback_data=f"spin_{orig_user_id}")]
        ])

    try:
        await callback.message.edit_text(result_display, reply_markup=spin_again_kb, parse_mode="HTML")
    except Exception:
        pass

    if result > 0:
        winner_mention = f"@{callback.from_user.username}" if callback.from_user.username else clean_name(callback.from_user.first_name)
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=f"🎉 ဂုဏ်ယူပါတယ်! <b>{winner_mention}</b> သည် ကံစမ်းမဲမှ <b>{result} Bill</b> ကံထူးသွားပါသည်! 💰",
                parse_mode="HTML"
            )
        except Exception:
            pass

@dp.callback_query(lambda c: c.data.startswith("pending_"))
async def handle_pending_btn(callback: types.CallbackQuery):
    orig_user_id = int(callback.data.split("_")[1])
    if callback.from_user.id != orig_user_id:
        await callback.answer("သင့်အတွက် မဟုတ်ပါ!", show_alert=True)
        return

    pending_list = await db.get_user_pending_redemptions(orig_user_id)
    if not pending_list:
        await callback.answer("လွှဲရန်ကျန်ရှိသော တောင်းဆိုမှု မရှိသေးပါ။", show_alert=True)
        return

    text = "⏳ လဲလှယ်တောင်းဆိုထားသောစာရင်း:\n\n"
    for op, ph, dt, r_type in pending_list:
        if r_type == "gift":
            text += f"• 🧸 ဝက်ဝံရုပ် Gift ({ph})\n"
        else:
            text += f"• 📱 {op} ({ph})\n"
    await callback.answer(text, show_alert=True)

# ==========================================================
# 📱 /bill Command
# ==========================================================
@dp.message(Command("bill"))
@dp.message(Command("start"), F.text.startswith("/start bill"))
async def bill_cmd(message: types.Message, state: FSMContext):
    if message.chat.type != "private":
        await message.reply("ဆုငွေလဲလှယ်ရန် Bot ၏ Private Chat တွင် /bill ဟု ရိုက်ပေးပါ။")
        return

    user = await db.get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    user_coins = user[3]

    if user_coins < 1000:
        await message.reply(f"သင့်တွင် အနည်းဆုံး Bill 1,000 မပြည့်သေးပါ (လက်ရှိ: {user_coins} ကျပ်)။")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 ဖုန်းဘေလ် ၁,၀၀၀ ကျပ် လဲမည်", callback_data="redeem_type_bill")],
        [InlineKeyboardButton(text="🧸 ဝက်ဝံရုပ် Profile Gift (၁,၅၀၀ ကျပ်)", callback_data="redeem_type_bear")]
    ])
    await message.reply(f"သင့်လက်ကျန်ငွေ: <b>{user_coins} ကျပ်</b>\n\nလဲလှယ်လိုသော ဆုအမျိုးအစားကို ရွေးချယ်ပါ:", reply_markup=kb, parse_mode="HTML")
    await state.set_state(RedeemState.waiting_for_reward_choice)

@dp.callback_query(RedeemState.waiting_for_reward_choice, lambda c: c.data.startswith("redeem_type_"))
async def handle_reward_choice(callback: types.CallbackQuery, state: FSMContext):
    choice = callback.data.replace("redeem_type_", "")
    user = await db.get_or_create_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    user_coins = user[3]

    if choice == "bear":
        if user_coins < 1500:
            await callback.answer("ဝက်ဝံရုပ် Gift လဲရန် အနည်းဆုံး ၁,၅၀၀ ကျပ် လိုအပ်ပါသည်!", show_alert=True)
            return

        await callback.message.edit_text(
            "🧸 <b>ဝက်ဝံရုပ် Profile Gift လဲလှယ်ခြင်း (၁,၅၀၀ ကျပ်)</b>\n\n"
            "လက်ဆောင် ပို့ပေးရမည့် သင့် <b>Telegram Username</b> ကို ရိုက်ပို့ပေးပါ:\n"
            "(ဥပမာ - <code>@username</code>)",
            parse_mode="HTML"
        )
        await state.set_state(RedeemState.waiting_for_gift_username)
    else:
        if user_coins < 1000:
            await callback.answer("ဖုန်းဘေလ်လဲရန် အနည်းဆုံး ၁,၀၀၀ ကျပ် လိုအပ်ပါသည်!", show_alert=True)
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="MPT", callback_data="op_MPT"), InlineKeyboardButton(text="ATOM", callback_data="op_ATOM")],
            [InlineKeyboardButton(text="Ooredoo", callback_data="op_Ooredoo"), InlineKeyboardButton(text="Mytel", callback_data="op_Mytel")]
        ])
        await callback.message.edit_text("သင့်ဖုန်း အော်ပရေတာကို ရွေးချယ်ပါ:", reply_markup=kb)
        await state.set_state(RedeemState.waiting_for_operator)

@dp.message(RedeemState.waiting_for_gift_username)
async def submit_gift_username(message: types.Message, state: FSMContext):
    target_un = message.text.strip()
    if not target_un.startswith("@"):
        target_un = f"@{target_un}"

    user = await db.get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if user[3] < 1500:
        await message.reply("လက်ကျန်ငွေ မလုံလောက်တော့ပါ။")
        await state.clear()
        return

    await db.add_coins(message.from_user.id, -1500)
    redemption_id = await db.create_redemption(message.from_user.id, "Profile Gift", target_un, reward_type="gift")

    if ADMIN_LOG_GROUP_ID != 0:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Gift ပို့ပေးပြီးပြီ", callback_data=f"approve_{redemption_id}")]
        ])
        u_mention = f"@{message.from_user.username}" if message.from_user.username else clean_name(message.from_user.first_name)
        admin_log = (
            f"🔔 [🧸 ဝက်ဝံရုပ် Gift လဲလှယ်မှု အော်ဒါ]\n\n"
            f"အကောင့်: {u_mention} (<code>{message.from_user.id}</code>)\n"
            f"ပို့ပေးရမည့် Username: {target_un}\n"
            f"အချိန်: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_LOG_GROUP_ID, text=admin_log, reply_markup=admin_kb, parse_mode="HTML")
        except Exception:
            pass

    await message.reply("✅ <b>တောင်းဆိုမှု အောင်မြင်ပါသည်!</b> Admin မှ စစ်ဆေး၍ ပို့ပေးပါလိမ့်မည်။", parse_mode="HTML")
    await state.clear()

@dp.callback_query(RedeemState.waiting_for_operator, lambda c: c.data.startswith("op_"))
async def select_operator(callback: types.CallbackQuery, state: FSMContext):
    op = callback.data.split("_")[1]
    await state.update_data(operator=op)
    await callback.message.edit_text(f"အော်ပရေတာ: {op}\n\nဘေလ်ထည့်လိုသော ဖုန်းနံပါတ်ကို ရိုက်ပို့ပေးပါ:")
    await state.set_state(RedeemState.waiting_for_phone)

@dp.message(RedeemState.waiting_for_phone)
async def submit_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    data = await state.get_data()
    operator = data.get("operator")

    user = await db.get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if user[3] < 1000:
        await message.reply("လက်ကျန်ငွေ မလုံလောက်တော့ပါ။")
        await state.clear()
        return

    await db.add_coins(message.from_user.id, -1000)
    redemption_id = await db.create_redemption(message.from_user.id, operator, phone, reward_type="bill")

    if ADMIN_LOG_GROUP_ID != 0:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ ဖြည့်ပေးပြီးပြီ", callback_data=f"approve_{redemption_id}")]
        ])
        u_mention = f"@{message.from_user.username}" if message.from_user.username else clean_name(message.from_user.first_name)
        admin_log = (
            f"🔔 [📱 ဖုန်းဘေလ် လဲလှယ်မှု အော်ဒါ]\n\n"
            f"အကောင့်: {u_mention} (<code>{message.from_user.id}</code>)\n"
            f"အော်ပရေတာ: {operator}\n"
            f"ဖုန်းနံပါတ်: <code>{phone}</code>\n"
            f"အချိန်: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_LOG_GROUP_ID, text=admin_log, reply_markup=admin_kb, parse_mode="HTML")
        except Exception:
            pass

    await message.reply("တောင်းဆိုမှု အောင်မြင်ပါသည်။ Admin မှ စစ်ဆေး၍ ဖုန်းဘေလ် ဖြည့်ပေးပါလိမ့်မည်။")
    await state.clear()

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

    uid, operator, phone, _, r_type = res
    user_detail = await db.get_user_detail(uid)
    if user_detail:
        u_data = user_detail[0]
        if u_data[1]:
            user_name = f"@{u_data[1]}"
        elif u_data[2]:
            user_name = f'<a href="tg://user?id={uid}">{clean_name(u_data[2])}</a>'
        else:
            user_name = f'<a href="tg://user?id={uid}">User</a>'
    else:
        user_name = f'<a href="tg://user?id={uid}">User</a>'

    if r_type == "gift":
        announce_text = f"🎉 ဂုဏ်ယူပါတယ်! <b>{user_name}</b> သို့ 🧸 ဝက်ဝံရုပ် Profile Gift ပေးပို့ပြီးပါပြီ!"
        user_notify = f"🎉 သင်တောင်းဆိုထားသော 🧸 ဝက်ဝံရုပ် Profile Gift ကို {phone} သို့ ပို့ပေးပြီးပါပြီ!"
    else:
        announce_text = f"🎉 ဂုဏ်ယူပါတယ်! <b>{user_name}</b> သို့ ၁,၀၀၀ ကျပ် ဖုန်းဘေလ် ({operator}) ထည့်ပေးပြီးပါပြီ!"
        user_notify = f"✅ သင့်ထံသို့ ၁,၀၀၀ ကျပ် ဖုန်းဘေလ် ({operator}) ထည့်ပေးပြီးပါပြီ။"

    await callback.message.edit_text(f"✅ Approved: {user_name} ({phone})", parse_mode="HTML")
    await callback.answer("မှတ်တမ်းတင်ပြီးပါပြီ။")

    try:
        await bot.send_message(chat_id=SPIN_GROUP_ID, message_thread_id=SPIN_TOPIC_ID, text=announce_text, parse_mode="HTML")
    except Exception:
        pass

    try:
        await bot.send_message(chat_id=uid, text=user_notify)
    except Exception:
        pass

# ==========================================================
# 👑 Admin Commands
# ==========================================================
@dp.message(Command("top10"))
async def cmd_top10(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return
    rows = await db.get_top_10()
    if not rows:
        await message.reply("မှတ်တမ်းမရှိသေးပါ။")
        return
    text = "🏆 <b>Top 10 အများဆုံး စာရင်း</b>\n\n"
    for i, (fn, un, c, t) in enumerate(rows, 1):
        u_str = f"@{un}" if un else clean_name(fn)
        text += f"{i}. {u_str} — <b>{c} ကျပ်</b>\n"
    await message.reply(text, parse_mode="HTML")

@dp.message(Command("detail"))
async def cmd_detail(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return

    target_id = None
    target_user = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_id = target_user.id
        await db.get_or_create_user(target_id, target_user.username, target_user.first_name)
    else:
        args = message.text.split()
        if len(args) > 1:
            param = args[1].strip()
            if param.isdigit():
                target_id = int(param)
            elif param.startswith("@"):
                target_id = await db.get_user_by_username(param)

    if not target_id:
        await message.reply("💡 <b>အသုံးပြုနည်း:</b> <code>/detail @username</code> သို့မဟုတ် စာကို Reply ထောက်၍ ရိုက်ပါ။", parse_mode="HTML")
        return

    data = await db.get_user_detail(target_id)
    if not data:
        await message.reply("❌ မှတ်တမ်း မတွေ့ပါ။")
        return

    u, inv_count = data
    u_display = f"@{u[1]}" if u[1] else clean_name(u[2])
    await message.reply(
        f"🔍 <b>အကောင့် စစ်ဆေးချက်:</b>\n\n"
        f"👤 အမည်: {clean_name(u[2])} ({u_display})\n"
        f"🆔 User ID: <code>{u[0]}</code>\n"
        f"💰 Bill: <b>{u[3]} ကျပ်</b> | 🎟️ Token: <b>{u[4]} ခု</b>\n"
        f"👥 Add ခဲ့သူ: <b>{inv_count} ယောက်</b>",
        parse_mode="HTML"
    )

@dp.message(Command("givebill"))
async def cmd_givebill(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return

    args = message.text.split()
    target_id = None
    target_user = None
    amount = 0

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        if target_user.is_bot:
            await message.reply("❌ Bot များသို့ ထည့်ပေး၍ မရပါ။")
            return
        target_id = target_user.id
        for arg in args[1:]:
            if arg.isdigit():
                amount = int(arg)
                break
    elif len(args) >= 3:
        target_param = args[1].strip()
        amount_param = args[2].strip()
        if amount_param.isdigit():
            amount = int(amount_param)
        if target_param.isdigit():
            target_id = int(target_param)
        elif target_param.startswith("@"):
            target_id = await db.get_user_by_username(target_param)

    if not target_id or amount <= 0:
        await message.reply("💡 <b>အသုံးပြုနည်း:</b> <code>/givebill 50</code> (Reply ဖြင့်) သို့မဟုတ် <code>/givebill @username 50</code>", parse_mode="HTML")
        return

    u_name = target_user.first_name if target_user else ""
    u_uname = target_user.username if target_user else ""
    await db.get_or_create_user(target_id, u_uname, u_name)

    new_c = await db.add_coins(target_id, amount)
    display_name = clean_name(target_user.first_name) if target_user else f"<code>{target_id}</code>"
    await message.reply(f"🎁 <b>{display_name}</b> သို့ +{amount} Bill ထည့်ပေးလိုက်ပါပြီ! (လက်ရှိ: <b>{new_c} ကျပ်</b>)", parse_mode="HTML")

@dp.message(Command("givetoken"))
async def cmd_givetoken(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return

    args = message.text.split()
    target_id = None
    target_user = None
    amount = 0

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        if target_user.is_bot:
            await message.reply("❌ Bot များသို့ ထည့်ပေး၍ မရပါ။")
            return
        target_id = target_user.id
        for arg in args[1:]:
            if arg.isdigit():
                amount = int(arg)
                break
    elif len(args) >= 3:
        target_param = args[1].strip()
        amount_param = args[2].strip()
        if amount_param.isdigit():
            amount = int(amount_param)
        if target_param.isdigit():
            target_id = int(target_param)
        elif target_param.startswith("@"):
            target_id = await db.get_user_by_username(target_param)

    if not target_id or amount <= 0:
        await message.reply("💡 <b>အသုံးပြုနည်း:</b> <code>/givetoken 1</code> (Reply ဖြင့်) သို့မဟုတ် <code>/givetoken @username 1</code>", parse_mode="HTML")
        return

    u_name = target_user.first_name if target_user else ""
    u_uname = target_user.username if target_user else ""
    await db.get_or_create_user(target_id, u_uname, u_name)

    new_t = await db.add_tokens(target_id, amount)
    display_name = clean_name(target_user.first_name) if target_user else f"<code>{target_id}</code>"
    await message.reply(f"🎟️ <b>{display_name}</b> သို့ +{amount} Token ထည့်ပေးလိုက်ပါပြီ! (လက်ရှိ: <b>{new_t} ခု</b>)", parse_mode="HTML")

@dp.message(Command("deduct"))
async def cmd_deduct(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return

    args = message.text.split()
    target_id = None
    target_name = ""
    item_type = None
    amount = 0

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        if target_user.is_bot:
            return
        target_id = target_user.id
        target_name = clean_name(target_user.first_name)
        if len(args) < 3:
            return
        item_type = args[1].lower()
        if args[2].isdigit():
            amount = int(args[2])
    else:
        if len(args) < 4:
            return
        target_str = args[1].strip()
        item_type = args[2].lower()
        if args[3].isdigit():
            amount = int(args[3])

        if target_str.startswith("@") or not target_str.isdigit():
            target_id = await db.get_user_by_username(target_str)
            target_name = target_str
        else:
            target_id = int(target_str)
            target_name = f"ID: {target_id}"

    if not target_id or amount <= 0:
        return

    await db.get_or_create_user(target_id, "", target_name)
    if item_type in ["coin", "coins", "bill", "money", "mmk"]:
        new_balance = await db.deduct_coins(target_id, amount)
        await message.reply(f"✅ {target_name} ထံမှ ဘေလ် {amount} MMK နှုတ်ယူပြီးပါပြီ (လက်ကျန်: {new_balance})")
    elif item_type in ["token", "tokens"]:
        new_tokens = await db.deduct_tokens(target_id, amount)
        await message.reply(f"✅ {target_name} ထံမှ Token {amount} ခု နှုတ်ယူပြီးပါပြီ (လက်ကျန်: {new_tokens})")

@dp.message(Command("backup"))
async def cmd_manual_backup(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return
    if not os.path.exists(db.DB_NAME):
        return
    now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_file = FSInputFile(db.DB_NAME, filename=f"backup_manual_{now_str}.db")
    await message.reply_document(document=backup_file, caption=f"📦 Backup ({now_str})")

@dp.message(Command("restore"))
async def cmd_restore(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return
    if not message.reply_to_message or not message.reply_to_message.document:
        return
    doc = message.reply_to_message.document
    if not doc.file_name.endswith(".db"):
        return
    file_info = await bot.get_file(doc.file_id)
    await bot.download_file(file_info.file_path, destination=db.DB_NAME)
    await message.reply("✅ <b>Database Restore အောင်မြင်ပါပြီ!</b>", parse_mode="HTML")

# ==========================================================
# 💬 Chat Drop စနစ် (General Chat)
# ==========================================================
@dp.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def handle_chat_activity(message: types.Message):
    if message.chat.id != SPIN_GROUP_ID:
        return
    if message.message_thread_id == SPIN_TOPIC_ID:
        return

    user = message.from_user
    if not user or user.is_bot:
        return

    text = message.text or ""

    if message.reply_to_message:
        replied_msg_id = message.reply_to_message.message_id
        if chat_stats["active_drop_code"] and replied_msg_id == chat_stats["drop_message_id"]:
            entered_code = text.strip().upper()
            async with claim_lock:
                target_code = chat_stats["active_drop_code"]
                if not target_code or entered_code != target_code:
                    return

                drop_type = chat_stats["drop_type"]
                drop_msg_id = chat_stats["drop_message_id"]

                chat_stats["active_drop_code"] = None
                chat_stats["drop_type"] = None
                chat_stats["drop_message_id"] = None
                chat_stats["claimed_drop_msg_id"] = drop_msg_id

            await db.get_or_create_user(user.id, user.username, user.first_name)
            winner_mention = f"@{user.username}" if user.username else f'<a href="tg://user?id={user.id}">{clean_name(user.first_name)}</a>'

            if drop_type == "token":
                await db.add_tokens(user.id, 1)
                reward_text = "🎟️ Free Spin Token ၁ ခု"
            elif drop_type == "bill_50":
                await db.add_coins(user.id, 50)
                reward_text = "💰 ဖုန်းဘေလ် ၅၀ ကျပ်"
            else:
                await db.add_coins(user.id, 100)
                reward_text = "💎 ဖုန်းဘေလ် ၁၀၀ ကျပ်"

            congrats_text = f"🎁 <b>Claimed by {winner_mention}!</b> ({reward_text})"
            try:
                await bot.send_message(chat_id=message.chat.id, text=congrats_text, parse_mode="HTML")
                await bot.delete_message(chat_id=message.chat.id, message_id=drop_msg_id)
            except Exception:
                pass
            return

    if not is_valid_chat_message(text):
        return

    async with claim_lock:
        if chat_stats["last_user_id"] == user.id:
            if chat_stats["user_consecutive_counts"] >= 2:
                return
            chat_stats["user_consecutive_counts"] += 1
        else:
            chat_stats["last_user_id"] = user.id
            chat_stats["user_consecutive_counts"] = 1

        chat_stats["count"] += 1
        trigger_drop = (chat_stats["count"] >= MESSAGE_TARGET and chat_stats["active_drop_code"] is None)
        if trigger_drop:
            chat_stats["count"] = 0

    if trigger_drop:
        drop_code = generate_drop_code()
        d_type, _ = get_random_drop_reward()

        async with claim_lock:
            chat_stats["active_drop_code"] = drop_code
            chat_stats["drop_type"] = d_type

        try:
            drop_msg = await bot.send_message(
                chat_id=message.chat.id,
                text=f"🎁 <b>Lucky Code :</b> <code>{drop_code}</code>",
                parse_mode="HTML"
            )
            chat_stats["drop_message_id"] = drop_msg.message_id
            asyncio.create_task(expire_drop_code_task(drop_code, message.chat.id, drop_msg.message_id, delay=180))
        except Exception as e:
            print(f"Error dropping code: {e}")

# ==========================================================
# 🚀 Startup
# ==========================================================
async def main():
    global BOT_USERNAME
    await db.init_db()
    bot_info = await bot.get_me()
    BOT_USERNAME = bot_info.username
    asyncio.create_task(start_web_server())
    asyncio.create_task(auto_backup_task())
    print(f"Bot @{BOT_USERNAME} is successfully running...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
