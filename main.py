import os
import re
import asyncio
import logging
from datetime import datetime
from typing import Optional, Tuple

import aiohttp
import aiosqlite

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# ----------------------------
# Config
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "15").strip() or "15")
DB_PATH = os.getenv("DB_PATH", "bot.db").strip() or "bot.db"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment variables")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("saleohota-bot")

# ----------------------------
# WB helpers (FIX: no card.wb.ru)
# ----------------------------
BASKETS = [f"basket-{i:02d}.wb.ru" for i in range(1, 21)]  # 01..20 обычно хватает


def extract_wb_nm(text: str) -> Optional[int]:
    """
    Accepts:
    - "546168907"
    - "546168907 (wb)"
    - WB link: https://www.wildberries.ru/catalog/546168907/detail.aspx...
    """
    if not text:
        return None

    # WB link
    m = re.search(r"wildberries\.ru/catalog/(\d+)", text)
    if m:
        return int(m.group(1))

    # first long digit sequence
    m = re.search(r"\b(\d{6,12})\b", text)
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
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                last_err = f"{r.status} {url}"
        except Exception as e:
            last_err = str(e)
            continue
    raise RuntimeError(f"WB card not found. Last error: {last_err}")


def wb_extract_title_price(card_json: dict) -> Tuple[str, Optional[int]]:
    """
    priceU/salePriceU are usually in kopecks
    """
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
# OZON helpers (parsing only for now)
# ----------------------------
def extract_ozon_id(text: str) -> Optional[str]:
    """
    Accepts:
    - "ozon 123456789"
    - ozon link containing product id at the end like ...-123456789/
    """
    if not text:
        return None

    t = text.strip().lower()

    m = re.search(r"\bozon\s+(\d{6,12})\b", t)
    if m:
        return m.group(1)

    # common ozon product link patterns: ...-123456789/ or .../product/...-123456789/
    m = re.search(r"-([0-9]{6,12})/?(?:\?|$)", t)
    if m and "ozon" in t:
        return m.group(1)

    return None


# ----------------------------
# DB
# ----------------------------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    marketplace TEXT NOT NULL,         -- "wb" | "ozon"
    product_id TEXT NOT NULL,          -- nm for wb, id for ozon
    title TEXT,
    url TEXT,
    target_price INTEGER NOT NULL,
    last_price INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
"""


async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()


async def db_add_subscription(
    user_id: int,
    chat_id: int,
    marketplace: str,
    product_id: str,
    title: str,
    url: str,
    target_price: int,
    last_price: Optional[int],
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO subscriptions (user_id, chat_id, marketplace, product_id, title, url, target_price, last_price, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                user_id,
                chat_id,
                marketplace,
                product_id,
                title,
                url,
                target_price,
                last_price,
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()


async def db_list_subscriptions(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, marketplace, product_id, title, target_price, last_price, active, url
            FROM subscriptions
            WHERE chat_id = ?
            ORDER BY id DESC
            """,
            (chat_id,),
        )
        rows = await cur.fetchall()
        return rows


async def db_remove_subscription(chat_id: int, sub_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM subscriptions WHERE chat_id = ? AND id = ?",
            (chat_id, sub_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def db_get_active():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM subscriptions WHERE active = 1
            """
        )
        return await cur.fetchall()


async def db_update_last_price(sub_id: int, last_price: Optional[int]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subscriptions SET last_price = ? WHERE id = ?",
            (last_price, sub_id),
        )
        await db.commit()


async def db_deactivate(sub_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE subscriptions SET active = 0 WHERE id = ?", (sub_id,))
        await db.commit()


# ----------------------------
# Bot state (waiting for target price)
# ----------------------------
PENDING = {}  # user_id -> dict(marketplace, product_id, title, url, last_price)


def is_price_message(text: str) -> Optional[int]:
    """
    Accepts: 5000 or 5 000 or 5.000
    """
    if not text:
        return None
    t = text.strip().replace(" ", "").replace(".", "")
    if not t.isdigit():
        return None
    price = int(t)
    if price <= 0:
        return None
    return price


# ----------------------------
# Price checking
# ----------------------------
async def fetch_current_price(marketplace: str, product_id: str) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """
    Returns: (price, title, url)
    """
    if marketplace == "wb":
        nm = int(product_id)
        url = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
            card = await fetch_wb_card(nm, session)
            title, price = wb_extract_title_price(card)
            return price, title, url

    # OZON: parsing saved, fetching disabled (yet)
    if marketplace == "ozon":
        return None, None, None

    return None, None, None


async def check_prices(bot: Bot):
    rows = await db_get_active()
    if not rows:
        return

    for r in rows:
        sub_id = r["id"]
        chat_id = r["chat_id"]
        marketplace = r["marketplace"]
        product_id = r["product_id"]
        target_price = r["target_price"]
        old_last = r["last_price"]
        url = r["url"] or ""
        title = r["title"] or ""

        try:
            price, new_title, new_url = await fetch_current_price(marketplace, product_id)

            # OZON пока пропускаем
            if marketplace == "ozon":
                # чтобы не спамить каждую проверку — ничего не шлём
                continue

            if new_title:
                title = new_title
            if new_url:
                url = new_url

            await db_update_last_price(sub_id, price)

            if price is None:
                continue

            # Notify if reached
            if price <= target_price:
                await bot.send_message(
                    chat_id,
                    f"🎯 Цена достигнута!\n"
                    f"{'WB' if marketplace=='wb' else marketplace.upper()}: {title}\n"
                    f"Текущая цена: {price} ₽ (цель: {target_price} ₽)\n"
                    f"{url}\n\n"
                    f"Подписка отключена (ID {sub_id}).",
                )
                await db_deactivate(sub_id)
            else:
                # Optional: you can notify on big drop; currently silent
                pass

        except Exception as e:
            logger.warning(f"Check failed sub_id={sub_id}: {e}")


# ----------------------------
# Health server (Render friendly)
# ----------------------------
async def start_health_server():
    app = web.Application()

    async def health(_):
        return web.json_response({"ok": True})

    async def root(_):
        return web.Response(text="ok")

    app.router.add_get("/health", health)
    app.router.add_get("/", root)

    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server started on 0.0.0.0:{port}")


# ----------------------------
# Bot handlers
# ----------------------------
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я бот «ОхотаНаСкидки» 🦆\n\n"
        "Пришли:\n"
        "• ссылку WB или артикул (например: 546168907)\n"
        "• или Ozon: `ozon 123456789`\n\n"
        "Я спрошу цену, которую ты хочешь дождаться, и буду мониторить.\n\n"
        "Команды:\n"
        "/list — список подписок\n"
        "/remove <id> — удалить подписку\n"
        "/help — помощь"
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "Как пользоваться:\n"
        "1) Пришли ссылку WB или артикул\n"
        "2) В ответ пришли желаемую цену (числом)\n"
        "3) Я буду проверять цену каждые N минут\n\n"
        "Примеры:\n"
        "• 546168907\n"
        "• 546168907 (wb)\n"
        "• https://www.wildberries.ru/catalog/546168907/detail.aspx\n\n"
        "Ozon пока сохраняю, мониторинг подключим следующим шагом.\n\n"
        "/list\n"
        "/remove 12"
    )


@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    rows = await db_list_subscriptions(message.chat.id)
    if not rows:
        await message.answer("Подписок пока нет. Пришли ссылку WB/Ozon или артикул 🙂")
        return

    lines = ["📌 Твои подписки:"]
    for r in rows:
        status = "✅" if r["active"] == 1 else "⏸"
        mp = r["marketplace"].upper()
        title = (r["title"] or "").strip()
        if not title:
            title = f"{mp} {r['product_id']}"
        lp = r["last_price"]
        lp_txt = f"{lp} ₽" if lp is not None else "—"
        lines.append(
            f"{status} ID {r['id']} | {mp} | цель: {r['target_price']} ₽ | последняя: {lp_txt}\n{title}"
        )
    await message.answer("\n\n".join(lines))


@dp.message(Command("remove"))
async def cmd_remove(message: types.Message):
    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: /remove <id>\nПример: /remove 12")
        return
    sub_id = int(parts[1])
    ok = await db_remove_subscription(message.chat.id, sub_id)
    await message.answer("Удалил ✅" if ok else "Не нашёл подписку с таким ID 🤷‍♂️")


@dp.message()
async def on_message(message: types.Message):
    text = (message.text or "").strip()
    if not text:
        return

    user_id = message.from_user.id

    # 1) If user is replying with target price
    if user_id in PENDING:
        price = is_price_message(text)
        if price is None:
            await message.answer("Напиши цену числом, например: 4990")
            return

        pending = PENDING.pop(user_id)
        await db_add_subscription(
            user_id=user_id,
            chat_id=message.chat.id,
            marketplace=pending["marketplace"],
            product_id=pending["product_id"],
            title=pending.get("title", ""),
            url=pending.get("url", ""),
            target_price=price,
            last_price=pending.get("last_price"),
        )

        await message.answer(
            f"✅ Добавил в отслеживание!\n"
            f"{pending['marketplace'].upper()}: {pending.get('title','')}\n"
            f"Цель: {price} ₽\n"
            f"Проверка каждые {CHECK_INTERVAL_MINUTES} мин.\n\n"
            f"/list — посмотреть подписки"
        )
        return

    # 2) WB: parse + fetch card (FIX)
    nm = extract_wb_nm(text)
    if nm:
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
                card = await fetch_wb_card(nm, session)
                title, current_price = wb_extract_title_price(card)
                url = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"

            PENDING[user_id] = {
                "marketplace": "wb",
                "product_id": str(nm),
                "title": title,
                "url": url,
                "last_price": current_price,
            }

            if current_price is not None:
                await message.answer(
                    f"✅ WB товар найден:\n{title}\n"
                    f"Текущая цена: {current_price} ₽\n{url}\n\n"
                    f"Теперь напиши цену, которую хочешь дождаться (например: 4990)."
                )
            else:
                await message.answer(
                    f"✅ WB товар найден:\n{title}\n{url}\n\n"
                    f"Теперь напиши цену, которую хочешь дождаться (например: 4990)."
                )
        except Exception as e:
            await message.answer(
                "Не получилось распознать товар 😕\n"
                f"Причина: {e}\n\n"
                "Пришли ссылку WB или артикул."
            )
        return

    # 3) OZON: parse and store (monitoring to be added later)
    ozon_id = extract_ozon_id(text)
    if ozon_id:
        PENDING[user_id] = {
            "marketplace": "ozon",
            "product_id": ozon_id,
            "title": f"Ozon товар {ozon_id}",
            "url": text if "ozon" in text.lower() else f"ozon {ozon_id}",
            "last_price": None,
        }
        await message.answer(
            f"✅ Ozon распознал (ID {ozon_id}).\n"
            "Сейчас у меня включён мониторинг цен WB.\n"
            "Ozon подключим следующим шагом.\n\n"
            "Напиши цену, которую хочешь дождаться (например: 4990)."
        )
        return

    # 4) fallback
    await message.answer(
        "Не понял ссылку/артикул 🤷‍♂️\n\n"
        "Пришли:\n"
        "• ссылку WB или артикул (например: 546168907)\n"
        "• или Ozon: `ozon 123456789`\n\n"
        "Команды: /help /list /remove <id>"
    )


# ----------------------------
# Entrypoint
# ----------------------------
async def main():
    await db_init()

    bot = Bot(token=BOT_TOKEN)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(lambda: asyncio.create_task(check_prices(bot)), "interval", minutes=CHECK_INTERVAL_MINUTES)
    scheduler.start()

    # Health server for Render
    await start_health_server()

    logger.info("Bot started.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
