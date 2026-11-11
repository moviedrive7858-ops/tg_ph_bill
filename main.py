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
last_duel_call_time = 0  # ⏳ /lucky ထပ်ခေါ်ခြင်းကို ကာကွယ်မည့် Group Cooldown Tracker

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

# 👥 Member ဖိတ်ခေါ်မှု စနစ်
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

    r_mention = f"@{receiver.username}" if receiver.username else clean_name(receiver.first_name)
    s_mention = f"@{sender.username}" if sender.username else clean_name(sender.first_name)

    await bot.send_message(
        chat_id=message.chat.id,
        message_thread_id=SPIN_TOPIC_ID,
        text=f"🎁 {s_mention} မှ {r_mention} ထံသို့ <b>၄၀ ကျပ်</b> လက်ဆောင် ပေးပို့လိုက်ပါပြီ! 🎉",
        parse_mode="HTML"
    )

# ==========================================================
# 🃏 Shan Koe Mee Duel (/lucky)
# ==========================================================
async def auto_resolve_duel(match_id: str):
    await asyncio.sleep(20)
    if match_id in active_duels:
        await finish_duel(match_id)

async def finish_duel(match_id: str):
    duel = active_duels.pop(match_id, None)
    if not duel:
        return

    p1_id, p2_id = duel["p1_id"], duel["p2_id"]
    p1_name, p2_name = duel["p1_name"], duel["p2_name"]
    p1_hand, p2_hand = duel["p1_hand"], duel["p2_hand"]
    deck = duel["deck"]

    if calculate_hand_score(p1_hand) <= 4 and len(p1_hand) == 2:
        p1_hand.append(deck.pop())
    if calculate_hand_score(p2_hand) <= 4 and len
