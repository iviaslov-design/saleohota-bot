import os
import re
import asyncio
import logging
from datetime import datetime
from typing import Optional, Tuple

import aiohttp
import aiosqlite
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ----------------------------
# CONFIG
# ----------------------------
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
CHECK_INTERVAL_MINUTES = int((os.getenv("CHECK_INTERVAL_MINUTES") or "15").strip())
DB_PATH = (os.getenv("DB_PATH") or "data.sqlite3").strip()
PORT = int((os.getenv("PORT") or "10000").strip())

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in Render Environment Variables.")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("saleohota")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"

# ----------------------------
# WB FETCH (FIX: no card.wb.ru)
# ----------------------------
BASKETS = [f"basket-{i:02d}.wb.ru" for i in range(1, 21)]  # 01..20

def extract_wb_nm(text: str) -> Optional[int]:
    """
    Accepts:
    - 546168907
    - 546168907 (wb)
    - https://www.wildberries.ru/catalog/546168907/detail.aspx
    """
    if not text:
        return None
    t = text.strip()

    m = re.search(r"wildberries\.ru/catalog/(\d+)", t)
    if m:
        return int(m.group(1))

    m = re.search(r"\b(\d{6,12})\b", t)
    if m:
        return int(m.group(1))

    return None

def wb_card_urls(nm: int) -> list[str]:
    vol = nm // 1_000_000
    part = nm // 1_000
    path = f"/vol{vol}/part{part}/{nm}/info/ru/card.json"
    return [f"https://{host}{path}" for host in BASKETS]

async def fetch_wb_card(nm: int, session: aiohttp.ClientSession) -> dict:
    last_err = None
    for url in wb_card_urls(nm):
        try:
            async with session.get(url, headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                last_err = f"{r.status} {url}"
        except Exception as e:
            last_err = str(e)
            continue
    raise RuntimeError(f"WB card not found. Last error: {last_err}")

def wb_extract_title_price(card_json: dict) -> Tuple[str, Optional[int]]:
    title = card_json.get("imt_name") or card_json.get("goods_name") or "Товар WB"
    sale_u = card_json.get("salePriceU")
    price_u = card_json.get("priceU")

    price = None
    if isinstance(sale_u, int):
        price = sale_u // 100
    elif isinstance(price_u, int):
        price = price_u // 100

    return title, price

# ----------------------------
# OZON (stub for now)
# ----------------------------
def extract_ozon_id_or_link(text: str) -> Optional[Tuple[str, str]]:
    """
    Returns ("ozon", id_or_unknown) if looks like ozon.
    Accepts:
    - ozon 123456789
    - https://www.ozon.ru/product/...-123456789/
    """
    if not text:
        return None
    t = text.strip()

    m = re.search(r"(?i)\bozon\s+(\d{6,12})\b", t)
    if m:
        return ("ozon", m.group(1))

    if "ozon.ru" in t.lower():
        m = re.search(r"-([0-9]{6,12})/?(?:\?|$)", t)
        if m:
            return ("ozon", m.group(1))
        return ("ozon", "unknown")

    return None

# ----------------------------
# DB
# ----------------------------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  marketplace TEXT NOT NULL,   -- wb|ozon
  product_id TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  target_price INTEGER NOT NULL,
  last_price INTEGER,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_watches_chat ON watches(chat_id);
CREATE INDEX IF NOT EXISTS idx_watches_user ON watches(user_id);
CREATE INDEX IF NOT EXISTS idx_watches_item ON watches(marketplace, product_id);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

async def add_watch(user_id: int, chat_id: int, marketplace: str, product_id: str, url: str, title: str,
                    target_price: int, last_price: Optional[int]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO watches (user_id, chat_id, marketplace, product_id, url, title, target_price, last_price, active, created_at)
               VALUES (?,?,?,?,?,?,?,?,1,?)""",
            (user_id, chat_id, marketplace, product_id, url, title, target_price, last_price, datetime.utcnow().isoformat())
        )
        await db.commit()

async def list_watches(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT id, marketplace, title, target_price, last_price, active, url
               FROM watches WHERE chat_id=? ORDER BY id DESC""",
            (chat_id,)
        )
        return await cur.fetchall()

async def delete_watch(chat_id: int, watch_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM watches WHERE chat_id=? AND id=?", (chat_id, watch_id))
        await db.commit()
        return cur.rowcount > 0

async def all_active_watches():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT id, user_id, chat_id, marketplace, product_id, url, title, target_price, last_price
               FROM watches WHERE active=1"""
        )
        return await cur.fetchall()

async def update_last_price(watch_id: int, new_price: Optional[int]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE watches SET last_price=? WHERE id=?", (new_price, watch_id))
        await db.commit()

async def deactivate_watch(watch_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE watches SET active=0 WHERE id=?", (watch_id,))
        await db.commit()

# ----------------------------
# BOT + STATE
# ----------------------------
dp = Dispatcher()
bot = Bot(BOT_TOKEN)  # IMPORTANT: no parse_mode=HTML to avoid <id> issues

# pending: user_id -> dict with product data, waiting for target price
PENDING = {}

def parse_price(text: str) -> Optional[int]:
    if not text:
        return None
    t = text.strip().replace(" ", "").replace(".", "")
    if not t.isdigit():
        return None
    v = int(t)
    if v <= 0:
        return None
    return v

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Привет! Я бот «ОхотаНаСкидки» 🦆\n\n"
        "Пришли ссылку или артикул:\n"
        "• Wildberries: 546168907 или ссылка\n"
        "• Ozon: ozon 123456789 или ссылка\n\n"
        "Я спрошу цену, которую хочешь дождаться, и буду мониторить.\n\n"
        "Команды:\n"
        "/list — список подписок\n"
        "/remove 12 — удалить подписку\n"
        "/help — помощь"
    )

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(
        "Как пользоваться:\n"
        "1) Пришли артикул WB или ссылку\n"
        "2) Я покажу текущую цену и попрошу «цель»\n"
        "3) Напиши цену числом (например 4990)\n"
        "4) Я проверяю каждые N минут и уведомляю\n\n"
        "Примеры:\n"
        "• 546168907\n"
        "• 546168907 (wb)\n"
        "• https://www.wildberries.ru/catalog/546168907/detail.aspx\n"
        "• ozon 123456789\n\n"
        "Команды:\n"
        "/list\n"
        "/remove 12"
    )

@dp.message(Command("list"))
async def list_cmd(m: Message):
    rows = await list_watches(m.chat.id)
    if not rows:
        await m.answer("Подписок пока нет. Пришли ссылку WB/Ozon или артикул 🙂")
        return
    lines = ["Твои подписки:"]
    for (wid, mp, title, target, last, active, url) in rows:
        status = "✅" if active == 1 else "⏸"
        last_txt = f"{last} ₽" if last is not None else "—"
        lines.append(f"{status} ID {wid} | {mp.upper()} | цель: {target} ₽ | последняя: {last_txt}\n{title}\n{url}")
    await m.answer("\n\n".join(lines))

@dp.message(Command("remove"))
async def remove_cmd(m: Message):
    parts = (m.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await m.answer("Формат: /remove 12")
        return
    wid = int(parts[1])
    ok = await delete_watch(m.chat.id, wid)
    await m.answer("Удалил ✅" if ok else "Не нашёл такой ID 🤷‍♂️")

@dp.message(F.text)
async def on_text(m: Message):
    text = (m.text or "").strip()
    uid = m.from_user.id

    # If waiting for target price
    if uid in PENDING:
        target = parse_price(text)
        if target is None:
            await m.answer("Напиши цену числом (например 4990).")
            return
        p = PENDING.pop(uid)
        await add_watch(
            user_id=uid,
            chat_id=m.chat.id,
            marketplace=p["marketplace"],
            product_id=p["product_id"],
            url=p["url"],
            title=p["title"],
            target_price=target,
            last_price=p.get("last_price"),
        )
        await m.answer(
            f"✅ Добавил!\n{p['marketplace'].upper()}: {p['title']}\n"
            f"Цель: {target} ₽\nПроверка каждые {CHECK_INTERVAL_MINUTES} минут.\n\n"
            f"/list — посмотреть подписки"
        )
        return

    # 1) WB
    nm = extract_wb_nm(text)
    if nm:
        try:
            async with aiohttp.ClientSession() as session:
                card = await fetch_wb_card(nm, session)
                title, price = wb_extract_title_price(card)

            url = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
            PENDING[uid] = {
                "marketplace": "wb",
                "product_id": str(nm),
                "url": url,
                "title": title,
                "last_price": price,
            }

            if price is not None:
                await m.answer(
                    f"✅ WB товар найден:\n{title}\nТекущая цена: {price} ₽\n{url}\n\n"
                    f"Теперь напиши цену, которую хочешь дождаться (например 4990)."
                )
            else:
                await m.answer(
                    f"✅ WB товар найден:\n{title}\n{url}\n\n"
                    f"Теперь напиши цену, которую хочешь дождаться (например 4990)."
                )
        except Exception as e:
            await m.answer(f"Не получилось получить WB товар 😕\nПричина: {e}\nПришли артикул/ссылку ещё раз.")
        return

    # 2) OZON (save only)
    oz = extract_ozon_id_or_link(text)
    if oz:
        mp, ozid = oz
        url = text if "ozon.ru" in text.lower() else f"ozon {ozid}"
        title = f"Ozon товар {ozid}"

        PENDING[uid] = {
            "marketplace": "ozon",
            "product_id": ozid,
            "url": url,
            "title": title,
            "last_price": None
        }
        await m.answer(
            f"✅ Ozon распознал: {ozid}\n"
            "Сейчас мониторинг цены включён для WB.\n"
            "Ozon подключим следующим шагом.\n\n"
            "Напиши цену, которую хочешь дождаться (например 4990)."
        )
        return

    await m.answer("Не понял 😕 Пришли ссылку WB/Ozon или артикул. /help")

# ----------------------------
# SCHEDULER
# ----------------------------
async def check_prices():
    rows = await all_active_watches()
    if not rows:
        return

    async with aiohttp.ClientSession() as session:
        for (wid, user_id, chat_id, mp, pid, url, title, target, last_price) in rows:
            try:
                if mp == "wb":
                    nm = int(pid)
                    card = await fetch_wb_card(nm, session)
                    new_title, new_price = wb_extract_title_price(card)
                    if new_title:
                        title = new_title
                    if new_price is None:
                        continue

                    await update_last_price(wid, new_price)

                    # notify when crossed target downward
                    if new_price <= int(target):
                        await bot.send_message(
                            chat_id,
                            f"🔥 Цена достигнута!\n{title}\n"
                            f"Сейчас: {new_price} ₽ (цель: {target} ₽)\n{url}\n\n"
                            f"Подписка отключена (ID {wid})."
                        )
                        await deactivate_watch(wid)

                # OZON пока пропускаем
                else:
                    continue

            except Exception as e:
                log.warning(f"check failed id={wid} mp={mp} err={e}")

# ----------------------------
# HEALTH SERVER (Render Web Service needs open port)
# ----------------------------
async def start_http_server():
    app = web.Application()

    async def health(_request):
        return web.json_response({"ok": True})

    async def root(_request):
        return web.Response(text="OK")

    app.router.add_get("/", root)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"HTTP server listening on :{PORT}")

# ----------------------------
# ENTRYPOINT
# ----------------------------
async def main():
    await db_init()

    # Start HTTP server so Render sees an open port
    await start_http_server()

    # Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_prices, "interval", minutes=CHECK_INTERVAL_MINUTES, max_instances=1)
    scheduler.start()

    log.info(f"Bot started. Interval={CHECK_INTERVAL_MINUTES} minutes")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
