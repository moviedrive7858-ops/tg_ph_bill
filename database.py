import aiosqlite
import random
from datetime import datetime

DB_NAME = "bot_database.db"

# 8 Slices Probability ပေါက်နိုင်ခြေ အချိုးများ
PRIZES = [0, 50, 100, 150, 200, 300, 500, 1000]
WEIGHTS = [64.0, 16.0, 6.5, 5.0, 4.0, 2.5, 1.5, 0.5]

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                coins INTEGER DEFAULT 0,
                tokens INTEGER DEFAULT 0,
                consecutive_losses INTEGER DEFAULT 0,
                daily_spins INTEGER DEFAULT 0,
                last_spin_date TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invited_members (
                user_id INTEGER PRIMARY KEY,
                inviter_id INTEGER NOT NULL,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def get_or_create_user(user_id: int, username: str, first_name: str):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()
        if not user:
            await db.execute(
                "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                (user_id, username, first_name)
            )
            await db.commit()
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user = await cursor.fetchone()
        return user

async def add_coins(user_id: int, amount: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()
        cursor = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
        return (await cursor.fetchone())[0]

async def add_tokens(user_id: int, amount: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET tokens = tokens + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()
        cursor = await db.execute("SELECT tokens FROM users WHERE user_id = ?", (user_id,))
        return (await cursor.fetchone())[0]

async def record_invitation(inviter_id: int, new_member_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT user_id FROM invited_members WHERE user_id = ?", (new_member_id,))
        if await cursor.fetchone():
            return False  # ထွက်လိုက်ဝင်လိုက် လုပ်သူကို Token မပေးပါ
        
        await db.execute("INSERT INTO invited_members (user_id, inviter_id) VALUES (?, ?)", (new_member_id, inviter_id))
        
        # Invite count စစ်ဆေးခြင်း
        count_cursor = await db.execute("SELECT COUNT(*) FROM invited_members WHERE inviter_id = ?", (inviter_id,))
        total_invites = (await count_cursor.fetchone())[0]
        
        tokens_to_add = 0
        if total_invites % 2 == 0:
            tokens_to_add += 1  # လူ ၂ ယောက်ပြည့်တိုင်း Token ၁ ခု
        if total_invites % 10 == 0:
            tokens_to_add += 1  # လူ ၁၀ ယောက် Milestone Bonus Free Token ၁ ခု
            
        if tokens_to_add > 0:
            await db.execute("UPDATE users SET tokens = tokens + ? WHERE user_id = ?", (tokens_to_add, inviter_id))
        await db.commit()
        return True

async def spin_wheel(user_id: int):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT tokens, consecutive_losses, daily_spins, last_spin_date FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()
        tokens, losses, daily_spins, last_date = user

        # Daily spin reset
        if last_date != today:
            daily_spins = 0
            last_date = today

        if daily_spins >= 10:
            return "LIMIT_REACHED", 0
        if tokens < 1:
            return "NO_TOKEN", 0

        # Pity System စစ်ဆေးခြင်း (၂ ကြိမ်ဆက် ရှုံးပါက ၃ ကြိမ်မြောက် အာမခံ)
        if losses >= 2:
            prize = random.choice([50, 100])
            new_losses = 0
        else:
            prize = random.choices(PRIZES, weights=WEIGHTS, k=1)[0]
            new_losses = losses + 1 if prize == 0 else 0

        await db.execute("""
            UPDATE users 
            SET tokens = tokens - 1, 
                coins = coins + ?, 
                consecutive_losses = ?, 
                daily_spins = ?, 
                last_spin_date = ? 
            WHERE user_id = ?
        """, (prize, new_losses, daily_spins + 1, last_date, user_id))
        await db.commit()

        cursor = await db.execute("SELECT coins, tokens FROM users WHERE user_id = ?", (user_id,))
        res = await cursor.fetchone()
        return prize, res