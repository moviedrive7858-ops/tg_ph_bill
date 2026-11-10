import os
import time
import asyncio
import random
import string
import re
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, ChatMemberUpdatedFilter, StateFilter, KICKED, LEFT, RESTRICTED, MEMBER, ADMINISTRATOR, CREATOR
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, ChatMemberUpdated
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import database as db

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
SPIN_GROUP_ID = int(os.environ.get("SPIN_GROUP_ID", 0))
SPIN_TOPIC_ID = int(os.environ.get("SPIN_TOPIC_ID", 0))
ADMIN_LOG_GROUP_ID = int(os.environ.get("ADMIN_LOG_GROUP_ID", 0))

GENERAL_TOPIC_ID = 1

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

async def expire_drop_code_task(drop_code: str, delay: int = 180):
    await asyncio.sleep(delay)
    async with claim_lock:
        if chat_stats["active_drop_code"] == drop_code:
            chat_stats["active_drop_code"] = None
            chat_stats["drop_type"] = None
            chat_stats["drop_message_id"] = None

# ==========================================================
# 🃏 Shan Koe Mee Helper Functions (Safely Handled)
# ==========================================================
SUITS = ["♣️", "♦️", "♥️", "♠️"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

def get_card_value(rank: str) -> int:
    if rank == "A":
        return 1
    if rank in ["10", "J", "Q", "K"]:
        return 0
    return int(rank)

def draw_shan_hand(deck: list) -> dict:
    hand = [deck.pop(), deck.pop()]
    initial_score = (get_card_value(hand[0][0]) + get_card_value(hand[1][0])) % 10
    
    if initial_score < 7:
        hand.append(deck.pop())
        
    total_score = sum(get_card_value(card[0]) for card in hand) % 10
    cards_display = " ".join([f"[{card[0]}{card[1]}]" for card in hand])
    
    title = f"{total_score} မှတ်"
    if len(hand) == 2 and total_score == 9:
        title = "🔥 ရှမ်းကိုး (၉)"
    elif len(hand) == 2 and total_score == 8:
        title = "⚡ ရှစ်ဒေါင်း (၈)"
    elif total_score == 0:
        title = "💀 ဘူး (၀)"
        
    return {
        "cards": hand,
        "display": cards_display,
        "score": total_score,
        "card_count": len(hand),
        "title": title
    }

def compare_shan_hands(p1: dict, p2: dict) -> str:
    if p1["score"] > p2["score"]:
        return "p1"
    if p2["score"] > p1["score"]:
        return "p2"
        
    if p1["card_count"] < p2["card_count"]:
        return "p1"
    if p2["card_count"] < p1["card_count"]:
        return "p2"
        
    def card_rank_val(card):
        r_idx = RANKS.index(card[0])
        s_idx = SUITS.index(card[1])
        return (r_idx * 4) + s_idx

    p1_max = max(card_rank_val(c) for c in p1["cards"])
    p2_max = max(card_rank_val(c) for c in p2["cards"])
    return "p1" if p1_max > p2_max else "p2"

@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=(LEFT | KICKED | RESTRICTED) >> (MEMBER | ADMINISTRATOR | CREATOR)))
async def on_user_join(event: ChatMemberUpdated):
    if SPIN_GROUP_ID != 0 and event.chat.id != SPIN_GROUP_ID:
        return
    inviter = event.from_user
    new_member = event.new_chat_member.user
    if new_member.is_bot or inviter.is_bot or inviter.id == new_member.id:
        return
    await db.get_or_create_user(inviter.id, inviter.username, inviter.first_name)
    await db.get_or_create_user(new_member.id, new_member.username, new_member.first_name)
    await db.record_invitation(inviter.id, new_member.id)

@dp.message(F.new_chat_members)
async def new_member_message_handler(message: types.Message):
    if SPIN_GROUP_ID != 0 and message.chat.id != SPIN_GROUP_ID:
        return
    inviter = message.from_user
    if not inviter or inviter.is_bot:
        return

    await db.get_or_create_user(inviter.id, inviter.username, inviter.first_name)

    for new_member in message.new_chat_members:
        if new_member.is_bot or inviter.id == new_member.id:
            continue
        
        await db.get_or_create_user(new_member.id, new_member.username, new_member.first_name)
        awarded = await db.record_invitation(inviter.id, new_member.id)
        if awarded:
            inviter_mention = f"@{inviter.username}" if inviter.username else inviter.first_name
            try:
                await message.reply(
                    f"🎉 ဂုဏ်ယူပါတယ် {inviter_mention}!\n"
                    f"Member ၂ ယောက် ဖိတ်ခေါ်မှု ပြည့်မြောက်သဖြင့် 🎟️ **Spin Token ၁ ခု** ရရှိပါသည်!",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

# ==========================================================
# 🎁 Gift Transfer (/gf)
# ==========================================================
@dp.message(Command("gf"))
async def cmd_gift_coin(message: types.Message):
    if SPIN_GROUP_ID != 0 and message.chat.id != SPIN_GROUP_ID:
        return

    sender = message.from_user
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("💡 **အသုံးပြုနည်း:**\nငွေလွှဲပေးလိုသူ၏ စာကို Reply ထောက်၍ `/gf` ဟု ရိုက်ပေးပါ။\n\n📌 *သတ်မှတ်ချက်: ၅၀ ကျပ် လွှဲမည် (Fee ၁၀ ကျပ် ဖြတ်မည်၊ အနည်းဆုံး ၃၀၀ ကျန်ရမည်)*")
        return

    receiver = message.reply_to_message.from_user
    if receiver.is_bot:
        await message.reply("❌ Bot များထံသို့ ငွေလွှဲ၍ မရပါ။")
        return

    if receiver.id == sender.id:
        await message.reply("❌ မိမိအကောင့်ထံသို့ ပြန်လည် လွှဲယူ၍ မရပါ။")
        return

    await db.get_or_create_user(sender.id, sender.username, sender.first_name)
    await db.get_or_create_user(receiver.id, receiver.username, receiver.first_name)

    success, reason, balance = await db.transfer_gift_coins(sender.id, receiver.id)

    if not success:
        if reason == "INSUFFICIENT_BALANCE":
            await message.reply(f"❌ **ငွေလွှဲရန် လက်ကျန်မလုံလောက်ပါ!**\nလွှဲပြီးပါက အနည်းဆုံး ၃၀၀ ကျပ် ကျန်ရှိနေရပါမည် (လက်ရှိ: {balance} ကျပ်)။")
        elif reason == "COOLDOWN_48H":
            await message.reply("❌ **၄၈ နာရီ မပြည့်သေးပါ!**\nဤ Member ထံသို့ ၄၈ နာရီအတွင်း ၁ ကြိမ်သာ ငွေလွှဲခွင့်ရှိပါသည်။")
        elif reason == "DAILY_LIMIT_REACHED":
            await message.reply("❌ **တစ်ရက် ကန့်သတ်ချက် ပြည့်သွားပါပြီ!**\nတစ်ရက်လျှင် မတူညီသော လူ ၃ ဦးထံသာ ငွေလွှဲခွင့်ရှိပါသည်။")
        return

    r_mention = f"@{receiver.username}" if receiver.username else receiver.first_name
    await message.reply(
        f"🎁 **လက်ဆောင်ငွေလွှဲ အောင်မြင်ပါသည်!**\n\n"
        f"👤 လက်ခံသူ: {r_mention}\n"
        f"💰 လွှဲငွေ: ၅၀ ကျပ် (ရောက်ရှိ)\n"
        f"🏷️ လွှဲခ (Fee): ၁၀ ကျပ်\n"
        f"💳 သင့်လက်ကျန်: **{balance} ကျပ်**",
        parse_mode="Markdown"
    )

async def expire_duel_task(challenge_id: str, sent_msg: types.Message):
    await asyncio.sleep(60)
    if challenge_id in active_duels:
        del active_duels[challenge_id]
        try:
            await sent_msg.edit_text("⏳ **စိန်ခေါ်မှု သက်တမ်းကုန်ဆုံးသွားပါပြီ။**")
        except Exception:
            pass

@dp.message(Command("lucky"))
async def cmd_lucky_duel(message: types.Message):
    if SPIN_GROUP_ID != 0 and message.chat.id != SPIN_GROUP_ID:
        return

    challenger = message.from_user
    is_owner = (challenger.id == OWNER_ID)

    for c_id, d_info in list(active_duels.items()):
        if d_info["challenger_id"] == challenger.id:
            await message.reply("⚠️ **သင်ဖွင့်ထားသော စိန်ခေါ်မှု ရှိနေဆဲဖြစ်ပါသည်!**\nတစ်ဖက်လူ လက်ခံသည်အထိ (သို့မဟုတ် သက်တမ်းကုန်သည်အထိ) စောင့်ဆိုင်းပေးပါ။")
            return

    user_data = await db.get_or_create_user(challenger.id, challenger.username, challenger.first_name)

    if not is_owner and user_data[3] - 50 < 300:
        await message.reply(f"❌ **ကစားရန် လက်ကျန်မလုံလောက်ပါ!**\nကစားပြီးပါက အနည်းဆုံး ၃၀၀ ကျပ် ကျန်ရမည်ဖြစ်၍ အနည်းဆုံး ၃၅၀ ကျပ် ရှိရပါမည် (လက်ရှိ: {user_data[3]} ကျပ်)။")
        return

    challenge_id = f"{challenger.id}_{int(time.time())}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🃏 ရှမ်းကိုးမီး စိန်ခေါ်မှု လက်ခံမည် (၅၀ ကျပ်)", callback_data=f"accept_duel_{challenge_id}")]
    ])

    sent_msg = await message.reply(
        f"🃏 **{challenger.first_name}** က ၅၀ ကျပ်တန် ရှမ်းကိုးမီး စိန်ခေါ်ထားပါသည်!\n\n"
        f"💰 လောင်းကြေး: ၅၀ စီ (နိုင်သူ ၉၀ ကျပ်ရမည်, Fee ၁၀)\n"
        f"📌 *စည်းမျဉ်း: ကစားပြီးပါက အနည်းဆုံး ၃၀၀ ကျပ် ကျန်ရမည်*",
        reply_markup=kb,
        parse_mode="Markdown"
    )

    active_duels[challenge_id] = {
        "challenger_id": challenger.id,
        "challenger_name": challenger.first_name,
        "msg_id": sent_msg.message_id,
        "chat_id": message.chat.id
    }

    asyncio.create_task(expire_duel_task(challenge_id, sent_msg))

@dp.callback_query(lambda c: c.data.startswith("accept_duel_"))
async def handle_accept_duel(callback: types.CallbackQuery):
    challenge_id = callback.data.replace("accept_duel_", "")
    accepter = callback.from_user

    async with duel_lock:
        if challenge_id not in active_duels:
            await callback.answer("စိန်ခေါ်မှု မရှိတော့ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ!", show_alert=True)
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
            if reason == "P1_INSUFFICIENT":
                await callback.answer(f"{challenger_name} တွင် လက်ကျန် ၃၅၀ မပြည့်တော့ပါ!", show_alert=True)
            elif reason == "P2_INSUFFICIENT":
                await callback.answer("သင့်တွင် လက်ကျန်ငွေ အနည်းဆုံး ၃၅၀ ကျပ် ရှိရပါမည်!", show_alert=True)
            elif reason == "P1_DAILY_LIMIT":
                await callback.answer(f"{challenger_name} ယနေ့အတွက် ၅ ပွဲ ပြည့်သွားပါပြီ!", show_alert=True)
            elif reason == "P2_DAILY_LIMIT":
                await callback.answer("သင်သည် ယနေ့အတွက် အများဆုံး ၅ ပွဲ ပြည့်သွားပါပြီ!", show_alert=True)
            elif reason == "PAIR_DAILY_LIMIT":
                await callback.answer("ဤ Player နှင့် ယနေ့အတွက် ၂ ပွဲ ပြည့်သွားပါပြီ!", show_alert=True)
            return

        del active_duels[challenge_id]

    await callback.answer()
    
    chat_id = callback.message.chat.id
    thread_id = callback.message.message_thread_id
    
    try:
        await callback.message.edit_text(
            f"🃏 **{challenger_name}** vs **{accepter.first_name}**\n\n"
            f"🎴 ရှမ်းကိုးမီး ဖဲချပ်ဝေနေပါပြီ! ခဏစောင့်ပေးပါ...",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    deck = [(r, s) for r in RANKS for s in SUITS]
    random.shuffle(deck)

    p1_hand = draw_shan_hand(deck)
    p2_hand = draw_shan_hand(deck)

    await asyncio.sleep(2.0)

    winner = compare_shan_hands(p1_hand, p2_hand)
    if winner == "p1":
        winner_id, loser_id = challenger_id, accepter.id
        winner_name, loser_name = challenger_name, accepter.first_name
    else:
        winner_id, loser_id = accepter.id, challenger_id
        winner_name, loser_name = accepter.first_name, challenger_name

    w_coins, l_coins = await db.record_duel_match(winner_id, loser_id, challenger_id, accepter.id)

    result_text = (
        f"🃏 **ရှမ်းကိုးမီးပွဲစဉ် ရလဒ် ထွက်ပေါ်ပါပြီ!**\n\n"
        f"👤 **{challenger_name}**: {p1_hand['display']}\n"
        f"👉 ရလဒ်: **{p1_hand['title']}**\n\n"
        f"👤 **{accepter.first_name}**: {p2_hand['display']}\n"
        f"👉 ရလဒ်: **{p2_hand['title']}**\n"
        f"━━━━━━━━━━━━━━\n"
        f"👑 အနိုင်ရရှိသူ: **{winner_name}** (+၄၀ ကျပ်)\n"
        f"💀 ရှုံးနိမ့်သူ: **{loser_name}** (-၅၀ ကျပ်)\n"
        f"🏷️ စနစ်ကြေး (Fee): ၁၀ ကျပ်\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 {winner_name} လက်ကျန်: **{w_coins} ကျပ်**\n"
        f"💰 {loser_name} လက်ကျန်: **{l_coins} ကျပ်**"
    )

    try:
        await bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=result_text, parse_mode="Markdown")
    except Exception:
        try:
            await bot.send_message(chat_id=chat_id, text=result_text, parse_mode="Markdown")
        except Exception:
            pass

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
    if SPIN_GROUP_ID != 0 and message.chat.id != SPIN_GROUP_ID:
        return
    if SPIN_TOPIC_ID != 0 and message.message_thread_id != SPIN_TOPIC_ID:
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
    thread_id = callback.message.message_thread_id

    try:
        await callback.message.edit_text(f"🎰 {callback.from_user.first_name} ကံစမ်းမဲ လှည့်နေပါသည်...")
    except Exception:
        pass

    await asyncio.sleep(2.0)

    result, data = await db.spin_wheel(orig_user_id, is_owner=is_owner)
    user_active_menu.pop(orig_user_id, None)

    if result == "MAX_BALANCE_REACHED":
        try:
            await callback.message.edit_text(f"⚠️ {callback.from_user.first_name} သင့်အကောင့်ထဲတွင် Bill ၃,၀၀၀ ကျပ် ပြည့်နေပါပြီ။ ဖုန်းဘေလ် သို့မဟုတ် Gift အရင်လဲလှယ်ပြီးမှ ပြန်လှည့်ပါ။")
        except Exception:
            pass
        return

    if result == "NO_TOKEN":
        try:
            await callback.message.edit_text(f"{callback.from_user.first_name}\nSpin လှည့်ခွင့် မရှိတော့ပါ။ Group ထဲသို့ Contact မှ သူငယ်ချင်းများကို Add ပေးပါ။")
        except Exception:
            pass
        return

    prize_text = f"သင် {result} Bill ကံထူးသွားပါပြီ!" if result > 0 else "ယခုအကြိမ်တွင် ကံစမ်းမဲ မပေါက်ပါ ။"

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

    try:
        await callback.message.edit_text(result_display, reply_markup=spin_again_kb)
    except Exception:
        pass

    if result > 0:
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=f"🎉 ဂုဏ်ယူပါတယ်! {callback.from_user.first_name} သည် **{result} Bill** ကံထူးသွားပါသည်!"
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
    await message.reply(f"သင့်လက်ကျန်ငွေ: **{user_coins} ကျပ်**\n\nလဲလှယ်လိုသော ဆုအမျိုးအစားကို ရွေးချယ်ပါ:", reply_markup=kb, parse_mode="Markdown")
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
            "🧸 **ဝက်ဝံရုပ် Profile Gift လဲလှယ်ခြင်း (၁,၅၀၀ ကျပ်)**\n\n"
            "လက်ဆောင် ပို့ပေးရမည့် သင့် **Telegram Username** ကို ရိုက်ပို့ပေးပါ:\n"
            "(ဥပမာ - `@username` ဟု စာပို့ပေးပါ)"
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
        admin_log = (
            f"🔔 [🧸 ဝက်ဝံရုပ် Gift လဲလှယ်မှု အော်ဒါအသစ်]\n\n"
            f"အကောင့်: {message.from_user.first_name} (`{message.from_user.id}`)\n"
            f"လက်ဆောင်အမျိုးအစား: ဝက်ဝံရုပ် Profile Gift\n"
            f"ပို့ပေးရမည့် Username: {target_un}\n"
            f"ကုန်ကျစရိတ်: 1,500 Bill\n"
            f"အခြေအနေ: ⏳ Pending\n"
            f"အချိန်: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_LOG_GROUP_ID, text=admin_log, reply_markup=admin_kb)
        except Exception:
            pass

    await message.reply(
        f"✅ **တောင်းဆိုမှု အောင်မြင်ပါသည်!**\n\n"
        f"🧸 ဆု: ဝက်ဝံရုပ် Profile Gift\n"
        f"👤 ပို့ပေးရမည့် အကောင့်: {target_un}\n\n"
        f"Admin မှ စစ်ဆေး၍ သင့် Profile သို့ ဝက်ဝံရုပ် လက်ဆောင် ပို့ပေးပါလိမ့်မည်။"
    )
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
        admin_log = (
            f"🔔 [📱 ဖုန်းဘေလ် လဲလှယ်မှု အော်ဒါအသစ်]\n\n"
            f"အကောင့်: {message.from_user.first_name} (`{message.from_user.id}`)\n"
            f"အော်ပရေတာ: {operator}\n"
            f"ဖုန်းနံပါတ်: `{phone}`\n"
            f"ကုန်ကျစရိတ်: 1,000 Bill\n"
            f"အချိန်: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        try:
            await bot.send_message(chat_id=ADMIN_LOG_GROUP_ID, text=admin_log, reply_markup=admin_kb)
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
    user_info = await db.get_or_create_user(uid, "", "")
    user_name = user_info[2] or "User"

    if r_type == "gift":
        updated_text = (
            f"✅ [🧸 ဝက်ဝံရုပ် Gift ပို့ပြီးပါပြီ]\n\n"
            f"အကောင့်: {user_name} (`{uid}`)\n"
            f"လက်ဆောင်: ဝက်ဝံရုပ် Profile Gift\n"
            f"Username: {phone}\n"
            f"ပမာဏ: ၁,၅၀၀ ကျပ်\n"
            f"အခြေအနေ: ပေးပို့ပြီးစီး"
        )
        user_notify = f"🎉 ဂုဏ်ယူပါတယ်! သင်တောင်းဆိုထားသော 🧸 ဝက်ဝံရုပ် Profile Gift ကို {phone} သို့ ပို့ပေးပြီးပါပြီ!"
        announce_text = f"🎉 ဂုဏ်ယူပါတယ်! {user_name} သို့ 🧸 ဝက်ဝံရုပ် Profile Gift ပေးပို့ပြီးပါပြီ!"
    else:
        updated_text = (
            f"✅ [📱 ဘေလ်ဖြည့်ပြီးပါပြီ]\n\n"
            f"အကောင့်: {user_name} (`{uid}`)\n"
            f"အော်ပရေတာ: {operator}\n"
            f"ဖုန်းနံပါတ်: `{phone}`\n"
            f"ပမာဏ: ၁,၀၀၀ ကျပ်\n"
            f"အခြေအနေ: ဖြည့်သွင်းပြီးစီး"
        )
        user_notify = f"✅ သင့်ထံသို့ ၁,၀၀၀ ကျပ် ဖုန်းဘေလ် ({operator}) ထည့်ပေးပြီးပါပြီ။ ကျေးဇူးတင်ပါသည်!"
        announce_text = f"🎉 ဂုဏ်ယူပါတယ်! {user_name} သို့ ၁,၀၀၀ ကျပ် ဖုန်းဘေလ် ({operator}) ထည့်ပေးပြီးပါပြီ!"

    await callback.message.edit_text(updated_text)
    await callback.answer("မှတ်တမ်းတင်ပြီးပါပြီ။")

    if SPIN_GROUP_ID != 0 and SPIN_TOPIC_ID != 0:
        try:
            await bot.send_message(
                chat_id=SPIN_GROUP_ID,
                message_thread_id=SPIN_TOPIC_ID,
                text=announce_text
            )
        except Exception:
            pass

    try:
        await bot.send_message(chat_id=uid, text=user_notify)
    except Exception:
        pass

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
        text += f"{i}. {u_str} — **{c} ကျပ်**\n"
    await message.reply(text)

# ==========================================================
# 🔍 User Detail (/detail)
# ==========================================================
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
        await message.reply("💡 **အသုံးပြုနည်း:** စာကို Reply ထောက်၍ `/detail` သို့မဟုတ် `/detail @username` / `/detail 12345678`")
        return

    data = await db.get_user_detail(target_id)
    if not data:
        await message.reply("❌ မှတ်တမ်း မတွေ့ပါ။")
        return

    u, inv_count = data
    await message.reply(
        f"🔍 **အကောင့် စစ်ဆေးချက်:**\n\n"
        f"👤 အမည်: {u[2]} (@{u[1] or 'none'})\n"
        f"🆔 User ID: `{u[0]}`\n"
        f"💰 Bill လက်ကျန်: **{u[3]} ကျပ်**\n"
        f"🎟️ Spin လှည့်ခွင့်: **{u[4]} ကြိမ်**\n"
        f"👥 Add ခဲ့သူ: **{inv_count} ယောက်**"
    )

# ==========================================================
# 🎁 Admin Give Bill (/givebill)
# ==========================================================
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
            await message.reply("❌ Bot များထံသို့ Bill ထည့်ပေး၍ မရပါ။")
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
        await message.reply(
            "💡 **အသုံးပြုနည်း:**\n"
            "• Reply ထောက်၍: `/givebill 50`\n"
            "• ID/@username ဖြင့်: `/givebill @username 50` သို့မဟုတ် `/givebill 12345678 50`",
            parse_mode="Markdown"
        )
        return

    u_name = target_user.first_name if target_user else ""
    u_uname = target_user.username if target_user else ""
    await db.get_or_create_user(target_id, u_uname, u_name)

    new_c = await db.add_coins(target_id, amount)
    display_name = target_user.first_name if target_user else f"`{target_id}`"
    await message.reply(f"🎁 **{display_name}** သို့ +{amount} Bill ထည့်ပေးလိုက်ပါပြီ! (လက်ရှိ: **{new_c} ကျပ်**)", parse_mode="Markdown")

# ==========================================================
# 🎟️ Admin Give Token (/givetoken)
# ==========================================================
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
            await message.reply("❌ Bot များထံသို့ Token ထည့်ပေး၍ မရပါ။")
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
        await message.reply(
            "💡 **အသုံးပြုနည်း:**\n"
            "• Reply ထောက်၍: `/givetoken 1`\n"
            "• ID/@username ဖြင့်: `/givetoken @username 1` သို့မဟုတ် `/givetoken 12345678 1`",
            parse_mode="Markdown"
        )
        return

    u_name = target_user.first_name if target_user else ""
    u_uname = target_user.username if target_user else ""
    await db.get_or_create_user(target_id, u_uname, u_name)

    new_t = await db.add_tokens(target_id, amount)
    display_name = target_user.first_name if target_user else f"`{target_id}`"
    await message.reply(f"🎟️ **{display_name}** သို့ +{amount} Token ထည့်ပေးလိုက်ပါပြီ! (လက်ရှိ: **{new_t} ခု**)", parse_mode="Markdown")

# ==========================================================
# ✂️ Admin Deduct (/deduct)
# ==========================================================
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
            await message.reply("❌ Bot များထံမှ နှုတ်ယူ၍ မရနိုင်ပါ။")
            return
        target_id = target_user.id
        target_name = target_user.first_name

        if len(args) < 3:
            await message.reply("⚠️ **Reply ဖြင့် နှုတ်နည်း:** `/deduct coin 50` သို့မဟုတ် `/deduct token 1`", parse_mode="Markdown")
            return
        item_type = args[1].lower()
        if args[2].isdigit():
            amount = int(args[2])
    else:
        if len(args) < 4:
            await message.reply("⚠️ **ID/Username ဖြင့် သုံးနည်း:** `/deduct <ID/@username> <coin/token> <ပမာဏ>`", parse_mode="Markdown")
            return
        target_str = args[1].strip()
        item_type = args[2].lower()
        if args[3].isdigit():
            amount = int(args[3])

        if target_str.startswith("@") or not target_str.isdigit():
            target_id = await db.get_user_by_username(target_str)
            if not target_id:
                await message.reply(f"❌ User **{target_str}** အား ရှာမတွေ့ပါ။", parse_mode="Markdown")
                return
            target_name = target_str
        else:
            target_id = int(target_str)
            target_name = f"ID: `{target_id}`"

    if not target_id or amount <= 0:
        await message.reply("❌ ပမာဏသည် 0 ထက်ကြီးသော ဂဏန်း ဖြစ်ရပါမည်။")
        return

    await db.get_or_create_user(target_id, "", target_name)

    if item_type in ["coin", "coins", "bill", "money", "mmk"]:
        new_balance = await db.deduct_coins(target_id, amount)
        await message.reply(
            f"✅ **{target_name}** ထံမှ ဘေလ် **{amount} MMK** နှုတ်ယူပြီးပါပြီ!\n💰 လက်ကျန်ငွေ: **{new_balance} MMK**",
            parse_mode="Markdown"
        )
    elif item_type in ["token", "tokens"]:
        new_tokens = await db.deduct_tokens(target_id, amount)
        await message.reply(
            f"✅ **{target_name}** ထံမှ Token **{amount} ခု** နှုတ်ယူပြီးပါပြီ!\n🎟️ လက်ကျန် Token: **{new_tokens} ခု**",
            parse_mode="Markdown"
        )
    else:
        await message.reply("❌ `coin` သို့မဟုတ် `token` ဟု ရွေးချယ်ပေးပါ။", parse_mode="Markdown")

@dp.message(Command("backup"))
async def cmd_manual_backup(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return
    if not os.path.exists(db.DB_NAME):
        await message.reply("❌ Database ဖိုင် မရှိသေးပါ။")
        return

    status_msg = await message.reply("⏳ Database ဖိုင်ကို ထုတ်ယူနေပါသည်...")
    try:
        now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_file = FSInputFile(db.DB_NAME, filename=f"backup_manual_{now_str}.db")
        
        await message.reply_document(
            document=backup_file,
            caption=f"📦 **Update Database Backup**\n\n📅 အချိန်: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n👤 ထုတ်ယူသူ: Owner"
        )
        await status_msg.delete()

        if ADMIN_LOG_GROUP_ID != 0 and message.chat.id != ADMIN_LOG_GROUP_ID:
            try:
                await bot.send_document(
                    chat_id=ADMIN_LOG_GROUP_ID,
                    document=backup_file,
                    caption=f"📦 **Update Database Backup (Auto-Log)**\n\n📅 အချိန်: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
            except Exception:
                pass
    except Exception as e:
        await status_msg.edit_text(f"❌ Backup Error: {e}")

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

# ==========================================================
# 💬 General Topic စကားဝိုင်းနှင့် Chat Drop စနစ်
# ==========================================================
@dp.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def handle_chat_activity(message: types.Message):
    if SPIN_GROUP_ID != 0 and message.chat.id != SPIN_GROUP_ID:
        return

    msg_topic_id = message.message_thread_id
    if SPIN_TOPIC_ID != 0 and msg_topic_id == SPIN_TOPIC_ID:
        return
    if msg_topic_id not in [GENERAL_TOPIC_ID, 1, None]:
        return

    user = message.from_user
    if not user or user.is_bot:
        return

    text = message.text or ""
    # Forum Thread ID ကို တိကျစွာ ခွဲခြားသတ်မှတ်ခြင်း
    target_thread_id = message.message_thread_id if getattr(message, "is_topic_message", False) else None

    # 🎯 Drop Code အား Reply ပြန်၍ ကောက်ယူခြင်း
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
            safe_name = (user.first_name or "User").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            winner_mention = f"@{user.username}" if user.username else f'<a href="tg://user?id={user.id}">{safe_name}</a>'

            if drop_type == "token":
                await db.add_tokens(user.id, 1)
                reward_text = "🎟️ Free Spin Token ၁ ခု"
            elif drop_type == "bill_50":
                await db.add_coins(user.id, 50)
                reward_text = "💰 ဖုန်းဘေလ် ၅၀ ကျပ်"
            else:
                await db.add_coins(user.id, 100)
                reward_text = "💎 ဖုန်းဘေလ် ၁၀၀ ကျပ်"

            congrats_text = (
                f"🎁 <b>Claimed by {winner_mention}!</b>\n"
                f"🏆 ဆု: <b>{reward_text}</b>\n"
                f"🔑 Code: <code>{target_code}</code>"
            )

            try:
                await bot.send_message(chat_id=message.chat.id, message_thread_id=target_thread_id, text=congrats_text, parse_mode="HTML")
            except Exception:
                try:
                    await bot.send_message(chat_id=message.chat.id, text=congrats_text, parse_mode="HTML")
                except Exception:
                    pass

            try:
                await bot.edit_message_text(chat_id=message.chat.id, message_id=drop_msg_id, text=congrats_text, parse_mode="HTML")
            except Exception:
                pass
            return

        elif chat_stats["claimed_drop_msg_id"] and replied_msg_id == chat_stats["claimed_drop_msg_id"]:
            await message.reply("❌ **ယခုကုတ်သည် ရယူပြီးသားဖြစ်ပါသည်။**\nနောက်ထပ်ကုတ်များရယူရန် GP ထဲစကားဝင်ပြောပါ။", parse_mode="Markdown")
            return

    # 📊 စာကြောင်းရေ စစ်ဆေးခြင်း
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

    # 🎁 စာကြောင်းရေ ၁၀၀ ပြည့်ပါက Drop ချပေးခြင်း
    if trigger_drop:
        drop_code = generate_drop_code()
        d_type, _ = get_random_drop_reward()

        async with claim_lock:
            chat_stats["active_drop_code"] = drop_code
            chat_stats["drop_type"] = d_type

        try:
            drop_msg = await bot.send_message(
                chat_id=message.chat.id,
                message_thread_id=target_thread_id,
                text=f"🎁 **Lucky Code :** `{drop_code}`",
                parse_mode="Markdown"
            )
        except Exception:
            drop_msg = await bot.send_message(
                chat_id=message.chat.id,
                text=f"🎁 **Lucky Code :** `{drop_code}`",
                parse_mode="Markdown"
            )

        chat_stats["drop_message_id"] = drop_msg.message_id
        asyncio.create_task(expire_drop_code_task(drop_code, delay=180))

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
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member"])

if __name__ == "__main__":
    asyncio.run(main())
