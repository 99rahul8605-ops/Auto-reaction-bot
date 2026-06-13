import asyncio
import logging
import time
import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from config import config
from database import db
from cashfree_utils import create_payment_order

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ─── States ───────────────────────────────────────────────────────────────────

class OrderStates(StatesGroup):
    waiting_service_id = State()
    waiting_link = State()
    waiting_quantity = State()
    confirming = State()

class RechargeStates(StatesGroup):
    waiting_amount = State()
    waiting_upi_screenshot = State()

class AdminStates(StatesGroup):
    waiting_addbal = State()
    waiting_broadcast = State()

# ─── Service Cache ─────────────────────────────────────────────────────────────

_all_services: list = []
_tg_categorized: dict = {}
_cache_time: float = 0

TARGET_CATEGORIES = [
    "Telegram: Post Reactions [Fast]",
    "Telegram: Post Reactions [Cheap]",
]

def get_markup_by_cat(cat: str) -> float:
    return config.DEFAULT_MARKUP

async def fetch_services():
    global _all_services, _tg_categorized, _cache_time
    if time.time() - _cache_time < 3600 and _tg_categorized:
        return True
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(config.SMM_API_URL, data={
                "key": config.SMM_API_KEY, "action": "services"
            }) as r:
                data = await r.json(content_type=None)
        if not isinstance(data, list): return False
        _all_services = data

        # Only show TARGET_CATEGORIES services
        _tg_categorized = {}
        for cat in TARGET_CATEGORIES:
            filtered = [s for s in data if s.get("category", "") == cat]
            if filtered:
                _tg_categorized[cat] = filtered
        _cache_time = time.time()
        logger.info(f"Fast Reactions loaded: {len(filtered)} services")
        return True
    except Exception as e:
        logger.error(f"fetch_services error: {e}")
        return False

# ─── USD/INR ──────────────────────────────────────────────────────────────────

_usd_inr: float = 95.0
_usd_fetched: float = 0

async def get_usd_inr() -> float:
    global _usd_inr, _usd_fetched
    if time.time() - _usd_fetched < 3600:
        return _usd_inr
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://open.er-api.com/v6/latest/USD",
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                d = await r.json()
                if d.get("result") == "success":
                    _usd_inr = float(d["rates"]["INR"])
                    _usd_fetched = time.time()
                    logger.info(f"USD/INR: {_usd_inr}")
    except Exception as e:
        logger.warning(f"USD/INR fetch failed, using {_usd_inr}: {e}")
    return _usd_inr

def get_markup(svc: dict) -> float:
    return get_markup_by_cat(svc.get("category", ""))

def cf_charge(amount: float) -> float:
    return round(amount * 1.02, 2)

# ─── Keyboard ─────────────────────────────────────────────────────────────────

def main_kb(user_id: int):
    rows = [
        [KeyboardButton(text="📋 Services"),   KeyboardButton(text="🛒 New Order")],
        [KeyboardButton(text="📦 My Orders"),  KeyboardButton(text="💰 Balance")],
        [KeyboardButton(text="➕ Add Funds"),  KeyboardButton(text="👤 Profile")],
    ]
    if user_id in config.ADMIN_IDS:
        rows.append([KeyboardButton(text="🔧 Admin")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

# ─── /start ───────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message):
    user = await db.get_or_create_user(msg.from_user.id, msg.from_user.full_name, msg.from_user.username)
    await msg.answer(
        f"👋 <b>{config.BOT_NAME}</b>\n\n"
        f"🚀 Telegram SMM — Reactions, Members, Views & more\n\n"
        f"💰 Balance: <b>₹{user['balance']:.2f}</b>",
        parse_mode="HTML", reply_markup=main_kb(msg.from_user.id)
    )

# ─── Services ─────────────────────────────────────────────────────────────────

PER_PAGE = 8

async def build_page(cat_name: str, page: int, usd: float) -> tuple:
    svcs = _tg_categorized[cat_name]
    total = len(svcs)
    tp = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, tp - 1))
    chunk = svcs[page * PER_PAGE:(page + 1) * PER_PAGE]

    text = f"{cat_name} <b>Services</b>  ({page+1}/{tp})\n\n"
    for svc in chunk:
        m = get_markup(svc)
        rate = round(float(svc["rate"]) * usd * m, 4)
        text += (
            f"🆔 <code>{svc['service']}</code> — {svc['name'][:50]}\n"
            f"   💰 ₹{rate}/1k | Min: {svc['min']} Max: {svc['max']}\n\n"
        )

    cat_keys = list(_tg_categorized.keys())
    cidx = cat_keys.index(cat_name)
    rows = []

    # Prev/Next
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"svc_{cidx}_{page-1}"))
    if page < tp - 1:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"svc_{cidx}_{page+1}"))
    if nav: rows.append(nav)

    # Category tabs - short names
    SHORT_NAMES = {
        "Telegram: Post Reactions [Fast]":  "⚡ Fast",
        "Telegram: Post Reactions [Cheap]": "💰 Cheap",
        "Telegram: Post Reactions [S4]":    "🔥 S4",
        "Telegram: Post Reactions [Future NEW]": "🆕 Future",
        "Telegram: Post Reactions [Premium]": "💎 Premium",
        "Telegram: Post Reactions [Private channels]": "🔒 Private",
        "Telegram: Post Reaction [Auto]":   "🤖 Auto",
    }
    tab_row = []
    for i, cat in enumerate(cat_keys):
        short = SHORT_NAMES.get(cat, cat.replace("Telegram: Post Reactions", "").replace("Telegram:", "").strip()[:15])
        label = f"✅ {short}" if i == cidx else short
        tab_row.append(InlineKeyboardButton(text=label, callback_data=f"svc_{i}_0"))
        if len(tab_row) == 2:
            rows.append(tab_row); tab_row = []
    if tab_row: rows.append(tab_row)

    rows.append([InlineKeyboardButton(text="🛒 Place Order", callback_data="go_order")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

@router.message(F.text == "📋 Services")
async def cmd_services(msg: Message):
    wait = await msg.answer("⏳ Loading Telegram services...")
    ok = await fetch_services()
    if not ok or not _tg_categorized:
        await wait.edit_text("❌ Could not load services. Try again later.")
        return
    usd = await get_usd_inr()
    first = list(_tg_categorized.keys())[0]
    text, kb = await build_page(first, 0, usd)
    await wait.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("svc_"))
async def cb_svc_page(cb: CallbackQuery):
    await cb.answer()
    _, cidx, page = cb.data.split("_")
    cidx, page = int(cidx), int(page)
    keys = list(_tg_categorized.keys())
    if cidx >= len(keys):
        await cb.answer("Not found", show_alert=True); return
    usd = await get_usd_inr()
    text, kb = await build_page(keys[cidx], page, usd)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

# ─── Order Flow ───────────────────────────────────────────────────────────────

@router.message(F.text == "🛒 New Order")
async def new_order_msg(msg: Message, state: FSMContext):
    await msg.answer(
        "🛒 Enter <b>Service ID</b>:\n(Browse via 📋 Services)",
        parse_mode="HTML", reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(OrderStates.waiting_service_id)

@router.callback_query(F.data == "go_order")
async def new_order_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.answer(
        "🛒 Enter <b>Service ID</b>:",
        parse_mode="HTML", reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(OrderStates.waiting_service_id)

@router.message(OrderStates.waiting_service_id)
async def order_svc(msg: Message, state: FSMContext):
    sid = msg.text.strip()
    if not sid.isdigit():
        await msg.answer("❌ Enter a valid numeric Service ID"); return
    await fetch_services()
    svc = next((s for s in _all_services if str(s.get("service")) == sid), None)
    if not svc:
        await msg.answer("❌ Service not found."); return
    usd = await get_usd_inr()
    m = get_markup(svc)
    rate = round(float(svc["rate"]) * usd * m, 4)
    await state.update_data(service=svc)
    await msg.answer(
        f"✅ <b>{svc['name']}</b>\n\n"
        f"💰 ₹{rate}/1k\n"
        f"Min: {svc['min']} | Max: {svc['max']}\n\n"
        f"Send <b>link/username</b>:",
        parse_mode="HTML"
    )
    await state.set_state(OrderStates.waiting_link)

@router.message(OrderStates.waiting_link)
async def order_link(msg: Message, state: FSMContext):
    await state.update_data(link=msg.text.strip())
    data = await state.get_data()
    svc = data["service"]
    await msg.answer(f"📊 Enter <b>quantity</b> (Min: {svc['min']} | Max: {svc['max']}):", parse_mode="HTML")
    await state.set_state(OrderStates.waiting_quantity)

@router.message(OrderStates.waiting_quantity)
async def order_qty(msg: Message, state: FSMContext):
    try:
        qty = int(msg.text.strip())
    except ValueError:
        await msg.answer("❌ Enter a valid number"); return
    data = await state.get_data()
    svc = data["service"]
    if qty < int(svc["min"]) or qty > int(svc["max"]):
        await msg.answer(f"❌ Qty must be {svc['min']}–{svc['max']}"); return
    usd = await get_usd_inr()
    cost = round(float(svc["rate"]) * usd * qty * get_markup(svc) / 1000, 2)
    user = await db.get_or_create_user(msg.from_user.id, msg.from_user.full_name, msg.from_user.username)
    await state.update_data(quantity=qty, cost=cost)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm", callback_data="confirm_order")],
        [InlineKeyboardButton(text="❌ Cancel",  callback_data="cancel_order")]
    ])
    bal_ok = user["balance"] >= cost
    await msg.answer(
        f"📋 <b>Order Summary</b>\n\n"
        f"📦 {svc['name']}\n"
        f"🔗 {data['link']}\n"
        f"📊 Qty: {qty:,}\n"
        f"💰 Cost: ₹{cost}\n"
        f"👛 Balance: ₹{user['balance']:.2f}\n\n"
        f"{'✅ Sufficient balance' if bal_ok else '❌ Insufficient balance — add funds first'}",
        parse_mode="HTML", reply_markup=kb
    )
    await state.set_state(OrderStates.confirming)

@router.callback_query(F.data == "confirm_order")
async def confirm_order(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = await db.get_or_create_user(cb.from_user.id, cb.from_user.full_name, cb.from_user.username)
    if user["balance"] < data["cost"]:
        await cb.answer("❌ Insufficient balance!", show_alert=True)
        await state.clear(); return
    await cb.answer("Placing order...")
    async with aiohttp.ClientSession() as s:
        async with s.post(config.SMM_API_URL, data={
            "key": config.SMM_API_KEY, "action": "add",
            "service": data["service"]["service"],
            "link": data["link"], "quantity": data["quantity"]
        }) as r:
            res = await r.json(content_type=None)
    if res.get("order"):
        await db.deduct_balance(cb.from_user.id, data["cost"])
        await db.create_order(cb.from_user.id, res["order"], data["service"]["service"],
                              data["service"]["name"], data["link"], data["quantity"], data["cost"])
        user = await db.get_or_create_user(cb.from_user.id, cb.from_user.full_name, cb.from_user.username)
        await cb.message.edit_text(
            f"✅ <b>Order Placed!</b>\n\n"
            f"🆔 Order ID: <code>{res['order']}</code>\n"
            f"💰 Cost: ₹{data['cost']}\n"
            f"👛 Balance: ₹{user['balance']:.2f}",
            parse_mode="HTML"
        )
        await bot.send_message(cb.from_user.id, "✅ Order placed!", reply_markup=main_kb(cb.from_user.id))
    else:
        await cb.message.edit_text(f"❌ Order failed: {res.get('error', 'Unknown')}")
        await bot.send_message(cb.from_user.id, "❌ Failed.", reply_markup=main_kb(cb.from_user.id))
    await state.clear()

@router.callback_query(F.data == "cancel_order")
async def cancel_order(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Cancelled.")
    await bot.send_message(cb.from_user.id, "Cancelled.", reply_markup=main_kb(cb.from_user.id))

# ─── My Orders ────────────────────────────────────────────────────────────────

@router.message(F.text == "📦 My Orders")
async def my_orders(msg: Message):
    orders = await db.get_user_orders(msg.from_user.id, 10)
    if not orders:
        await msg.answer("📭 No orders yet!"); return
    text = "📦 <b>My Orders</b>\n\n"
    emoji = {"pending":"⏳","processing":"🔄","completed":"✅","partial":"⚠️","cancelled":"❌"}
    for o in orders:
        e = emoji.get(o["status"], "❓")
        text += f"{e} <b>#{o['smm_order_id']}</b> — {o['service_name'][:35]}\n   Qty: {o['quantity']:,} | ₹{o['cost']} | {o['status'].title()}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Refresh Status", callback_data="refresh_orders")]])
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "refresh_orders")
async def refresh_orders(cb: CallbackQuery):
    await cb.answer("Checking...")
    orders = await db.get_user_orders(cb.from_user.id, 5)
    for o in orders:
        if o["status"] not in ["completed", "cancelled"]:
            async with aiohttp.ClientSession() as s:
                async with s.post(config.SMM_API_URL, data={
                    "key": config.SMM_API_KEY, "action": "status", "order": o["smm_order_id"]
                }) as r:
                    res = await r.json(content_type=None)
            if res.get("status"):
                await db.update_order_status(o["smm_order_id"], res["status"].lower(),
                                             res.get("start_count", 0), res.get("remains", 0))
    await my_orders(cb.message)

# ─── Balance & Funds ──────────────────────────────────────────────────────────

@router.message(F.text == "💰 Balance")
async def balance(msg: Message):
    user = await db.get_or_create_user(msg.from_user.id, msg.from_user.full_name, msg.from_user.username)
    await msg.answer(f"💰 Balance: <b>₹{user['balance']:.2f}</b>", parse_mode="HTML")

@router.message(F.text == "➕ Add Funds")
async def add_funds(msg: Message, state: FSMContext):
    await msg.answer(
        "💳 <b>Add Funds</b>\n\nEnter amount (Min ₹10, Max ₹50000):",
        parse_mode="HTML", reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(RechargeStates.waiting_amount)

@router.message(RechargeStates.waiting_amount)
async def process_amount(msg: Message, state: FSMContext):
    try:
        amount = float(msg.text.strip())
        if amount < 10 or amount > 50000:
            await msg.answer("❌ Amount must be ₹10–₹50000"); return
    except ValueError:
        await msg.answer("❌ Invalid amount"); return
    charge = cf_charge(amount)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Card/Net Banking — ₹{charge}", callback_data=f"pay_cf_{amount}")],
        [InlineKeyboardButton(text=f"📱 UPI — ₹{amount} (Manual)",    callback_data=f"pay_upi_{amount}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="pay_cancel")]
    ])
    await msg.answer(
        f"💳 <b>Add ₹{amount:.0f}</b>\n\n"
        f"Card/NB: ₹{charge} <i>(includes 2% gateway fee)</i>\n"
        f"UPI: ₹{amount:.0f} <i>(no fee, manual verify)</i>",
        parse_mode="HTML", reply_markup=kb
    )
    await state.clear()

@router.callback_query(F.data.startswith("pay_cf_"))
async def cashfree_pay(cb: CallbackQuery):
    amount = float(cb.data.split("_")[2])
    charge = cf_charge(amount)
    order_id = f"TG{cb.from_user.id}{int(time.time())}"
    await cb.answer("Creating payment link...")
    user = await db.get_or_create_user(cb.from_user.id, cb.from_user.full_name, cb.from_user.username)
    result = await create_payment_order(order_id, charge, str(cb.from_user.id),
                                        cb.from_user.full_name or "User",
                                        user.get("phone", "9999999999"))
    if result.get("payment_link"):
        await db.create_recharge(cb.from_user.id, order_id, amount, charge, "cashfree")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Pay Now", url=result["payment_link"])],
            [InlineKeyboardButton(text="✅ Verify Payment", callback_data=f"verify_{order_id}")]
        ])
        await cb.message.edit_text(
            f"💳 Pay <b>₹{charge}</b>\nOrder: <code>{order_id}</code>",
            parse_mode="HTML", reply_markup=kb
        )
    else:
        await cb.message.edit_text("❌ Could not create payment link. Try again.")

@router.callback_query(F.data.startswith("pay_upi_"))
async def upi_pay(cb: CallbackQuery, state: FSMContext):
    import qrcode
    import io

    amount = float(cb.data.split("_")[2])
    await state.update_data(upi_amount=amount)
    await state.set_state(RechargeStates.waiting_upi_screenshot)

    # UPI deep link with amount pre-filled
    upi_url = (
        f"upi://pay?pa={config.UPI_ID}"
        f"&pn={config.UPI_NAME.replace(' ', '%20')}"
        f"&am={amount:.2f}"
        f"&cu=INR"
        f"&tn=SMM%20Wallet%20Recharge"
    )

    # QR generate karo
    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    caption = (
        f"📱 <b>UPI Payment</b>\n\n"
        f"💰 Amount: <b>₹{amount:.0f}</b>\n"
        f"🔖 UPI ID: <code>{config.UPI_ID}</code>\n"
        f"👤 Name: {config.UPI_NAME}\n\n"
        f"1️⃣ Scan QR <b>ya</b> UPI ID se pay karo\n"
        f"2️⃣ Screenshot yahan bhejo 👇"
    )

    await cb.message.delete()
    await bot.send_photo(
        cb.from_user.id,
        photo=buf,
        caption=caption,
        parse_mode="HTML"
    )

@router.message(RechargeStates.waiting_upi_screenshot, F.photo)
async def upi_screenshot(msg: Message, state: FSMContext):
    data = await state.get_data()
    amount = data.get("upi_amount", 0)
    await state.clear()
    order_id = f"UPI{msg.from_user.id}{int(time.time())}"
    await db.create_recharge(msg.from_user.id, order_id, amount, amount, "upi_manual")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"upi_approve_{order_id}_{msg.from_user.id}_{amount}"),
        InlineKeyboardButton(text="❌ Reject",  callback_data=f"upi_reject_{order_id}_{msg.from_user.id}")
    ]])
    for aid in config.ADMIN_IDS:
        try:
            await bot.send_photo(aid, photo=msg.photo[-1].file_id,
                caption=f"💰 <b>UPI Request</b>\n👤 {msg.from_user.full_name} (<code>{msg.from_user.id}</code>)\n💵 ₹{amount}\n🔖 <code>{order_id}</code>",
                parse_mode="HTML", reply_markup=kb)
        except Exception: pass
    await msg.answer(
        f"✅ Screenshot received!\nOrder: <code>{order_id}</code>\n⏳ Will be verified soon.",
        parse_mode="HTML", reply_markup=main_kb(msg.from_user.id)
    )

@router.message(RechargeStates.waiting_upi_screenshot)
async def upi_wrong(msg: Message):
    await msg.answer("📸 Please send a <b>screenshot/photo</b> of your payment.", parse_mode="HTML")

@router.callback_query(F.data.startswith("verify_"))
async def verify_cf(cb: CallbackQuery):
    order_id = cb.data.replace("verify_", "")
    await cb.answer("Checking...")
    recharge = await db.get_recharge(order_id)
    if not recharge:
        await cb.answer("❌ Not found", show_alert=True); return
    if recharge["status"] == "completed":
        await cb.answer("✅ Already credited!", show_alert=True); return
    async with aiohttp.ClientSession() as s:
        hdrs = {"x-client-id": config.CASHFREE_APP_ID,
                "x-client-secret": config.CASHFREE_SECRET_KEY, "x-api-version": "2023-08-01"}
        async with s.get(f"{config.CASHFREE_BASE_URL}/orders/{order_id}/payments", headers=hdrs) as r:
            data = await r.json()
    paid = any(p.get("payment_status") == "SUCCESS" for p in (data if isinstance(data, list) else [data]))
    if paid:
        await db.complete_recharge(order_id, recharge["user_id"], recharge["amount"])
        user = await db.get_or_create_user(cb.from_user.id, cb.from_user.full_name, cb.from_user.username)
        await cb.message.edit_text(f"✅ <b>₹{recharge['amount']} added!</b>\nBalance: ₹{user['balance']:.2f}", parse_mode="HTML")
        await bot.send_message(cb.from_user.id, "💰 Wallet recharged!", reply_markup=main_kb(cb.from_user.id))
    else:
        await cb.answer("❌ Payment not confirmed yet.", show_alert=True)

@router.callback_query(F.data == "pay_cancel")
async def pay_cancel(cb: CallbackQuery):
    await cb.message.edit_text("❌ Cancelled.")
    await bot.send_message(cb.from_user.id, "Cancelled.", reply_markup=main_kb(cb.from_user.id))

@router.callback_query(F.data.startswith("upi_approve_"))
async def upi_approve(cb: CallbackQuery):
    if cb.from_user.id not in config.ADMIN_IDS: return
    parts = cb.data.split("_")
    order_id, user_id, amount = parts[2], int(parts[3]), float(parts[4])
    recharge = await db.get_recharge(order_id)
    if not recharge or recharge["status"] == "completed":
        await cb.answer("Already processed!", show_alert=True); return
    await db.complete_recharge(order_id, user_id, amount)
    user = await db.get_or_create_user(user_id, "", "")
    await cb.message.edit_caption(cb.message.caption + f"\n\n✅ Approved by @{cb.from_user.username}", parse_mode="HTML")
    try: await bot.send_message(user_id, f"✅ ₹{amount:.0f} added! Balance: ₹{user['balance']:.2f}")
    except Exception: pass
    await cb.answer("✅ Approved!")

@router.callback_query(F.data.startswith("upi_reject_"))
async def upi_reject(cb: CallbackQuery):
    if cb.from_user.id not in config.ADMIN_IDS: return
    parts = cb.data.split("_")
    order_id, user_id = parts[2], int(parts[3])
    await db.reject_recharge(order_id)
    await cb.message.edit_caption(cb.message.caption + f"\n\n❌ Rejected by @{cb.from_user.username}", parse_mode="HTML")
    try: await bot.send_message(user_id, "❌ UPI payment rejected. Contact support.")
    except Exception: pass
    await cb.answer("❌ Rejected!")

# ─── Profile ──────────────────────────────────────────────────────────────────

@router.message(F.text == "👤 Profile")
async def profile(msg: Message):
    user = await db.get_or_create_user(msg.from_user.id, msg.from_user.full_name, msg.from_user.username)
    orders = await db.count_user_orders(msg.from_user.id)
    spent = await db.total_spent(msg.from_user.id)
    await msg.answer(
        f"👤 <b>Profile</b>\n\n"
        f"🆔 <code>{msg.from_user.id}</code>\n"
        f"💰 Balance: ₹{user['balance']:.2f}\n"
        f"📦 Orders: {orders}\n"
        f"💸 Total Spent: ₹{spent:.2f}",
        parse_mode="HTML"
    )

# ─── Admin ────────────────────────────────────────────────────────────────────

@router.message(F.text == "🔧 Admin")
async def admin_panel(msg: Message):
    if msg.from_user.id not in config.ADMIN_IDS:
        await msg.answer("❌ Access denied."); return
    stats = await db.get_stats()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Balance",  callback_data="adm_addbal"),
         InlineKeyboardButton(text="📢 Broadcast",    callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="👥 Users",        callback_data="adm_users"),
         InlineKeyboardButton(text="📦 Orders",       callback_data="adm_orders")]
    ])
    await msg.answer(
        f"🔧 <b>Admin Panel</b>\n\n"
        f"👥 Users: {stats['users']}\n"
        f"📦 Orders: {stats['orders']}\n"
        f"💰 Revenue: ₹{stats['revenue']:.2f}\n"
        f"📈 Today: {stats['today_orders']}",
        parse_mode="HTML", reply_markup=kb
    )

@router.callback_query(F.data == "adm_addbal")
async def adm_addbal(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in config.ADMIN_IDS: return
    await cb.message.edit_text("Send: <code>USER_ID AMOUNT</code>\nExample: <code>123456 500</code>", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_addbal)

@router.message(AdminStates.waiting_addbal)
async def adm_do_addbal(msg: Message, state: FSMContext):
    if msg.from_user.id not in config.ADMIN_IDS: return
    try:
        uid, amount = int(msg.text.split()[0]), float(msg.text.split()[1])
    except Exception:
        await msg.answer("❌ Format: USER_ID AMOUNT"); return
    user = await db.admin_update_balance(uid, amount)
    if user:
        await msg.answer(f"✅ ₹{amount} added to {uid}\nNew balance: ₹{user['balance']:.2f}", reply_markup=main_kb(msg.from_user.id))
        try: await bot.send_message(uid, f"💰 ₹{amount:+.2f} added. Balance: ₹{user['balance']:.2f}")
        except: pass
    else:
        await msg.answer("❌ User not found")
    await state.clear()

@router.callback_query(F.data == "adm_broadcast")
async def adm_broadcast(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in config.ADMIN_IDS: return
    await cb.message.edit_text("✉️ Send broadcast message (HTML supported):")
    await state.set_state(AdminStates.waiting_broadcast)

@router.message(AdminStates.waiting_broadcast)
async def adm_do_broadcast(msg: Message, state: FSMContext):
    if msg.from_user.id not in config.ADMIN_IDS: return
    users = await db.get_all_user_ids()
    sent = failed = 0
    await msg.answer(f"📢 Broadcasting to {len(users)} users...")
    for uid in users:
        try:
            await bot.send_message(uid, msg.text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except: failed += 1
    await msg.answer(f"✅ Sent: {sent} | Failed: {failed}", reply_markup=main_kb(msg.from_user.id))
    await state.clear()

@router.callback_query(F.data == "adm_users")
async def adm_users(cb: CallbackQuery):
    if cb.from_user.id not in config.ADMIN_IDS: return
    users = await db.get_recent_users(10)
    text = "👥 <b>Recent Users</b>\n\n" + "".join(
        f"• <code>{u['user_id']}</code> — {u['name']} — ₹{u['balance']:.2f}\n" for u in users)
    await cb.message.edit_text(text, parse_mode="HTML")

@router.callback_query(F.data == "adm_orders")
async def adm_orders(cb: CallbackQuery):
    if cb.from_user.id not in config.ADMIN_IDS: return
    orders = await db.get_recent_orders(10)
    text = "📦 <b>Recent Orders</b>\n\n" + "".join(
        f"• #{o['smm_order_id']} | {o['service_name'][:25]} | ₹{o['cost']} | {o['status']}\n" for o in orders)
    await cb.message.edit_text(text, parse_mode="HTML")

# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    await db.init()
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Telegram SMM Bot starting in polling mode...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
