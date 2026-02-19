import os
import re
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("saleohota")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "15"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in Railway Variables.")

DB_PATH = "data.sqlite3"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


@dataclass
class Product:
    marketplace: str  # wb | ozon
    product_id: str   # wb nmId | ozon sku (best-effort)
    url: str
    title: str
    price_rub: int


# ---------- DB ----------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  marketplace TEXT NOT NULL,
  product_id TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  target_price INTEGER NOT NULL,
  last_price INTEGER,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_watches_user ON watches(user_id);
CREATE INDEX IF NOT EXISTS idx_watches_item ON watches(marketplace, product_id);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

async def add_watch(user_id: int, p: Product, target_price: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO watches (user_id, marketplace, product_id, url, title, target_price, last_price) VALUES (?,?,?,?,?,?,?)",
            (user_id, p.marketplace, p.product_id, p.url, p.title, target_price, p.price_rub)
        )
        await db.commit()

async def list_watches(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, marketplace, title, target_price, last_price, url FROM watches WHERE user_id=? ORDER BY id DESC",
            (user_id,)
        )
        rows = await cur.fetchall()
        return rows

async def delete_watch(user_id: int, watch_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM watches WHERE user_id=? AND id=?", (user_id, watch_id))
        await db.commit()
        return cur.rowcount > 0

async def all_watches():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, user_id, marketplace, product_id, url, title, target_price, last_price FROM watches"
        )
        return await cur.fetchall()

async def update_last_price(watch_id: int, new_price: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE watches SET last_price=? WHERE id=?", (new_price, watch_id))
        await db.commit()


# ---------- Parsers ----------
def normalize_input(text: str) -> str:
    return text.strip()

def detect_marketplace_and_id(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (marketplace, product_id, url_guess)
    marketplace: 'wb' or 'ozon'
    product_id: wb nmId (digits) or ozon sku (digits best-effort)
    """
    t = normalize_input(text)

    # WB by link or digits
    if "wildberries.ru" in t:
        m = re.search(r"/catalog/(\d+)/", t)
        if m:
            nm = m.group(1)
            return "wb", nm, t
        # sometimes query contains nm=...
        m = re.search(r"nm=(\d+)", t)
        if m:
            nm = m.group(1)
            return "wb", nm, t

    if re.fullmatch(r"\d{6,12}", t):
        # ambiguous: could be wb nmId or ozon sku; we'll ask later? For MVP: assume WB if not told.
        # We'll treat as WB by default, but user can prefix "ozon 123"
        return "wb", t, f"https://www.wildberries.ru/catalog/{t}/detail.aspx"

    # Ozon by link
    if "ozon.ru" in t:
        # Try to extract SKU from url like .../product/...-123456789/
        m = re.search(r"-(\d{6,12})/?(\?|$)", t)
        if m:
            sku = m.group(1)
            return "ozon", sku, t
        return "ozon", "unknown", t  # best-effort

    # Ozon by "ozon 123456"
    m = re.match(r"ozon\s+(\d{6,12})", t, re.IGNORECASE)
    if m:
        sku = m.group(1)
        return "ozon", sku, f"https://www.ozon.ru/product/{sku}/"

    # WB by "wb 123"
    m = re.match(r"wb\s+(\d{6,12})", t, re.IGNORECASE)
    if m:
        nm = m.group(1)
        return "wb", nm, f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"

    return None, None, None


async def fetch_wb(session: aiohttp.ClientSession, nm_id: str) -> Product:
    # WB public JSON endpoint
    url = f"https://card.wb.ru/cards/v1/detail?appType=1&curr=rub&dest=-1257786&spp=0&nm={nm_id}"
    async with session.get(url, headers={"User-Agent": UA}) as r:
        r.raise_for_status()
        data = await r.json(content_type=None)

    products = data.get("data", {}).get("products", [])
    if not products:
        raise ValueError("WB: product not found")

    p = products[0]
    name = p.get("name") or f"WB {nm_id}"
    # Prices often in kopecks *? On WB: salePriceU / priceU are in "cents" (руб*100)
    price_u = p.get("salePriceU") or p.get("priceU") or 0
    price_rub = int(price_u // 100) if isinstance(price_u, int) else 0

    link = f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"
    return Product(marketplace="wb", product_id=nm_id, url=link, title=name, price_rub=price_rub)


async def fetch_ozon(session: aiohttp.ClientSession, url: str, sku_hint: str) -> Product:
    """
    Best-effort Ozon parsing:
    - loads HTML and extracts JSON-like price and title using regex.
    This may break if Ozon changes markup. Good enough for MVP.
    """
    async with session.get(url, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}) as r:
        r.raise_for_status()
        html = await r.text()

    # Title: try og:title
    title = None
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    if m:
        title = m.group(1).strip()

    # Price: try og:price:amount
    price = None
    m = re.search(r'<meta property="product:price:amount" content="([^"]+)"', html)
    if m:
        raw = m.group(1).strip().replace(" ", "").replace(",", ".")
        try:
            price = int(float(raw))
        except:
            price = None

    # fallback: search common json keys
    if price is None:
        # look for "finalPrice" or "price"
        m = re.search(r'"finalPrice"\s*:\s*{"value"\s*:\s*(\d+)', html)
        if m:
            price = int(m.group(1))
        else:
            m = re.search(r'"price"\s*:\s*{"value"\s*:\s*(\d+)', html)
            if m:
                price = int(m.group(1))

    if not title:
        title = f"Ozon {sku_hint if sku_hint != 'unknown' else ''}".strip()

    if price is None:
        raise ValueError("Ozon: could not parse price (site may have changed)")

    product_id = sku_hint if sku_hint and sku_hint != "unknown" else "unknown"
    return Product(marketplace="ozon", product_id=product_id, url=url, title=title, price_rub=price)


async def resolve_product(text: str) -> Product:
    marketplace, pid, url = detect_marketplace_and_id(text)
    if not marketplace:
        raise ValueError("Не понял ссылку/артикул. Пришли ссылку WB/Ozon или артикул. Для Ozon можно: `ozon 123456789`")

    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if marketplace == "wb":
            return await fetch_wb(session, pid)
        else:
            # ozon
            return await fetch_ozon(session, url, pid)


# ---------- Bot UI ----------
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="add")],
        [InlineKeyboardButton(text="📋 Мои отслеживания", callback_data="list")],
    ])

def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
    ])

dp = Dispatcher()
bot = Bot(BOT_TOKEN, parse_mode="HTML")

# Simple in-memory state: user_id -> awaiting ("item" or "price") and temp product
USER_STATE = {}
USER_TEMP = {}


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Привет! Я <b>ОхотаНаСкидки</b> 🦆\n\n"
        "Я слежу за ценами на <b>Wildberries</b> и <b>Ozon</b> и уведомляю, когда цена станет <= твоей.\n\n"
        "Нажми кнопку ниже:",
        reply_markup=main_menu()
    )

@dp.callback_query(F.data == "home")
async def home(c: CallbackQuery):
    await c.message.edit_text("Выбери действие:", reply_markup=main_menu())
    await c.answer()

@dp.callback_query(F.data == "add")
async def add(c: CallbackQuery):
    USER_STATE[c.from_user.id] = "item"
    await c.message.edit_text(
        "Ок! Пришли <b>ссылку</b> на товар WB/Ozon или <b>артикул</b>.\n\n"
        "Примеры:\n"
        "• https://www.wildberries.ru/catalog/123456/detail.aspx\n"
        "• 123456 (WB)\n"
        "• https://www.ozon.ru/product/.....-123456789/\n"
        "• ozon 123456789",
        reply_markup=back_menu()
    )
    await c.answer()

@dp.callback_query(F.data == "list")
async def list_my(c: CallbackQuery):
    rows = await list_watches(c.from_user.id)
    if not rows:
        await c.message.edit_text("Пока нет отслеживаний. Добавим?", reply_markup=main_menu())
        await c.answer()
        return

    lines = ["<b>Твои отслеживания:</b>\n"]
    kb = []
    for (wid, mp, title, target, last, url) in rows[:30]:
        last_txt = f"{last}₽" if last is not None else "—"
        mp_txt = "WB" if mp == "wb" else "Ozon"
        lines.append(f"#{wid} • <b>{mp_txt}</b> • {title}\nцель: <b>{target}₽</b> • последняя: <b>{last_txt}</b>\n{url}\n")
        kb.append([InlineKeyboardButton(text=f"🗑 Удалить #{wid}", callback_data=f"del:{wid}")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="home")])

    await c.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await c.answer()

@dp.callback_query(F.data.startswith("del:"))
async def del_watch(c: CallbackQuery):
    wid = int(c.data.split(":")[1])
    ok = await delete_watch(c.from_user.id, wid)
    await c.answer("Удалено" if ok else "Не найдено", show_alert=False)
    # refresh
    await list_my(c)

@dp.message()
async def on_text(m: Message):
    uid = m.from_user.id
    state = USER_STATE.get(uid)

    if state == "item":
        text = m.text or ""
        try:
            p = await resolve_product(text)
        except Exception as e:
            await m.answer(f"Не получилось распознать товар 😕\nПричина: <code>{e}</code>\n\nПопробуй ещё раз или пришли ссылку.")
            return

        USER_TEMP[uid] = p
        USER_STATE[uid] = "price"
        await m.answer(
            f"Нашёл товар:\n<b>{p.title}</b>\nТекущая цена: <b>{p.price_rub}₽</b>\n\n"
            f"Теперь напиши цену, при которой уведомить (например: 4990):",
            reply_markup=back_menu()
        )
        return

    if state == "price":
        raw = (m.text or "").strip().replace(" ", "")
        if not raw.isdigit():
            await m.answer("Напиши число (например 4990).")
            return

        target = int(raw)
        p: Product = USER_TEMP.get(uid)
        if not p:
            USER_STATE.pop(uid, None)
            await m.answer("Что-то пошло не так. Давай заново: /start")
            return

        await add_watch(uid, p, target)
        USER_STATE.pop(uid, None)
        USER_TEMP.pop(uid, None)

        await m.answer(
            f"Готово ✅\nЯ слежу за:\n<b>{p.title}</b>\n"
            f"Уведомлю, когда цена станет ≤ <b>{target}₽</b>.\n\n"
            f"Посмотреть список: нажми <b>Мои отслеживания</b>",
            reply_markup=main_menu()
        )
        return

    # default help
    await m.answer("Нажми кнопку в меню ниже 👇", reply_markup=main_menu())


# ---------- Scheduler ----------
async def check_prices():
    rows = await all_watches()
    if not rows:
        return

    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for (wid, user_id, mp, pid, url, title, target, last_price) in rows:
            try:
                if mp == "wb":
                    p = await fetch_wb(session, pid)
                else:
                    p = await fetch_ozon(session, url, pid)
                new_price = p.price_rub
            except Exception as e:
                log.warning("Check failed wid=%s mp=%s err=%s", wid, mp, e)
                continue

            if last_price is None or new_price != last_price:
                await update_last_price(wid, new_price)

            if new_price <= int(target) and (last_price is None or last_price > int(target)):
                # notify only on crossing threshold downward
                try:
                    await bot.send_message(
                        user_id,
                        f"🔥 Цена снизилась!\n<b>{title}</b>\n"
                        f"Теперь: <b>{new_price}₽</b> (цель: <b>{target}₽</b>)\n{url}"
                    )
                except Exception as e:
                    log.warning("Notify failed user=%s err=%s", user_id, e)


async def main():
    await db_init()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_prices, "interval", minutes=CHECK_INTERVAL_MINUTES, max_instances=1)
    scheduler.start()

    log.info("Bot started. Interval=%s minutes", CHECK_INTERVAL_MINUTES)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
