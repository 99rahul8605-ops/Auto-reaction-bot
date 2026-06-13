import aiosqlite
import logging
from datetime import datetime, date

logger = logging.getLogger(__name__)

DB_PATH = "smm_bot.db"

class Database:
    def __init__(self):
        self.db = None

    async def init(self):
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info("Database initialized")

    async def _create_tables(self):
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                name        TEXT,
                username    TEXT,
                phone       TEXT DEFAULT '',
                balance     REAL DEFAULT 0.0,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER,
                smm_order_id    TEXT,
                service_id      TEXT,
                service_name    TEXT,
                link            TEXT,
                quantity        INTEGER,
                cost            REAL,
                start_count     INTEGER DEFAULT 0,
                remains         INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'pending',
                created_at      TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS recharges (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                order_id    TEXT UNIQUE,
                amount      REAL,
                charged     REAL,
                method      TEXT,
                status      TEXT DEFAULT 'pending',
                created_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
        """)
        await self.db.commit()

    # ─── Users ───────────────────────────────────────────────────────────────

    async def get_or_create_user(self, user_id: int, name: str, username: str) -> dict:
        async with self.db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()

        if row:
            return dict(row)

        await self.db.execute(
            "INSERT INTO users (user_id, name, username) VALUES (?, ?, ?)",
            (user_id, name, username or "")
        )
        await self.db.commit()

        async with self.db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            return dict(await cur.fetchone())

    async def deduct_balance(self, user_id: int, amount: float):
        await self.db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?",
            (amount, user_id)
        )
        await self.db.commit()

    async def admin_update_balance(self, user_id: int, amount: float):
        await self.db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await self.db.commit()
        async with self.db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_all_user_ids(self) -> list:
        async with self.db.execute("SELECT user_id FROM users") as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]

    async def get_recent_users(self, limit=10) -> list:
        async with self.db.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ─── Orders ──────────────────────────────────────────────────────────────

    async def create_order(self, user_id, smm_order_id, service_id, service_name, link, quantity, cost):
        await self.db.execute(
            """INSERT INTO orders 
               (user_id, smm_order_id, service_id, service_name, link, quantity, cost) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, smm_order_id, service_id, service_name, link, quantity, cost)
        )
        await self.db.commit()

    async def get_user_orders(self, user_id: int, limit=10) -> list:
        async with self.db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def update_order_status(self, smm_order_id: str, status: str, start_count: int, remains: int):
        await self.db.execute(
            "UPDATE orders SET status=?, start_count=?, remains=? WHERE smm_order_id=?",
            (status, start_count, remains, smm_order_id)
        )
        await self.db.commit()

    async def count_user_orders(self, user_id: int) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM orders WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0]

    async def total_spent(self, user_id: int) -> float:
        async with self.db.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM orders WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0]

    async def get_recent_orders(self, limit=10) -> list:
        async with self.db.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ─── Recharges ───────────────────────────────────────────────────────────

    async def create_recharge(self, user_id, order_id, amount, charged, method):
        await self.db.execute(
            "INSERT OR IGNORE INTO recharges (user_id, order_id, amount, charged, method) VALUES (?,?,?,?,?)",
            (user_id, order_id, amount, charged, method)
        )
        await self.db.commit()

    async def get_recharge(self, order_id: str) -> dict:
        async with self.db.execute(
            "SELECT * FROM recharges WHERE order_id = ?", (order_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def complete_recharge(self, order_id: str, user_id: int, amount: float):
        await self.db.execute(
            "UPDATE recharges SET status='completed' WHERE order_id=?", (order_id,)
        )
        await self.db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id)
        )
        await self.db.commit()

    async def reject_recharge(self, order_id: str):
        await self.db.execute(
            "UPDATE recharges SET status='rejected' WHERE order_id=?", (order_id,)
        )
        await self.db.commit()

    # ─── Stats ───────────────────────────────────────────────────────────────

    async def get_stats(self) -> dict:
        today = date.today().isoformat()
        async with self.db.execute("SELECT COUNT(*) FROM users") as c:
            users = (await c.fetchone())[0]
        async with self.db.execute("SELECT COUNT(*) FROM orders") as c:
            orders = (await c.fetchone())[0]
        async with self.db.execute("SELECT COALESCE(SUM(amount),0) FROM recharges WHERE status='completed'") as c:
            revenue = (await c.fetchone())[0]
        async with self.db.execute("SELECT COUNT(*) FROM orders WHERE created_at LIKE ?", (f"{today}%",)) as c:
            today_orders = (await c.fetchone())[0]
        return {"users": users, "orders": orders, "revenue": revenue, "today_orders": today_orders}

db = Database()
