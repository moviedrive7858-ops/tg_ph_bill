import aiosqlite
import random
from datetime import datetime, timedelta

DB_NAME = "bot_database.db"

# ==========================================================
# 🎰 ပေါက်နှုန်း သတ်မှတ်ချက်များ (Win Rate: 48% | မပေါက်နှုန်း: 52%)
# ==========================================================
PRIZES = [0, 50, 100, 150, 200, 300, 500, 1000]
WEIGHTS = [52.0, 28.0, 9.5, 4.5, 3.0, 1.6, 1.0, 0.4]

# ၃ ကြိမ်မြောက် Pity (၂ ကြိမ်ဆက် မပေါက်ပါက ၅၀၊ ၁၀၀၊ ၁၅၀ အထိ အာမခံ)
PITY_PRIZES = [50, 100, 150]
PITY_WEIGHTS = [70.0, 22.0, 8.0]

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
        # 🎁 ငွေလွှဲမှတ်တမ်း Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 🎲 စိန်ခေါ်ပွဲမှတ်တမ်း Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS duels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player1_id INTEGER NOT NULL,
                player2_id INTEGER NOT NULL,
                winner_id INTEGER NOT NULL,
                match_date TEXT NOT NULL
            )
        """)
        await db.commit()

async def get_or_create_user(user_id: int, username: str, first_name: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO users (user_id, username, first_name) 
            VALUES (?, ?, ?)
            """,
            (user_id, username or "", first_name or "")
        )
        await db.execute(
            """
            UPDATE users 
            SET username = COALESCE(NULLIF(?, ''), username),
                first_name = COALESCE(NULLIF(?, ''), first_name)
            WHERE user_id = ?
            """,
            (username or "", first_name or "", user_id)
        )
        await db.commit()

        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cursor.fetchone()

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

async def deduct_coins(user_id: int, amount: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        current_coins = row[0]
        new_coins = max(0, current_coins - amount)
        await db.execute("UPDATE users SET coins = ? WHERE user_id = ?", (new_coins, user_id))
        await db.commit()
        return new_coins

async def deduct_tokens(user_id: int, amount: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT tokens FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        current_tokens = row[0]
        new_tokens = max(0, current_tokens - amount)
        await db.execute("UPDATE users SET tokens = ? WHERE user_id = ?", (new_tokens, user_id))
        await db.commit()
        return new_tokens

async def record_invitation(inviter_id: int, new_member_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT user_id FROM invited_members WHERE user_id = ?", (new_member_id,))
        if await cursor.fetchone():
            return False
        
        await db.execute("INSERT OR IGNORE INTO invited_members (user_id, inviter_id) VALUES (?, ?)", (new_member_id, inviter_id))
        
        count_cursor = await db.execute("SELECT COUNT(*) FROM invited_members WHERE inviter_id = ?", (inviter_id,))
        total_invites = (await count_cursor.fetchone())[0]
        
        if total_invites % 2 == 0:
            await db.execute("UPDATE users SET tokens = tokens + 1 WHERE user_id = ?", (inviter_id,))
            await db.commit()
            return True
            
        await db.commit()
        return False

async def spin_wheel(user_id: int, is_owner: bool = False):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT tokens, consecutive_losses, coins FROM users WHERE user_id = ?", 
            (user_id,)
        )
        user = await cursor.fetchone()
        tokens, losses, coins = user

        if not is_owner and coins >= 3000:
            return "MAX_BALANCE_REACHED", coins

        if tokens < 1:
            return "NO_TOKEN", 0

        if losses >= 2:
            prize = random.choices(PITY_PRIZES, weights=PITY_WEIGHTS, k=1)[0]
            new_losses = 0
        else:
            prize = random.choices(PRIZES, weights=WEIGHTS, k=1)[0]
            new_losses = losses + 1 if prize == 0 else 0

        await db.execute("""
            UPDATE users 
            SET tokens = tokens - 1, 
                coins = coins + ?, 
                consecutive_losses = ?, 
                last_spin_date = ? 
            WHERE user_id = ?
        """, (prize, new_losses, today, user_id))
        await db.commit()

        cursor = await db.execute("SELECT coins, tokens FROM users WHERE user_id = ?", (user_id,))
        res = await cursor.fetchone()
        return prize, res

# ==========================================================
# 🎁 Gift Transfer Logic (/gf)
# ==========================================================
async def transfer_gift_coins(sender_id: int, receiver_id: int):
    """
    စည်းမျဉ်းများ:
    - ၅၀ ကျပ် လွှဲမည် (လွှဲသူ ၆၀ နှုတ်၊ လက်ခံသူ ၅၀ ရ၊ Fee ၁၀)
    - လွှဲပြီးပါက အနည်းဆုံး ၃၀၀ ကျန်ရမည် (Coins >= 360)
    - တစ်ရက် မတူညီသော ၃ ယောက်သာ လွှဲခွင့်ရှိမည်
    - လွှဲပြီးသော အကောင့်ဆီ ၄၈ နာရီအတွင်း ထပ်မံ လွှဲခွင့်မရှိပါ
    """
    today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    two_days_ago = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_NAME) as db:
        # ၁။ လက်ကျန်ငွေ စစ်ဆေးခြင်း
        c = await db.execute("SELECT coins FROM users WHERE user_id = ?", (sender_id,))
        row = await c.fetchone()
        sender_coins = row[0] if row else 0

        if sender_coins - 60 < 300:
            return False, "INSUFFICIENT_BALANCE", sender_coins

        # ၂။ ၄၈ နာရီအတွင်း ဤလူဆီ လွှဲဖူးခြင်း ရှိ/မရှိ စစ်ဆေးခြင်း
        c = await db.execute(
            "SELECT COUNT(*) FROM transfers WHERE sender_id = ? AND receiver_id = ? AND created_at >= ?",
            (sender_id, receiver_id, two_days_ago)
        )
        if (await c.fetchone())[0] > 0:
            return False, "COOLDOWN_48H", 0

        # ၃။ ယနေ့ မတူညီသော ၃ ယောက် ပြည့်မပြည့် စစ်ဆေးခြင်း
        c = await db.execute(
            "SELECT COUNT(DISTINCT receiver_id) FROM transfers WHERE sender_id = ? AND created_at >= ?",
            (sender_id, today_start)
        )
        if (await c.fetchone())[0] >= 3:
            return False, "DAILY_LIMIT_REACHED", 0

        # ၄။ ငွေလွှဲပေးခြင်း
        await db.execute("UPDATE users SET coins = coins - 60 WHERE user_id = ?", (sender_id,))
        await db.execute("UPDATE users SET coins = coins + 50 WHERE user_id = ?", (receiver_id,))
        await db.execute(
            "INSERT INTO transfers (sender_id, receiver_id, amount) VALUES (?, ?, 50)",
            (sender_id, receiver_id)
        )
        await db.commit()

        c = await db.execute("SELECT coins FROM users WHERE user_id = ?", (sender_id,))
        new_sender_coins = (await c.fetchone())[0]
        return True, "SUCCESS", new_sender_coins

# ==========================================================
# 🎲 Duel Logic (/lucky)
# ==========================================================
async def check_duel_eligibility(player1_id: int, player2_id: int):
    """
    စည်းမျဉ်းများ:
    - အနည်းဆုံး ၃၀၀ ကျန်ရမည် (ကစားကြေး ၅၀ နှုတ်ပြီးပါက Coins >= 350)
    - တစ်ရက်လျှင် ၅ ပွဲသာ
    - ပြိုင်ဘက်တစ်ဦးနှင့် တစ်ရက်လျှင် ၂ ပွဲသာ
    """
    today = datetime.now().strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_NAME) as db:
        # ၁။ လက်ကျန်ငွေ စစ်ဆေးခြင်း (၃၅၀ ကျပ်စီ ရှိရမည်)
        c1 = await db.execute("SELECT coins FROM users WHERE user_id = ?", (player1_id,))
        c2 = await db.execute("SELECT coins FROM users WHERE user_id = ?", (player2_id,))
        r1, r2 = await c1.fetchone(), await c2.fetchone()

        p1_coins = r1[0] if r1 else 0
        p2_coins = r2[0] if r2 else 0

        if p1_coins - 50 < 300:
            return False, "P1_INSUFFICIENT"
        if p2_coins - 50 < 300:
            return False, "P2_INSUFFICIENT"

        # ၂။ Player တစ်ဦးချင်းစီ၏ ယနေ့ စုစုပေါင်းပွဲ စစ်ဆေးခြင်း (Max 5)
        c = await db.execute("SELECT COUNT(*) FROM duels WHERE (player1_id = ? OR player2_id = ?) AND match_date = ?", (player1_id, player1_id, today))
        if (await c.fetchone())[0] >= 5:
            return False, "P1_DAILY_LIMIT"

        c = await db.execute("SELECT COUNT(*) FROM duels WHERE (player1_id = ? OR player2_id = ?) AND match_date = ?", (player2_id, player2_id, today))
        if (await c.fetchone())[0] >= 5:
            return False, "P2_DAILY_LIMIT"

        # ၃။ အဆိုပါ ၂ ယောက် အချင်းချင်း ယနေ့ ပွဲအရေအတွက် စစ်ဆေးခြင်း (Max 2)
        c = await db.execute(
            """
            SELECT COUNT(*) FROM duels 
            WHERE ((player1_id = ? AND player2_id = ?) OR (player1_id = ? AND player2_id = ?))
              AND match_date = ?
            """,
            (player1_id, player2_id, player2_id, player1_id, today)
        )
        if (await c.fetchone())[0] >= 2:
            return False, "PAIR_DAILY_LIMIT"

        return True, "ELIGIBLE"

async def record_duel_match(winner_id: int, loser_id: int, player1_id: int, player2_id: int):
    """နိုင်သူထံ ၄၀ အသားတင်တိုး (၅၀ + ၄၀ = ၉၀ ရ), ရှုံးသူထံ ၅၀ နှုတ်, Bot Fee ၁၀ ဖြတ်"""
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET coins = coins + 40 WHERE user_id = ?", (winner_id,))
        await db.execute("UPDATE users SET coins = coins - 50 WHERE user_id = ?", (loser_id,))
        await db.execute(
            "INSERT INTO duels (player1_id, player2_id, winner_id, match_date) VALUES (?, ?, ?, ?)",
            (player1_id, player2_id, winner_id, today)
        )
        await db.commit()

        c = await db.execute("SELECT coins FROM users WHERE user_id = ?", (winner_id,))
        w_coins = (await c.fetchone())[0]
        c = await db.execute("SELECT coins FROM users WHERE user_id = ?", (loser_id,))
        l_coins = (await c.fetchone())[0]
        return w_coins, l_coins

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
