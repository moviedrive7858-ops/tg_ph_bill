import aiosqlite
import random
from datetime import datetime

DB_NAME = "bot_database.db"

# နဂိုမူလ သတ်မှတ်ထားသော Probability Weights (၈ မျိုး)
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                operator TEXT,
                phone TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                (user_id, username or "", first_name or "")
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
        res = await cursor.fetchone()
        return res[0] if res else 0

async def add_tokens(user_id: int, amount: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET tokens = tokens + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()
        cursor = await db.execute("SELECT tokens FROM users WHERE user_id = ?", (user_id,))
        res = await cursor.fetchone()
        return res[0] if res else 0

async def record_invitation(inviter_id: int, new_member_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT user_id FROM invited_members WHERE user_id = ?", (new_member_id,))
        if await cursor.fetchone():
            return False
        
        await db.execute("INSERT INTO invited_members (user_id, inviter_id) VALUES (?, ?)", (new_member_id, inviter_id))
        
        count_cursor = await db.execute("SELECT COUNT(*) FROM invited_members WHERE inviter_id = ?", (inviter_id,))
        total_invites = (await count_cursor.fetchone())[0]
        
        # လူ ၂ ယောက် Add တိုင်း Spin ၁ ကြိမ် အတိအကျ ပေးခြင်း (၁၀ ယောက် အပိုဆု လုံးဝ မပါတော့ပါ)
        if total_invites % 2 == 0:
            await db.execute("UPDATE users SET tokens = tokens + 1 WHERE user_id = ?", (inviter_id,))
            await db.commit()
            return True
            
        await db.commit()
        return True

async def spin_wheel(user_id: int, is_owner: bool = False):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT tokens, consecutive_losses, daily_spins, last_spin_date FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()
        tokens, losses, daily_spins, last_date = user

        if last_date != today:
            daily_spins = 0
            last_date = today

        # Token ရှိ/မရှိ စစ်ဆေးခြင်း (Owner ရော User ရော အနည်းဆုံး ၁ ကြိမ် လိုအပ်သည်)
        if tokens < 1:
            return "NO_TOKEN", 0

        # Owner မဟုတ်မှသာ Daily Limit ၁၀ ကြိမ် စစ်ဆေးမည် (Owner ဖြစ်ပါက Limit ကျော်ဖြတ်မည်)
        if not is_owner and daily_spins >= 10:
            return "LIMIT_REACHED", 0

        # Pity System (၂ ကြိမ်ဆက် မပေါက်ပါက ၅၀ သို့မဟုတ် ၁၀၀ အာမခံ)
        if losses >= 2:
            prize = random.choice([50, 100])
            new_losses = 0
        else:
            prize = random.choices(PRIZES, weights=WEIGHTS, k=1)[0]
            new_losses = losses + 1 if prize == 0 else 0

        # Owner ရော User ရော Token ကို ၁ ခု နုတ်မည်
        # သို့သော် Owner အတွက် Daily Spin အကြိမ်ရေ မတိုးစေပါ
        spin_increment = 0 if is_owner else 1

        await db.execute("""
            UPDATE users 
            SET tokens = tokens - 1, 
                coins = coins + ?, 
                consecutive_losses = ?, 
                daily_spins = daily_spins + ?, 
                last_spin_date = ? 
            WHERE user_id = ?
        """, (prize, new_losses, spin_increment, last_date, user_id))
        await db.commit()

        cursor = await db.execute("SELECT coins, tokens FROM users WHERE user_id = ?", (user_id,))
        res = await cursor.fetchone()
        return prize, res

async def create_redemption(user_id: int, operator: str, phone: str):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO redemptions (user_id, operator, phone, status) VALUES (?, ?, ?, 'pending')",
            (user_id, operator, phone)
        )
        await db.commit()
        return cursor.lastrowid

async def approve_redemption(redemption_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT user_id, operator, phone, status FROM redemptions WHERE id = ?", (redemption_id,))
        row = await cursor.fetchone()
        if row and row[3] == "pending":
            await db.execute("UPDATE redemptions SET status = 'approved' WHERE id = ?", (redemption_id,))
            await db.commit()
            return row
        return None

async def get_user_pending_redemptions(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT operator, phone, created_at FROM redemptions WHERE user_id = ? AND status = 'pending' ORDER BY id DESC",
            (user_id,)
        )
        return await cursor.fetchall()

async def get_top_10():
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT first_name, username, coins, tokens FROM users ORDER BY coins DESC LIMIT 10")
        return await cursor.fetchall()

async def get_user_detail(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()
        if not user:
            return None
        c_cursor = await db.execute("SELECT COUNT(*) FROM invited_members WHERE inviter_id = ?", (user_id,))
        inv_count = (await c_cursor.fetchone())[0]
        return user, inv_count

async def get_user_by_username(username: str):
    clean_un = username.lstrip("@")
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (clean_un,))
        row = await cursor.fetchone()
        return row[0] if row else None
