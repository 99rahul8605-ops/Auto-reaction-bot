import aiosqlite
import logging
import asyncio
from datetime import date

logger = logging.getLogger(__name__)

DB_PATH = "smm_bot.db"

# ─── MongoDB Setup ────────────────────────────────────────────────────────────

try:
    from motor.motor_asyncio import AsyncIOMotorClient
    MONGO_AVAILABLE = True
except ImportError:
    MONGO_AVAILABLE = False
    logger.warning("motor not installed, MongoDB sync disabled")

class Database:
    def __init__(self):
        self.db = None        # SQLite
        self.mongo = None     # MongoDB client
        self.mdb = None       # MongoDB database

    async def init(self):
        # SQLite init
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        await self._create_tables()

        # MongoDB init
        if MONGO_AVAILABLE:
            try:
                from config import config
                if config.MONGO_URI:
                    self.mongo = AsyncIOMotorClient(config.MONGO_URI)
                    self.mdb = self.mongo[config.MONGO_DB_NAME]
                    await self._sync_from_mongo()
                    logger.info("✅ MongoDB connected and synced")
                else:
                    logger.warning("MONGO_URI not set, skipping MongoDB")
            except Exception as e:
                logger.error(f"MongoDB init failed: {e}")

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

    # ─── MongoDB Sync ─────────────────────────────────────────────────────────

    async def _sync_from_mongo(self):
        """On startup: restore SQLite from MongoDB if SQLite is empty/new"""
        try:
            # Check if SQLite has any users
            async with self.db.execute("SELECT COUNT(*) FROM users") as c:
                count = (await c.fetchone())[0]

            if count > 0:
                logger.info(f"SQLite has {count} users, skipping restore")
                return

            logger.info("SQLite empty — restoring from MongoDB...")

            # Restore users
            async for u in self.mdb.users.find():
                await self.db.execute(
                    "INSERT OR IGNORE INTO users (user_id, name, username, phone, balance, created_at) VALUES (?,?,?,?,?,?)",
                    (u["user_id"], u.get("name",""), u.get("username",""),
                     u.get("phone",""), u.get("balance", 0.0), u.get("created_at",""))
                )

            # Restore orders
            async for o in self.mdb.orders.find():
                await self.db.execute(
                    """INSERT OR IGNORE INTO orders
                       (user_id, smm_order_id, service_id, service_name, link, quantity, cost, start_count, remains, status, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (o["user_id"], o["smm_order_id"], o["service_id"], o["service_name"],
                     o["link"], o["quantity"], o["cost"], o.get("start_count",0),
                     o.get("remains",0), o.get("status","pending"), o.get("created_at",""))
                )

            # Restore recharges
            async for r in self.mdb.recharges.find():
                await self.db.execute(
                    "INSERT OR IGNORE INTO recharges (user_id, order_id, amount, charged, method, status, created_at) VALUES (?,?,?,?,?,?,?)",
                    (r["user_id"], r["order_id"], r["amount"], r["charged"],
                     r["method"], r.get("status","pending"), r.get("created_at",""))
                )

            await self.db.commit()
            logger.info("✅ SQLite restored from MongoDB")

        except Exception as e:
            logger.error(f"MongoDB restore failed: {e}")

    def _mongo_fire(self, coro):
        """Fire and forget MongoDB update — never block SQLite operations"""
        if self.mdb is None:
            return
        asyncio.create_task(self._safe_mongo(coro))

    async def _safe_mongo(self, coro):
        try:
            await coro
        except Exception as e:
            logger.error(f"MongoDB sync error: {e}")

    # ─── Users ────────────────────────────────────────────────────────────────

    async def get_or_create_user(self, user_id: int, name: str, username: str) -> dict:
        async with self.db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row:
            return dict(row)

        await self.db.execute(
            "INSERT INTO users (user_id, name, username) VALUES (?, ?, ?)",
            (user_id, name, username or "")
        )
        await self.db.commit()

        async with self.db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            user = dict(await cur.fetchone())

        # MongoDB sync
        self._mongo_fire(self.mdb.users.update_one(
            {"user_id": user_id},
            {"$setOnInsert": user},
            upsert=True
        ) if self.mdb else asyncio.sleep(0))

        return user

    async def deduct_balance(self, user_id: int, amount: float):
        await self.db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id)
        )
        await self.db.commit()
        self._mongo_fire(self.mdb.users.update_one(
            {"user_id": user_id}, {"$inc": {"balance": -amount}}
        ) if self.mdb else asyncio.sleep(0))

    async def admin_update_balance(self, user_id: int, amount: float):
        await self.db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id)
        )
        await self.db.commit()
        self._mongo_fire(self.mdb.users.update_one(
            {"user_id": user_id}, {"$inc": {"balance": amount}}
        ) if self.mdb else asyncio.sleep(0))
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
            return [dict(r) for r in await cur.fetchall()]

    # ─── Orders ───────────────────────────────────────────────────────────────

    async def create_order(self, user_id, smm_order_id, service_id, service_name, link, quantity, cost):
        await self.db.execute(
            """INSERT INTO orders (user_id, smm_order_id, service_id, service_name, link, quantity, cost)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, smm_order_id, service_id, service_name, link, quantity, cost)
        )
        await self.db.commit()

        doc = {"user_id": user_id, "smm_order_id": str(smm_order_id), "service_id": str(service_id),
               "service_name": service_name, "link": link, "quantity": quantity, "cost": cost,
               "status": "pending", "start_count": 0, "remains": 0}
        self._mongo_fire(self.mdb.orders.update_one(
            {"smm_order_id": str(smm_order_id)}, {"$setOnInsert": doc}, upsert=True
        ) if self.mdb else asyncio.sleep(0))

    async def get_user_orders(self, user_id: int, limit=10) -> list:
        async with self.db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def update_order_status(self, smm_order_id: str, status: str, start_count: int, remains: int):
        await self.db.execute(
            "UPDATE orders SET status=?, start_count=?, remains=? WHERE smm_order_id=?",
            (status, start_count, remains, smm_order_id)
        )
        await self.db.commit()
        self._mongo_fire(self.mdb.orders.update_one(
            {"smm_order_id": smm_order_id},
            {"$set": {"status": status, "start_count": start_count, "remains": remains}}
        ) if self.mdb else asyncio.sleep(0))

    async def count_user_orders(self, user_id: int) -> int:
        async with self.db.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (user_id,)) as cur:
            return (await cur.fetchone())[0]

    async def total_spent(self, user_id: int) -> float:
        async with self.db.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM orders WHERE user_id = ?", (user_id,)
        ) as cur:
            return (await cur.fetchone())[0]

    async def get_recent_orders(self, limit=10) -> list:
        async with self.db.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ─── Recharges ────────────────────────────────────────────────────────────

    async def create_recharge(self, user_id, order_id, amount, charged, method):
        await self.db.execute(
            "INSERT OR IGNORE INTO recharges (user_id, order_id, amount, charged, method) VALUES (?,?,?,?,?)",
            (user_id, order_id, amount, charged, method)
        )
        await self.db.commit()
        doc = {"user_id": user_id, "order_id": order_id, "amount": amount,
               "charged": charged, "method": method, "status": "pending"}
        self._mongo_fire(self.mdb.recharges.update_one(
            {"order_id": order_id}, {"$setOnInsert": doc}, upsert=True
        ) if self.mdb else asyncio.sleep(0))

    async def get_recharge(self, order_id: str) -> dict:
        async with self.db.execute("SELECT * FROM recharges WHERE order_id = ?", (order_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def complete_recharge(self, order_id: str, user_id: int, amount: float):
        await self.db.execute("UPDATE recharges SET status='completed' WHERE order_id=?", (order_id,))
        await self.db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await self.db.commit()
        self._mongo_fire(self.mdb.recharges.update_one(
            {"order_id": order_id}, {"$set": {"status": "completed"}}
        ) if self.mdb else asyncio.sleep(0))
        self._mongo_fire(self.mdb.users.update_one(
            {"user_id": user_id}, {"$inc": {"balance": amount}}
        ) if self.mdb else asyncio.sleep(0))

    async def reject_recharge(self, order_id: str):
        await self.db.execute("UPDATE recharges SET status='rejected' WHERE order_id=?", (order_id,))
        await self.db.commit()
        self._mongo_fire(self.mdb.recharges.update_one(
            {"order_id": order_id}, {"$set": {"status": "rejected"}}
        ) if self.mdb else asyncio.sleep(0))

    # ─── Stats ────────────────────────────────────────────────────────────────

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
