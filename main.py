import asyncio
import hashlib
import hmac
import json
import random
from html import escape
import os
import aiohttp
from aiohttp import web
from datetime import datetime, timedelta
from urllib.parse import parse_qsl
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv
from supabase import create_client, Client
from postgrest.exceptions import APIError

load_dotenv(override=True)

# Жорстко очищаємо URL від будь-яких зайвих хвостів
raw_url = os.getenv("SUPABASE_URL", "")
SUPABASE_URL = raw_url.split("/rest/v1")[0].rstrip("/")

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
if MINI_APP_URL and not MINI_APP_URL.startswith(("https://", "http://")):
    MINI_APP_URL = f"https://{MINI_APP_URL}"
WEB_PORT = int(os.getenv("PORT", "8080"))

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
daily_bonus_column_available = True
daily_bonus_claims = {}
daily_streak_fallback = {}
mini_blackjack_games = {}

CHESS_HEADERS = {"User-Agent": "TelegramChessBot/1.0 (contact: your_email@example.com)"}

def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    if MINI_APP_URL:
        builder.button(text="🚀 Відкрити Mini App", web_app=types.WebAppInfo(url=MINI_APP_URL))
    builder.button(text="👤 Профіль")
    builder.button(text="⚔️ Мої бої")
    builder.button(text="🏆 Лідерборд")
    builder.button(text="🎰 Казино")
    builder.button(text="🎁 Забрати +10")
    builder.adjust(1 if MINI_APP_URL else 2, 2)
    return builder.as_markup(resize_keyboard=True)


def mini_app_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🚀 Відкрити Mini App", web_app=types.WebAppInfo(url=MINI_APP_URL))
    return keyboard.as_markup()

# Динамічне отримання ігор за ПОТОЧНИЙ місяць
async def get_user_games(chess_username: str):
    current_date = datetime.now().strftime("%Y/%m")  # Автоматично бере YYYY/MM
    url = f"https://api.chess.com/pub/player/{chess_username.lower()}/games/{current_date}"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=CHESS_HEADERS) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("games", [])
            return []

def get_game_result(result: str):
    if result == "win":
        return "wins"
    if result in ["agreed", "repetition", "stalemate", "insufficient_material", "timevsinsufficient"]:
        return "draws"
    return "losses"

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    supabase.table("users").upsert({
        "id": message.from_user.id,
        "first_name": message.from_user.first_name,
        "username": (message.from_user.username or "").lower() or None,
    }).execute()

    saved_user = supabase.table("users") \
        .select("chess_username") \
        .eq("id", message.from_user.id) \
        .limit(1) \
        .execute().data

    if saved_user and saved_user[0].get("chess_username"):
        chess_nick = escape(saved_user[0]["chess_username"])
        greeting = (
            f"З поверненням, {escape(message.from_user.first_name)}!\n"
            f"Твій Chess.com нік вже прив'язаний: <b>{chess_nick}</b>\n\n"
            "Повторно реєструватися не потрібно."
        )
    else:
        greeting = (
            "Привіт! Я бот для шахів та ігор.\n\n"
            "Chess.com нік можна додати пізніше командою:\n"
            "<code>/set_chess ваш_нікнейм</code>"
        )

    await message.answer(
        greeting,
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )
    if MINI_APP_URL:
        await message.answer(
            "Відкрий казино через цю кнопку, щоб Telegram передав профіль і баланс:",
            reply_markup=mini_app_keyboard()
        )


@dp.message(Command("app"))
async def mini_app_command(message: types.Message):
    if not MINI_APP_URL:
        await message.answer("Mini App URL ще не налаштований.")
        return
    await message.answer(
        "🚀 Відкрий Mini App через Telegram:",
        reply_markup=mini_app_keyboard()
    )

@dp.message(Command("set_chess"))
async def set_chess_username(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Будь ласка, вкажи нікнейм. Приклад:\n<code>/set_chess magnuscarlsen</code>", parse_mode="HTML")
        return

    chess_nick = args[1].strip()
    user_id = message.from_user.id

    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.chess.com/pub/player/{chess_nick.lower()}", headers=CHESS_HEADERS) as resp:
            if resp.status != 200:
                await message.answer("❌ Такого користувача не знайдено на Chess.com!")
                return

    supabase.table("users").update({"chess_username": chess_nick.lower()}).eq("id", user_id).execute()

    await message.answer(f"✅ Твій Chess.com нікнейм збережено: <b>{chess_nick}</b>", parse_mode="HTML")

@dp.message(F.text == "👤 Профіль")
async def profile_handler(message: types.Message):
    user_id = message.from_user.id
    res = supabase.table("users").select("*").eq("id", user_id).execute()
    
    if res.data:
        chess_nick = res.data[0].get("chess_username") or "не вказано (напиши /set_chess)"
    else:
        chess_nick = "не вказано (напиши /set_chess)"

    stats = res.data[0] if res.data else {}
    chess_points = stats.get("points", 0)
    casino_balance = stats.get("casino_balance", 0)
    today = datetime.now().date().isoformat()
    bonus_claimed = stats.get("daily_bonus_date") == today or daily_bonus_claims.get(user_id) == today
    bonus_status = "✅ Уже забрано сьогодні" if bonus_claimed else "🎁 Доступно +10 очок"
    streak = stats.get("daily_streak", daily_streak_fallback.get(user_id, 0))

    await message.answer(
        f"<b>Твій профіль:</b>\n"
        f"👤 Ім'я: {message.from_user.first_name}\n"
        f"♟️ Chess.com: <b>{chess_nick}</b>\n"
        f"🏆 Шахові очки: <b>{chess_points}</b>\n"
        f"🎰 Баланс казино: <b>{casino_balance}</b>\n"
        f"🔥 Серія бонусів: <b>{streak} дн.</b>\n"
        f"{bonus_status}",
        reply_markup=exchange_keyboard(),
        parse_mode="HTML"
    )


@dp.message(F.text == "🎁 Забрати +10")
async def daily_bonus_handler(message: types.Message):
    user_id = message.from_user.id
    today = datetime.now().date().isoformat()
    rows = supabase.table("users").select("casino_balance, daily_bonus_date, daily_streak").eq("id", user_id).limit(1).execute().data

    if not rows:
        await message.answer("❌ Спочатку відкрий «🏆 Лідерборд», щоб створити баланс казино.")
        return

    player = rows[0]
    if player.get("daily_bonus_date") == today or daily_bonus_claims.get(user_id) == today:
        await message.answer("⏳ Ти вже забирав бонус сьогодні. Повертайся завтра!")
        return

    previous_date = player.get("daily_bonus_date")
    previous_streak = player.get("daily_streak", daily_streak_fallback.get(user_id, 0))
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    streak = previous_streak + 1 if previous_date == yesterday else 1
    bonus = 100 if streak % 7 == 0 else 10
    new_balance = player.get("casino_balance", 0) + bonus
    update_data = {"casino_balance": new_balance, "daily_bonus_date": today, "daily_streak": streak}
    supabase.table("users").update(update_data).eq("id", user_id).execute()

    await message.answer(
        f"🎁 Бонус отримано: <b>+{bonus} очок</b>\n"
        f"🔥 Серія: <b>{streak} дн.</b>\n"
        f"🎰 Твій баланс: <b>{new_balance}</b> оч.",
        parse_mode="HTML"
    )


def duel_keyboard(duel_id):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Прийняти", callback_data=f"duel:accept:{duel_id}")
    keyboard.button(text="❌ Відхилити", callback_data=f"duel:decline:{duel_id}")
    keyboard.adjust(2)
    return keyboard.as_markup()


def coin_side_keyboard(duel_id):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🟡 Орел", callback_data=f"duel:side:heads:{duel_id}")
    keyboard.button(text="⚪ Решка", callback_data=f"duel:side:tails:{duel_id}")
    keyboard.adjust(2)
    return keyboard.as_markup()


def duel_result_keyboard(duel_id):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔁 Зіграти ще раз", callback_data=f"duel:rematch:{duel_id}")
    return keyboard.as_markup()


@dp.message(F.text == "⚔️ Дуель")
async def duel_menu_handler(message: types.Message):
    await message.answer(
        "⚔️ Щоб викликати гравця, напиши:\n"
        "<code>/duel @username 20</code>\n\n"
        "Переможець отримає банк із двох ставок.",
        parse_mode="HTML"
    )


@dp.message(Command("duel"))
async def duel_handler(message: types.Message):
    args = message.text.split()
    if len(args) != 3:
        await message.answer("⚔️ Формат: <code>/duel @username 20</code>", parse_mode="HTML")
        return

    target_username = args[1].lstrip("@").lower()
    try:
        amount = int(args[2])
    except ValueError:
        await message.answer("❌ Ставка має бути цілим числом.")
        return
    if amount <= 0:
        await message.answer("❌ Ставка має бути більшою за нуль.")
        return
    if not message.from_user.username:
        await message.answer("❌ Для дуелі потрібен Telegram username у твоєму профілі.")
        return
    if target_username == message.from_user.username.lower():
        await message.answer("❌ Не можна викликати самого себе.")
        return

    target_rows = supabase.table("users") \
        .select("id, first_name, username") \
        .ilike("username", target_username) \
        .limit(1) \
        .execute().data
    if not target_rows:
        await message.answer("❌ Гравця не знайдено. Він має запустити бота і встановити username.")
        return

    current_month = datetime.now().strftime("%Y-%m")
    balance_rows = supabase.table("users").select("casino_balance").eq("id", message.from_user.id).limit(1).execute().data
    if not balance_rows or balance_rows[0].get("casino_balance", 0) < amount:
        balance = balance_rows[0].get("casino_balance", 0) if balance_rows else 0
        await message.answer(f"❌ Недостатньо очок казино. Баланс: <b>{balance}</b>.", parse_mode="HTML")
        return

    duel = supabase.table("duels").insert({
        "challenger_id": message.from_user.id,
        "opponent_id": target_rows[0]["id"],
        "amount": amount,
    }).execute().data[0]
    duel_id = duel["id"]
    await bot.send_message(
        target_rows[0]["id"],
        f"⚔️ <b>Тебе викликали на дуель!</b>\n\n"
        f"Гравець: @{escape(message.from_user.username)}\n"
        f"Ставка: <b>{amount}</b> очок казино\n"
        f"Банк переможця: <b>{amount * 2}</b> очок",
        reply_markup=duel_keyboard(duel_id),
        parse_mode="HTML"
    )
    await message.answer(f"✅ Виклик надіслано гравцю @{escape(target_username)}.", parse_mode="HTML")


@dp.callback_query(F.data.startswith("duel:"))
async def duel_callback_handler(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    action = parts[1]
    selected_side = parts[2] if action == "side" else None
    duel_id_text = parts[3] if action == "side" else parts[2]
    duel_id = int(duel_id_text)
    duel_rows = supabase.table("duels").select("*").eq("id", duel_id).limit(1).execute().data
    if not duel_rows:
        await callback.answer("Дуель не знайдена.", show_alert=True)
        return
    duel = duel_rows[0]
    if action == "rematch":
        if callback.from_user.id not in {duel["challenger_id"], duel["opponent_id"]}:
            await callback.answer("Це не твоя дуель.", show_alert=True)
            return
        if duel["status"] != "finished":
            await callback.answer("Ця дуель ще не завершена.", show_alert=True)
            return
        challenger_id = callback.from_user.id
        opponent_id = duel["opponent_id"] if challenger_id == duel["challenger_id"] else duel["challenger_id"]
        amount = duel["amount"]
        current_month = datetime.now().strftime("%Y-%m")
        balance_rows = supabase.table("users").select("casino_balance").eq("id", challenger_id).limit(1).execute().data
        if not balance_rows or balance_rows[0]["casino_balance"] < amount:
            await callback.answer("Недостатньо очок для повторної ставки.", show_alert=True)
            return
        new_duel = supabase.table("duels").insert({
            "challenger_id": challenger_id,
            "opponent_id": opponent_id,
            "amount": amount,
        }).execute().data[0]
        new_duel_id = new_duel["id"]
        challenger_user = supabase.table("users").select("username").eq("id", challenger_id).limit(1).execute().data
        challenger_name = (challenger_user[0].get("username") if challenger_user else None) or "гравець"
        await bot.send_message(
            opponent_id,
            f"⚔️ <b>Реванш!</b>\n\n"
            f"Гравець @{escape(challenger_name)} хоче зіграти ще раз.\n"
            f"Ставка: <b>{amount}</b> очок казино.",
            reply_markup=duel_keyboard(new_duel_id),
            parse_mode="HTML"
        )
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("✅ Реванш надіслано супернику.")
        await callback.answer("Реванш надіслано!")
        return

    if callback.from_user.id != duel["opponent_id"]:
        await callback.answer("Ця дуель призначена іншому гравцю.", show_alert=True)
        return
    if action in {"accept", "decline"} and duel["status"] != "pending":
        await callback.answer("Ця дуель вже завершена.", show_alert=True)
        return
    if action == "side" and duel["status"] != "accepted":
        await callback.answer("Спочатку прийми дуель.", show_alert=True)
        return

    if action == "decline":
        supabase.table("duels").update({"status": "declined"}).eq("id", duel_id).execute()
        await callback.message.edit_text("❌ Дуель відхилено.")
        await callback.answer()
        await bot.send_message(duel["challenger_id"], "❌ Твій виклик на дуель відхилили.")
        return

    current_month = datetime.now().strftime("%Y-%m")
    challenger_rows = supabase.table("users").select("casino_balance").eq("id", duel["challenger_id"]).limit(1).execute().data
    opponent_rows = supabase.table("users").select("casino_balance").eq("id", duel["opponent_id"]).limit(1).execute().data
    amount = duel["amount"]
    if not challenger_rows or not opponent_rows or challenger_rows[0]["casino_balance"] < amount or opponent_rows[0]["casino_balance"] < amount:
        await callback.answer("У одного з гравців недостатньо очок.", show_alert=True)
        return

    if action == "accept":
        accepted = supabase.table("duels").update({"status": "accepted"}).eq("id", duel_id).eq("status", "pending").select("id").execute().data
        if not accepted:
            await callback.answer("Дуель вже обробляється.", show_alert=True)
            return
        await callback.message.edit_text(
            "🪙 <b>Дуель прийнята!</b>\n\nОбери сторону монетки:",
            reply_markup=coin_side_keyboard(duel_id),
            parse_mode="HTML"
        )
        await callback.answer("Обери сторону монетки")
        return

    if action != "side" or selected_side not in {"heads", "tails"}:
        await callback.answer("Невідома дія.", show_alert=True)
        return

    updated = supabase.table("duels").update({"status": "flipping", "opponent_side": selected_side}).eq("id", duel_id).eq("status", "accepted").select("id").execute().data
    if not updated:
        await callback.answer("Монетка вже підкидається.", show_alert=True)
        return

    await callback.answer("Монетка підкидається!")
    challenger_animation = await bot.send_message(
        duel["challenger_id"],
        "🪙 <b>Готуємо підкидання монетки...</b>",
        parse_mode="HTML"
    )
    animation_frames = [
        "🪙 <b>Підкидаю монетку...</b>\n\n        🔄",
        "🪙 <b>Монетка в повітрі...</b>\n\n        🟡",
        "🪙 <b>Ще мить...</b>\n\n        ⚪",
        "🪙 <b>Ловлю!</b>\n\n        ✨",
    ]
    for frame in animation_frames:
        await callback.message.edit_text(frame, parse_mode="HTML")
        await challenger_animation.edit_text(frame, parse_mode="HTML")
        await asyncio.sleep(0.45)

    result_side = random.choice(["heads", "tails"])
    winner_id = duel["opponent_id"] if selected_side == result_side else duel["challenger_id"]
    bank = amount * 2
    supabase.table("users").update({"casino_balance": challenger_rows[0]["casino_balance"] - amount}).eq("id", duel["challenger_id"]).execute()
    supabase.table("users").update({"casino_balance": opponent_rows[0]["casino_balance"] - amount}).eq("id", duel["opponent_id"]).execute()
    winner_balance = (challenger_rows[0]["casino_balance"] if winner_id == duel["challenger_id"] else opponent_rows[0]["casino_balance"]) - amount + bank
    supabase.table("users").update({"casino_balance": winner_balance}).eq("id", winner_id).execute()
    supabase.table("duels").update({"status": "finished", "winner_id": winner_id}).eq("id", duel_id).execute()

    side_label = "🟡 Орел" if result_side == "heads" else "⚪ Решка"
    selected_side_label = "🟡 Орел" if selected_side == "heads" else "⚪ Решка"
    opponent_result_text = (
        f"🪙 <b>Монетка впала!</b>\n\n"
        f"Ти обрав: <b>{selected_side_label}</b>\n"
        f"Результат: <b>{side_label}</b>\n"
        f"Переможець: <b>{'ти' if winner_id == duel['opponent_id'] else 'суперник'}</b>\n"
        f"🏆 Банк: <b>{bank}</b> очок"
    )
    challenger_result_text = (
        f"🪙 <b>Монетка впала!</b>\n\n"
        f"Суперник обрав: <b>{selected_side_label}</b>\n"
        f"Результат: <b>{side_label}</b>\n"
        f"Переможець: <b>{'ти' if winner_id == duel['challenger_id'] else 'суперник'}</b>\n"
        f"🏆 Банк: <b>{bank}</b> очок"
    )
    result_markup = duel_result_keyboard(duel_id)
    await callback.message.edit_text(opponent_result_text, reply_markup=result_markup, parse_mode="HTML")
    await challenger_animation.edit_text(challenger_result_text, reply_markup=result_markup, parse_mode="HTML")


def case_battle_keyboard(battle_id):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Відкрити кейс", callback_data=f"case:accept:{battle_id}")
    keyboard.button(text="❌ Відхилити", callback_data=f"case:decline:{battle_id}")
    keyboard.adjust(2)
    return keyboard.as_markup()


def case_drop():
    drops = [
        ("🪙 Монетка", 5, 45),
        ("💵 Купюра", 15, 30),
        ("💎 Алмаз", 40, 18),
        ("👑 Корона", 100, 6),
        ("🌟 Джекпот", 250, 1),
    ]
    name, value, _ = random.choices(drops, weights=[drop[2] for drop in drops], k=1)[0]
    return name, value


SOLO_CASES = {
    "common": {"title": "🟫 ЗВИЧАЙНИЙ КЕЙС", "cost": 10, "weight_bonus": 1},
    "rare": {"title": "🟪 РІДКІСНИЙ КЕЙС", "cost": 30, "weight_bonus": 2},
    "legendary": {"title": "🟨 ЛЕГЕНДАРНИЙ КЕЙС", "cost": 100, "weight_bonus": 4},
}


def solo_case_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🟫 Звичайний · 10", callback_data="solo_case:common")
    keyboard.button(text="🟪 Рідкісний · 30", callback_data="solo_case:rare")
    keyboard.button(text="🟨 Легендарний · 100", callback_data="solo_case:legendary")
    keyboard.button(text="⬅️ Назад", callback_data="casino:solo")
    keyboard.adjust(1)
    return keyboard.as_markup()


def solo_case_drop(case_type):
    drop_sets = {
        "common": [("🪙 Монетка", 5, 55), ("💵 Купюра", 15, 35), ("💎 Алмаз", 40, 10)],
        "rare": [("💵 Купюра", 15, 40), ("💎 Алмаз", 40, 38), ("👑 Корона", 100, 18), ("🌟 Джекпот", 250, 4)],
        "legendary": [("💎 Алмаз", 40, 25), ("👑 Корона", 100, 45), ("🌟 Джекпот", 250, 30)],
    }
    drops = drop_sets[case_type]
    name, value, _ = random.choices(drops, weights=[drop[2] for drop in drops], k=1)[0]
    return name, value


def solo_case_result_keyboard(case_type):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🔁 Відкрити ще раз", callback_data=f"solo_case:{case_type}")
    keyboard.button(text="📦 Інший кейс", callback_data="solo_case:menu")
    keyboard.adjust(1)
    return keyboard.as_markup()


def case_roll_frame(title, center_drop):
    drop_names = ["🪙", "💵", "💎", "👑", "🌟"]
    preview = [random.choice(drop_names) for _ in range(4)]
    left_items = "  ".join(preview[:2])
    right_items = "  ".join(preview[2:])
    return (
        f"📦 <b>{title}</b>\n\n"
        f"{left_items}  | <b>{center_drop}</b> |  {right_items}\n"
        f"                 🎯"
    )


@dp.message(Command("casebattle"))
async def case_battle_handler(message: types.Message):
    args = message.text.split()
    if len(args) != 3:
        await message.answer("📦 Формат: <code>/casebattle @username 20</code>", parse_mode="HTML")
        return
    if not message.from_user.username:
        await message.answer("❌ Для битви потрібен Telegram username.")
        return
    target_username = args[1].lstrip("@").lower()
    try:
        amount = int(args[2])
    except ValueError:
        await message.answer("❌ Ставка має бути цілим числом.")
        return
    if amount <= 0:
        await message.answer("❌ Ставка має бути більшою за нуль.")
        return
    if target_username == message.from_user.username.lower():
        await message.answer("❌ Не можна викликати самого себе.")
        return

    target_rows = supabase.table("users").select("id, username").ilike("username", target_username).limit(1).execute().data
    if not target_rows:
        await message.answer("❌ Гравця не знайдено. Він має натиснути /start у боті.")
        return

    current_month = datetime.now().strftime("%Y-%m")
    balance_rows = supabase.table("users").select("casino_balance").eq("id", message.from_user.id).limit(1).execute().data
    balance = balance_rows[0].get("casino_balance", 0) if balance_rows else 0
    if balance < amount:
        await message.answer(f"❌ Недостатньо очок казино. Баланс: <b>{balance}</b>.", parse_mode="HTML")
        return

    battle = supabase.table("duels").insert({
        "challenger_id": message.from_user.id,
        "opponent_id": target_rows[0]["id"],
        "amount": amount,
    }).execute().data[0]
    battle_id = battle["id"]
    await bot.send_message(
        target_rows[0]["id"],
        f"📦 <b>Битва кейсів!</b>\n\n"
        f"Гравець: @{escape(message.from_user.username)}\n"
        f"Ставка: <b>{amount}</b> очок\n"
        f"Переможець забере: <b>{amount * 2}</b> очок",
        reply_markup=case_battle_keyboard(battle_id),
        parse_mode="HTML"
    )
    await message.answer(f"✅ Виклик на битву кейсів надіслано @{escape(target_username)}.", parse_mode="HTML")


@dp.callback_query(F.data.startswith("case:"))
async def case_battle_callback(callback: types.CallbackQuery):
    _, action, battle_id_text = callback.data.split(":")
    battle_id = int(battle_id_text)
    rows = supabase.table("duels").select("*").eq("id", battle_id).limit(1).execute().data
    if not rows:
        await callback.answer("Битву не знайдено.", show_alert=True)
        return
    battle = rows[0]
    if callback.from_user.id != battle["opponent_id"] or battle["status"] != "pending":
        await callback.answer("Цей виклик уже недійсний.", show_alert=True)
        return
    if action == "decline":
        supabase.table("duels").update({"status": "declined"}).eq("id", battle_id).execute()
        await callback.message.edit_text("❌ Битву кейсів відхилено.")
        await callback.answer()
        await bot.send_message(battle["challenger_id"], "❌ Твій виклик на битву кейсів відхилили.")
        return

    current_month = datetime.now().strftime("%Y-%m")
    challenger_rows = supabase.table("users").select("casino_balance").eq("id", battle["challenger_id"]).limit(1).execute().data
    opponent_rows = supabase.table("users").select("casino_balance").eq("id", battle["opponent_id"]).limit(1).execute().data
    amount = battle["amount"]
    if not challenger_rows or not opponent_rows or challenger_rows[0]["casino_balance"] < amount or opponent_rows[0]["casino_balance"] < amount:
        await callback.answer("У одного з гравців недостатньо очок.", show_alert=True)
        return
    accepted = supabase.table("duels").update({"status": "opening"}).eq("id", battle_id).eq("status", "pending").select("id").execute().data
    if not accepted:
        await callback.answer("Битва вже відкривається.", show_alert=True)
        return

    await callback.answer("Відкриваємо кейси!")
    challenger_drop, challenger_value = case_drop()
    opponent_drop, opponent_value = case_drop()
    challenger_message = await bot.send_message(
        battle["challenger_id"],
        case_roll_frame("ТВІЙ КЕЙС", "🔒"),
        parse_mode="HTML"
    )
    animation_steps = [
        ("🎁 КРУТИМО", 0.45),
        ("🎁 КРУТИМО", 0.55),
        ("🎁 СПОВІЛЬНЮЄМО", 0.70),
        ("🎁 ОСТАННІЙ КАДР", 0.90),
    ]
    for title, pause in animation_steps:
        challenger_frame = case_roll_frame("ТВІЙ КЕЙС", "🔒")
        opponent_frame = case_roll_frame("КЕЙС СУПЕРНИКА", "🔒")
        await callback.message.edit_text(opponent_frame, parse_mode="HTML")
        await challenger_message.edit_text(challenger_frame, parse_mode="HTML")
        await asyncio.sleep(pause)

    await callback.message.edit_text(
        case_roll_frame("КЕЙС СУПЕРНИКА · 🎯 ЗУПИНКА", opponent_drop),
        parse_mode="HTML"
    )
    await challenger_message.edit_text(
        case_roll_frame("ТВІЙ КЕЙС · 🎯 ЗУПИНКА", challenger_drop),
        parse_mode="HTML"
    )
    await asyncio.sleep(0.7)
    if challenger_value > opponent_value:
        winner_id = battle["challenger_id"]
    elif opponent_value > challenger_value:
        winner_id = battle["opponent_id"]
    else:
        winner_id = None
    bank = amount * 2
    challenger_balance = challenger_rows[0]["casino_balance"] - amount
    opponent_balance = opponent_rows[0]["casino_balance"] - amount
    if winner_id == battle["challenger_id"]:
        challenger_balance += bank
    elif winner_id == battle["opponent_id"]:
        opponent_balance += bank
    else:
        challenger_balance += amount
        opponent_balance += amount
    supabase.table("users").update({"casino_balance": challenger_balance}).eq("id", battle["challenger_id"]).execute()
    supabase.table("users").update({"casino_balance": opponent_balance}).eq("id", battle["opponent_id"]).execute()
    supabase.table("duels").update({"status": "finished", "winner_id": winner_id}).eq("id", battle_id).execute()
    outcome = "🤝 Нічия! Ставки повернено." if winner_id is None else f"🏆 Переможець забирає <b>{bank}</b> очок!"
    opponent_result = f"📦 <b>Твій дроп:</b> {opponent_drop} ({opponent_value})\n<b>Дроп суперника:</b> {challenger_drop} ({challenger_value})\n\n{outcome}"
    challenger_result = f"📦 <b>Твій дроп:</b> {challenger_drop} ({challenger_value})\n<b>Дроп суперника:</b> {opponent_drop} ({opponent_value})\n\n{outcome}"
    await callback.message.edit_text(opponent_result, parse_mode="HTML")
    await challenger_message.edit_text(challenger_result, parse_mode="HTML")


def exchange_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="♻️ Обміняти шахові очки", callback_data="exchange:start")
    return keyboard.as_markup()


chess_exchange_waiting = set()
chess_exchange_fallback = {}


@dp.callback_query(F.data == "exchange:start")
async def exchange_start_handler(callback: types.CallbackQuery):
    chess_exchange_waiting.add(callback.from_user.id)
    await callback.answer()
    await callback.message.answer(
        "♻️ Введи скільки шахових очок обміняти.\n"
        "Курс: <b>1 шахове очко = 10 очок казино</b>\n\n"
        "Наприклад: <code>3</code>",
        parse_mode="HTML"
    )


@dp.message(lambda message: message.from_user.id in chess_exchange_waiting)
async def exchange_input_handler(message: types.Message):
    try:
        amount = int(message.text.strip())
    except (AttributeError, ValueError):
        await message.answer("❌ Введи ціле число, наприклад <code>3</code>.", parse_mode="HTML")
        return

    if amount <= 0:
        await message.answer("❌ Кількість має бути більшою за нуль.")
        return

    user_id = message.from_user.id
    rows = supabase.table("users").select("points, casino_balance, exchanged_chess_points").eq("id", user_id).limit(1).execute().data

    if not rows:
        await message.answer("❌ Спочатку відкрий «🏆 Лідерборд».")
        return

    player = rows[0]
    exchanged = player.get("exchanged_chess_points", chess_exchange_fallback.get(user_id, 0))
    available_points = player.get("points", 0)
    if amount > available_points:
        await message.answer(
            f"❌ Недостатньо шахових очок. Доступно: <b>{available_points}</b>.",
            parse_mode="HTML"
        )
        return

    new_points = available_points - amount
    new_balance = player.get("casino_balance", 0) + amount * 10
    update_data = {"points": new_points, "casino_balance": new_balance}
    update_data["exchanged_chess_points"] = exchanged + amount
    supabase.table("users").update(update_data).eq("id", user_id).execute()
    chess_exchange_waiting.discard(user_id)

    await message.answer(
        f"✅ Обмін виконано: <b>-{amount}</b> шах. оч.\n"
        f"🎰 Отримано: <b>+{amount * 10}</b> оч. казино\n"
        f"🏆 Залишилось шахових: <b>{new_points}</b>\n"
        f"🎰 Баланс казино: <b>{new_balance}</b>",
        parse_mode="HTML"
    )


@dp.message(F.text == "⚔️ Мої бої")
async def battles_handler(message: types.Message):
    user_id = message.from_user.id
    
    me_res = supabase.table("users").select("*").eq("id", user_id).execute()
    if not me_res.data or not me_res.data[0].get("chess_username"):
        await message.answer("❌ Спочатку прив'яжи свій нікнейм через команду:\n<code>/set_chess ваш_нікнейм</code>", parse_mode="HTML")
        return

    my_chess_nick = me_res.data[0]["chess_username"].lower()

    # Отримуємо всіх інших користувачів із бази
    all_users = supabase.table("users").select("*").neq("id", user_id).execute().data
    opponents = {u["chess_username"].lower(): u["first_name"] for u in all_users if u.get("chess_username")}

    if not opponents:
        await message.answer("⚠️ В базі немає інших гравців! Ваші друзі повинні також зареєструватися в боті через команду <code>/set_chess</code>.", parse_mode="HTML")
        return

    await message.answer("🔄 Підраховую табло боїв з Chess.com...")

    games = await get_user_games(my_chess_nick)

    stats = {}

    for game in games:
        white = game["white"]["username"].lower()
        black = game["black"]["username"].lower()

        opponent = None
        if white == my_chess_nick and black in opponents:
            opponent = black
            my_color = "white"
        elif black == my_chess_nick and white in opponents:
            opponent = white
            my_color = "black"

        if opponent:
            if opponent not in stats:
                stats[opponent] = {"wins": 0, "losses": 0, "draws": 0}

            my_result = game[my_color]["result"]

            if my_result == "win":
                stats[opponent]["wins"] += 1
            elif my_result in ["agreed", "repetition", "stalemate", "insufficient_material", "timevsinsufficient"]:
                stats[opponent]["draws"] += 1
            else:
                stats[opponent]["losses"] += 1

    if not stats:
        await message.answer("🥊 У вас ще не було спільних партій із зареєстрованими учасниками за цей місяць.")
        return

    report = "<b>🥊 РАХУНОК БОЇВ (Head-to-Head):</b>\n\n"
    for opp_nick, score in stats.items():
        name = opponents[opp_nick]
        w, l, d = score["wins"], score["losses"], score["draws"]
        report += f"⚔️ <b>Ви vs {name}</b> (@{opp_nick}):\n"
        report += f"🏆 Рахунок: <b>{w} - {l}</b>" + (f" (Нічиїх: {d})" if d > 0 else "") + "\n\n"

    await message.answer(report, parse_mode="HTML")

@dp.message(Command("leaderboard"))
@dp.message(F.text == "🏆 Лідерборд")
async def leaderboard_handler(message: types.Message):
    users = supabase.table("users").select("*").execute().data
    players = [user for user in users if user.get("chess_username")]

    if not players:
        await message.answer("⚠️ Лідерборд порожній. Зареєструй Chess.com нік через /set_chess.")
        return

    await message.answer("🔄 Оновлюю лідерборд за поточний місяць...")

    games_by_player = await asyncio.gather(
        *(get_user_games(user["chess_username"]) for user in players)
    )
    for user, games in zip(players, games_by_player):
        stats = {"wins": 0, "draws": 0, "losses": 0}
        for game in games:
            chess_nick = user["chess_username"].lower()
            white = game.get("white", {}).get("username", "").lower()
            black = game.get("black", {}).get("username", "").lower()
            color = "white" if white == chess_nick else "black" if black == chess_nick else None
            if color:
                result = game.get(color, {}).get("result")
                if result:
                    stats[get_game_result(result)] += 1

        stats["points"] = stats["wins"] * 3 + stats["draws"] - stats["losses"] * 2
        stats["games"] = stats["wins"] + stats["draws"] + stats["losses"]
        previous_chess_points = user.get("chess_points", 0)
        previous_balance = user.get("casino_balance", 0)
        previous_exchanged = user.get("exchanged_chess_points", 0)
        available_points = stats["points"] - previous_exchanged
        leaderboard_row = {
            "first_name": user.get("first_name") or user.get("username") or "Гравець",
            "username": user.get("username") or "відсутній",
            "chess_username": user["chess_username"],
            "chess_points": stats["points"],
            "casino_balance": previous_balance + stats["points"] - previous_chess_points,
            **stats,
            "points": available_points,
        }
        supabase.table("users").update(leaderboard_row).eq("id", user["id"]).execute()

    top_players = supabase.table("users") \
        .select("first_name, username, chess_username, points, wins, draws, losses, games") \
        .order("points", desc=True) \
        .order("wins", desc=True) \
        .order("games", desc=True) \
        .limit(3) \
        .execute().data

    report = f"<b>🏆 ЛІДЕРБОРД ({datetime.now().strftime('%m.%Y')})</b>\n"
    report += "<i>+3 за перемогу, +1 за нічию, -2 за програш</i>\n\n"

    for position, player in enumerate(top_players, start=1):
        name = escape(player.get("first_name") or player.get("username") or "Гравець")
        chess_nick = escape(player["chess_username"])
        report += (
            f"<b>{position}.</b> {name} (<code>{chess_nick}</code>) — "
            f"<b>{player['points']} оч.</b> | "
            f"{player['wins']}W / {player['draws']}D / {player['losses']}L\n"
        )

    await message.answer(report, parse_mode="HTML")


def blackjack_card():
    return random.choice([2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 11])


def blackjack_total(cards):
    total = sum(cards)
    aces = cards.count(11)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


blackjack_games = {}
blackjack_waiting_for_bet = set()
dice_waiting_for_bet = set()
solo_case_games = {}


def blackjack_keyboard(game_active=True):
    keyboard = InlineKeyboardBuilder()
    if game_active:
        keyboard.button(text="➕ Hit", callback_data="blackjack:hit")
        keyboard.button(text="✋ Stay", callback_data="blackjack:stay")
    else:
        keyboard.button(text="🎰 Нова гра", callback_data="blackjack:new")
    keyboard.adjust(2)
    return keyboard.as_markup()


def blackjack_result_keyboard(bet):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text=f"🔁 Повторити ставку {bet}", callback_data=f"blackjack:repeat:{bet}")
    keyboard.button(text="✏️ Інша ставка", callback_data="blackjack:new")
    keyboard.adjust(1)
    return keyboard.as_markup()


def blackjack_bet_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="1 очко", callback_data="blackjack:bet:1")
    keyboard.button(text="5 очок", callback_data="blackjack:bet:5")
    keyboard.button(text="10 очок", callback_data="blackjack:bet:10")
    keyboard.adjust(3)
    return keyboard.as_markup()


def blackjack_cards_text(cards):
    return "  ".join(f"[A]" if card == 11 else f"[{card}]" for card in cards)


def blackjack_game_text(game, result=None, change=0, balance=None):
    dealer_cards = game["dealer_cards"] if result else ["?"] + game["dealer_cards"][1:]
    dealer_score = blackjack_total(dealer_cards) if result else "?"
    text = (
        "🎰 <b>BLACKJACK</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"Дилер: {blackjack_cards_text(dealer_cards)} = "
        f"<b>{dealer_score}</b>\n"
        f"Ти: {blackjack_cards_text(game['player_cards'])} = "
        f"<b>{blackjack_total(game['player_cards'])}</b>\n\n"
        f"Ставка: <b>{game['bet']}</b> оч."
    )
    if result:
        text += (
            f"\n\n<b>{result}</b>  {change:+d} оч.\n"
            f"Баланс: <b>{balance}</b> оч."
        )
    else:
        text += "\n\n<i>Твій хід: добрати карту або зупинитися.</i>"
    return text


async def get_blackjack_balance(user_id):
    rows = supabase.table("users").select("casino_balance").eq("id", user_id).limit(1).execute().data
    return rows[0].get("casino_balance", 0) if rows else None


async def finish_blackjack(user_id, result, change):
    game = blackjack_games.pop(user_id)
    balance = await get_blackjack_balance(user_id)
    new_balance = balance + change
    supabase.table("users").update({"casino_balance": new_balance}).eq("id", user_id).execute()
    return blackjack_game_text(game, result, change, new_balance), new_balance


def casino_mode_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🤖 Сам проти бота", callback_data="casino:solo")
    keyboard.button(text="👥 З другом", callback_data="casino:friend")
    keyboard.adjust(1)
    return keyboard.as_markup()


def solo_games_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🃏 Blackjack", callback_data="casino:solo:blackjack")
    keyboard.button(text="🎲 Кості", callback_data="casino:solo:dice")
    keyboard.button(text="📦 Кейси", callback_data="casino:solo:cases")
    keyboard.button(text="⬅️ Назад", callback_data="casino:menu")
    keyboard.adjust(1)
    return keyboard.as_markup()


def friend_games_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="🪙 Монетка", callback_data="casino:friend:coin")
    keyboard.button(text="📦 Битва кейсів", callback_data="casino:friend:cases")
    keyboard.button(text="⬅️ Назад", callback_data="casino:menu")
    keyboard.adjust(1)
    return keyboard.as_markup()


@dp.message(F.text == "🎰 Казино")
async def casino_menu_handler(message: types.Message):
    await message.answer(
        "🎰 <b>КАЗИНО</b>\n\n"
        "Обери режим гри:",
        reply_markup=casino_mode_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "casino:solo")
async def casino_solo_handler(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "🤖 <b>САМ ПРОТИ БОТА</b>\n\n"
        "Обери гру:",
        reply_markup=solo_games_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "casino:solo:cases")
async def casino_solo_cases_handler(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "📦 <b>КЕЙСИ ПРОТИ БОТА</b>\n\n"
        "Обери кейс. Після відкриття рулетка поступово зупиниться на твоєму дропі:",
        reply_markup=solo_case_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "casino:solo:blackjack")
async def casino_blackjack_handler(callback: types.CallbackQuery):
    blackjack_waiting_for_bet.add(callback.from_user.id)
    await callback.answer()
    await callback.message.edit_text(
        "🃏 <b>BLACKJACK</b>\n\n"
        "Введи ставку числом, наприклад <code>20</code>:",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "casino:solo:dice")
async def casino_dice_handler(callback: types.CallbackQuery):
    dice_waiting_for_bet.add(callback.from_user.id)
    await callback.answer()
    await callback.message.edit_text(
        "🎲 <b>КОСТІ</b>\n\n"
        "Введи ставку числом, наприклад <code>20</code>:",
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("solo_case:"))
async def solo_case_callback(callback: types.CallbackQuery):
    case_type = callback.data.split(":", maxsplit=1)[1]
    if case_type == "menu":
        await callback.answer()
        await callback.message.answer(
            "🤖 <b>САМ ПРОТИ БОТА</b>\n\nОбери гру:",
            reply_markup=solo_games_keyboard(),
            parse_mode="HTML"
        )
        return
    if case_type not in SOLO_CASES:
        await callback.answer("Невідомий кейс.", show_alert=True)
        return

    user_id = callback.from_user.id
    if user_id in solo_case_games:
        await callback.answer("Спочатку заверши поточне відкриття.", show_alert=True)
        return
    balance = await get_blackjack_balance(user_id)
    case = SOLO_CASES[case_type]
    if balance is None:
        await callback.answer("Спочатку відкрий лідерборд.", show_alert=True)
        return
    if balance < case["cost"]:
        await callback.answer(f"Недостатньо очок. Потрібно: {case['cost']}.", show_alert=True)
        return

    solo_case_games[user_id] = case_type
    await callback.answer("Кейс відкривається!")
    drop_name, drop_value = solo_case_drop(case_type)
    rolling_message = await callback.message.edit_text(
        case_roll_frame(case["title"], "🔒"),
        parse_mode="HTML"
    )
    animation_steps = [
        (0.45, "🎁 КРУТИМО"),
        (0.55, "🎁 КРУТИМО"),
        (0.70, "🎁 СПОВІЛЬНЮЄМО"),
        (0.90, "🎁 ОСТАННІЙ КАДР"),
    ]
    for pause, title in animation_steps:
        await rolling_message.edit_text(
            case_roll_frame(f"{case['title']} · {title}", "🔒"),
            parse_mode="HTML"
        )
        await asyncio.sleep(pause)

    await rolling_message.edit_text(
        case_roll_frame(f"{case['title']} · 🎯 ЗУПИНКА", drop_name),
        parse_mode="HTML"
    )
    await asyncio.sleep(0.7)

    new_balance = balance - case["cost"] + drop_value
    supabase.table("users").update({"casino_balance": new_balance}).eq("id", user_id).execute()
    solo_case_games.pop(user_id, None)
    profit = drop_value - case["cost"]
    await rolling_message.edit_text(
        f"🎉 <b>КЕЙС ВІДКРИТО!</b>\n━━━━━━━━━━━━━━\n"
        f"{case['title']}\n\n"
        f"Твій дроп: <b>{drop_name}</b>\n"
        f"Вартість: <b>{drop_value}</b> оч.\n"
        f"Результат: <b>{profit:+d}</b> оч.\n\n"
        f"🎰 Баланс: <b>{new_balance}</b> оч.",
        reply_markup=solo_case_result_keyboard(case_type),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "casino:menu")
async def casino_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "🎰 <b>КАЗИНО</b>\n\nОбери режим гри:",
        reply_markup=casino_mode_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "casino:friend")
async def casino_friend_handler(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "👥 <b>ГРА З ДРУГОМ</b>\n\nОбери гру:",
        reply_markup=friend_games_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "casino:friend:coin")
async def casino_coin_duel_handler(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "🪙 <b>ДУЕЛЬ НА МОНЕТЦІ</b>\n\n"
        "Виклич друга командою:\n"
        "<code>/duel @username ставка</code>\n\n"
        "Наприклад: <code>/duel @player 20</code>",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "casino:friend:cases")
async def casino_cases_handler(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "📦 <b>БИТВА КЕЙСІВ</b>\n\n"
        "Виклич друга командою:\n"
        "<code>/casebattle @username ставка</code>\n\n"
        "Обидва відкриють кейси. Хто отримає дорожчий дроп, той забирає весь банк.",
        parse_mode="HTML"
    )


@dp.message(Command("blackjack"))
async def blackjack_handler(message: types.Message):
    args = message.text.split(maxsplit=1) if message.text and message.text.startswith("/blackjack") else []
    if len(args) < 2:
        await message.answer("🎰 Вкажи ставку очками: <code>/blackjack 5</code>", parse_mode="HTML")
        return

    try:
        bet = int(args[1].strip())
    except ValueError:
        await message.answer("❌ Ставка має бути цілим числом очок.")
        return

    if bet <= 0:
        await message.answer("❌ Ставка має бути більшою за нуль.")
        return

    await start_blackjack(message, bet)


@dp.message(F.text == "🎰 Blackjack")
async def blackjack_menu_handler(message: types.Message):
    blackjack_waiting_for_bet.add(message.from_user.id)
    await message.answer(
        "🎰 <b>BLACKJACK</b>\n\n"
        "Введи ставку числом, наприклад <code>7</code>\n"
        "Доступно очок: перевір у профілі.",
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("blackjack:bet:"))
async def blackjack_callback_handler(callback: types.CallbackQuery):
    bet = int(callback.data.split(":")[-1])
    await callback.answer()
    await start_blackjack(callback.message, bet, callback.from_user.id)


async def start_blackjack(message, bet, user_id=None):
    user_id = user_id or message.from_user.id
    if user_id in blackjack_games:
        await message.answer("🎰 У тебе вже є активна партія. Натисни Hit або Stay.")
        return
    balance = await get_blackjack_balance(user_id)
    if balance is None:
        await message.answer("❌ Спочатку відкрий «🏆 Лідерборд», щоб отримати актуальні очки.")
        return
    if bet > balance:
        await message.answer(f"❌ Недостатньо очок. Баланс: <b>{balance}</b>.", parse_mode="HTML")
        return

    player_cards = [blackjack_card(), blackjack_card()]
    dealer_cards = [blackjack_card(), blackjack_card()]
    blackjack_games[user_id] = {"bet": bet, "player_cards": player_cards, "dealer_cards": dealer_cards}
    await message.answer(
        blackjack_game_text(blackjack_games[user_id]),
        reply_markup=blackjack_keyboard(),
        parse_mode="HTML"
    )


def dice_result_keyboard(bet):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text=f"🔁 Повторити ставку {bet}", callback_data=f"dice:repeat:{bet}")
    keyboard.button(text="🎰 Інша гра", callback_data="casino:solo")
    keyboard.adjust(1)
    return keyboard.as_markup()


async def start_dice(message, bet, user_id=None):
    user_id = user_id or message.from_user.id
    balance = await get_blackjack_balance(user_id)
    if balance is None:
        await message.answer("❌ Спочатку відкрий «🏆 Лідерборд», щоб створити баланс казино.")
        return
    if bet > balance:
        await message.answer(f"❌ Недостатньо очок. Баланс: <b>{balance}</b>.", parse_mode="HTML")
        return

    rolling_message = await message.answer("🎲 <b>Готуємо кидок...</b>", parse_mode="HTML")
    frames = [
        "🎲 <b>Кидаю кубики...</b>\n\n🎲  ·  🎲",
        "🎲 <b>Кубики в повітрі...</b>\n\n⚀  ·  ⚄",
        "🎲 <b>Ще мить...</b>\n\n⚂  ·  ⚁",
    ]
    for frame in frames:
        await rolling_message.edit_text(frame, parse_mode="HTML")
        await asyncio.sleep(0.5)

    player_dice = [random.randint(1, 6), random.randint(1, 6)]
    bot_dice = [random.randint(1, 6), random.randint(1, 6)]
    player_total = sum(player_dice)
    bot_total = sum(bot_dice)
    if player_total > bot_total:
        result, change = "🏆 Перемога!", bet
    elif player_total < bot_total:
        result, change = "💥 Поразка", -bet
    else:
        result, change = "🤝 Нічия", 0

    new_balance = balance + change
    supabase.table("users").update({"casino_balance": new_balance}).eq("id", user_id).execute()
    await rolling_message.edit_text(
        f"🎲 <b>КОСТІ</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"Твої: <b>{player_dice[0]} + {player_dice[1]} = {player_total}</b>\n"
        f"Бота: <b>{bot_dice[0]} + {bot_dice[1]} = {bot_total}</b>\n\n"
        f"<b>{result}</b>  {change:+d} оч.\n"
        f"Баланс: <b>{new_balance}</b> оч.",
        reply_markup=dice_result_keyboard(bet),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "blackjack:new")
async def blackjack_new_callback(callback: types.CallbackQuery):
    await callback.answer()
    blackjack_waiting_for_bet.add(callback.from_user.id)
    await callback.message.answer(
        "🎰 Введи нову ставку числом, наприклад <code>7</code>:",
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("blackjack:repeat:"))
async def blackjack_repeat_callback(callback: types.CallbackQuery):
    bet = int(callback.data.split(":")[-1])
    await callback.answer()
    await start_blackjack(callback.message, bet, callback.from_user.id)


@dp.callback_query(F.data.startswith("dice:repeat:"))
async def dice_repeat_callback(callback: types.CallbackQuery):
    bet = int(callback.data.split(":")[-1])
    await callback.answer()
    await start_dice(callback.message, bet, callback.from_user.id)


@dp.message(lambda message: message.from_user.id in dice_waiting_for_bet)
async def dice_bet_input_handler(message: types.Message):
    try:
        bet = int(message.text.strip())
    except (AttributeError, ValueError):
        await message.answer("❌ Введи ставку цілим числом, наприклад <code>20</code>.", parse_mode="HTML")
        return

    if bet <= 0:
        await message.answer("❌ Ставка має бути більшою за нуль.")
        return

    dice_waiting_for_bet.discard(message.from_user.id)
    await start_dice(message, bet)


@dp.message(lambda message: message.from_user.id in blackjack_waiting_for_bet)
async def blackjack_bet_input_handler(message: types.Message):
    try:
        bet = int(message.text.strip())
    except (AttributeError, ValueError):
        await message.answer("❌ Введи ставку цілим числом, наприклад <code>7</code>.", parse_mode="HTML")
        return

    if bet <= 0:
        await message.answer("❌ Ставка має бути більшою за нуль.")
        return

    blackjack_waiting_for_bet.discard(message.from_user.id)
    await start_blackjack(message, bet)


@dp.callback_query(F.data.in_({"blackjack:hit", "blackjack:stay"}))
async def blackjack_action_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    game = blackjack_games.get(user_id)
    if not game:
        await callback.answer("Ця гра вже завершена. Почни нову.", show_alert=True)
        return

    if callback.data == "blackjack:hit":
        game["player_cards"].append(blackjack_card())
        if blackjack_total(game["player_cards"]) > 21:
            bet = game["bet"]
            text, _ = await finish_blackjack(user_id, "💥 Перебір! Програш", -bet)
            await callback.message.edit_text(text, reply_markup=blackjack_result_keyboard(bet), parse_mode="HTML")
        else:
            await callback.message.edit_text(
                blackjack_game_text(game), reply_markup=blackjack_keyboard(), parse_mode="HTML"
            )
    else:
        while blackjack_total(game["dealer_cards"]) < 17:
            game["dealer_cards"].append(blackjack_card())
        player_total = blackjack_total(game["player_cards"])
        dealer_total = blackjack_total(game["dealer_cards"])
        if dealer_total > 21 or player_total > dealer_total:
            result, change = "🏆 Перемога!", game["bet"]
        elif player_total < dealer_total:
            result, change = "😔 Програш", -game["bet"]
        else:
            result, change = "🤝 Нічия", 0
        bet = game["bet"]
        text, _ = await finish_blackjack(user_id, result, change)
        await callback.message.edit_text(text, reply_markup=blackjack_result_keyboard(bet), parse_mode="HTML")
    await callback.answer()


@dp.message()
async def cmd_leaderboard(message: types.Message):
    report = f"Невідома команда: {message.text}"
    await message.answer(report)


async def mini_app_handler(request):
    return web.FileResponse("web/index.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


def get_mini_app_user(request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data or not BOT_TOKEN:
        return None
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        return None
    try:
        return json.loads(values["user"])
    except (KeyError, json.JSONDecodeError):
        return None


def mini_json_error(message, status=400):
    return web.json_response({"error": message}, status=status)


async def mini_profile_api(request):
    telegram_user = get_mini_app_user(request)
    if not telegram_user:
        return mini_json_error("Telegram authorization required", 401)
    user_id = telegram_user["id"]
    user_rows = supabase.table("users").select("first_name, username, points, casino_balance, daily_streak").eq("id", user_id).limit(1).execute().data
    user = user_rows[0] if user_rows else {}
    return web.json_response({
        "user": user.get("first_name") or telegram_user.get("first_name", "Гравець"),
        "username": user.get("username") or telegram_user.get("username", ""),
        "chess_points": user.get("points", 0),
        "casino_balance": user.get("casino_balance", 0),
        "daily_streak": user.get("daily_streak", 0),
    })


async def mini_daily_bonus_api(request):
    telegram_user = get_mini_app_user(request)
    if not telegram_user:
        return mini_json_error("Telegram authorization required", 401)
    user_id = telegram_user["id"]
    today = datetime.now().date().isoformat()
    rows = supabase.table("users").select("casino_balance, daily_bonus_date, daily_streak").eq("id", user_id).limit(1).execute().data
    if not rows:
        return mini_json_error("Open the leaderboard first", 404)
    player = rows[0]
    if player.get("daily_bonus_date") == today or daily_bonus_claims.get(user_id) == today:
        return mini_json_error("Daily bonus already claimed", 409)
    previous_date = player.get("daily_bonus_date")
    previous_streak = player.get("daily_streak", daily_streak_fallback.get(user_id, 0))
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    streak = previous_streak + 1 if previous_date == yesterday else 1
    bonus = 100 if streak % 7 == 0 else 10
    new_balance = player.get("casino_balance", 0) + bonus
    update_data = {"casino_balance": new_balance, "daily_bonus_date": today, "daily_streak": streak}
    supabase.table("users").update(update_data).eq("id", user_id).execute()
    return web.json_response({"bonus": bonus, "streak": streak, "casino_balance": new_balance})


async def mini_blackjack_start_api(request):
    telegram_user = get_mini_app_user(request)
    if not telegram_user:
        return mini_json_error("Telegram authorization required", 401)
    payload = await request.json()
    try:
        bet = int(payload.get("bet", 0))
    except (TypeError, ValueError):
        return mini_json_error("Invalid bet")
    if bet <= 0:
        return mini_json_error("Invalid bet")
    user_id = telegram_user["id"]
    if user_id in mini_blackjack_games:
        return mini_json_error("Game already active")
    rows = supabase.table("users").select("casino_balance").eq("id", user_id).limit(1).execute().data
    balance = rows[0].get("casino_balance", 0) if rows else 0
    if bet > balance:
        return mini_json_error("Insufficient balance")
    game = {"bet": bet, "player_cards": [blackjack_card(), blackjack_card()], "dealer_cards": [blackjack_card(), blackjack_card()]}
    mini_blackjack_games[user_id] = game
    return web.json_response({"status": "playing", "player": game["player_cards"], "dealer": ["?", game["dealer_cards"][1]], "total": blackjack_total(game["player_cards"]), "bet": bet, "balance": balance})


async def mini_blackjack_action_api(request):
    telegram_user = get_mini_app_user(request)
    if not telegram_user:
        return mini_json_error("Telegram authorization required", 401)
    user_id = telegram_user["id"]
    game = mini_blackjack_games.get(user_id)
    if not game:
        return mini_json_error("No active game")
    action = (await request.json()).get("action")
    if action == "hit":
        game["player_cards"].append(blackjack_card())
        total = blackjack_total(game["player_cards"])
        if total <= 21:
            return web.json_response({"status": "playing", "player": game["player_cards"], "dealer": ["?", game["dealer_cards"][1]], "total": total, "bet": game["bet"]})
        result, change = "Перебір", -game["bet"]
    elif action == "stay":
        while blackjack_total(game["dealer_cards"]) < 17:
            game["dealer_cards"].append(blackjack_card())
        player_total = blackjack_total(game["player_cards"])
        dealer_total = blackjack_total(game["dealer_cards"])
        if dealer_total > 21 or player_total > dealer_total:
            result, change = "Перемога", game["bet"]
        elif player_total < dealer_total:
            result, change = "Поразка", -game["bet"]
        else:
            result, change = "Нічия", 0
    else:
        return mini_json_error("Unknown action")
    rows = supabase.table("users").select("casino_balance").eq("id", user_id).limit(1).execute().data
    balance = rows[0].get("casino_balance", 0) if rows else 0
    new_balance = balance + change
    supabase.table("users").update({"casino_balance": new_balance}).eq("id", user_id).execute()
    finished = {"status": "finished", "result": result, "change": change, "player": game["player_cards"], "dealer": game["dealer_cards"], "total": blackjack_total(game["player_cards"]), "balance": new_balance}
    mini_blackjack_games.pop(user_id, None)
    return web.json_response(finished)


async def mini_dice_play_api(request):
    telegram_user = get_mini_app_user(request)
    if not telegram_user:
        return mini_json_error("Telegram authorization required", 401)
    try:
        bet = int((await request.json()).get("bet", 0))
    except (TypeError, ValueError):
        return mini_json_error("Invalid bet")
    if bet <= 0:
        return mini_json_error("Invalid bet")
    user_id = telegram_user["id"]
    rows = supabase.table("users").select("casino_balance").eq("id", user_id).limit(1).execute().data
    balance = rows[0].get("casino_balance", 0) if rows else 0
    if bet > balance:
        return mini_json_error("Insufficient balance")
    player = [random.randint(1, 6), random.randint(1, 6)]
    dealer = [random.randint(1, 6), random.randint(1, 6)]
    player_total = sum(player)
    dealer_total = sum(dealer)
    if player_total > dealer_total:
        result, change = "Перемога", bet
    elif player_total < dealer_total:
        result, change = "Поразка", -bet
    else:
        result, change = "Нічия", 0
    new_balance = balance + change
    supabase.table("users").update({"casino_balance": new_balance}).eq("id", user_id).execute()
    return web.json_response({"player": player, "dealer": dealer, "player_total": player_total, "dealer_total": dealer_total, "result": result, "change": change, "balance": new_balance})


async def mini_slots_play_api(request):
    telegram_user = get_mini_app_user(request)
    if not telegram_user:
        return mini_json_error("Telegram authorization required", 401)
    try:
        bet = int((await request.json()).get("bet", 0))
    except (TypeError, ValueError):
        return mini_json_error("Invalid bet")
    if bet <= 0:
        return mini_json_error("Invalid bet")
    user_id = telegram_user["id"]
    rows = supabase.table("users").select("casino_balance").eq("id", user_id).limit(1).execute().data
    balance = rows[0].get("casino_balance", 0) if rows else 0
    if bet > balance:
        return mini_json_error("Insufficient balance")
    symbols = ["🍒", "🍋", "🍉", "🍇", "⭐"]
    reels = [random.choice(symbols) for _ in range(3)]
    if reels[0] == reels[1] == reels[2]:
        multiplier = 8 if reels[0] == "⭐" else 5
    elif len(set(reels)) == 2:
        multiplier = 2
    else:
        multiplier = 0
    payout = bet * multiplier
    change = payout - bet
    new_balance = balance + change
    supabase.table("users").update({"casino_balance": new_balance}).eq("id", user_id).execute()
    return web.json_response({
        "reels": reels,
        "multiplier": multiplier,
        "payout": payout,
        "change": change,
        "balance": new_balance,
    })


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", mini_app_handler)
    app.router.add_get("/health", lambda request: web.json_response({"status": "ok"}))
    app.router.add_get("/api/profile", mini_profile_api)
    app.router.add_post("/api/daily-bonus", mini_daily_bonus_api)
    app.router.add_post("/api/blackjack/start", mini_blackjack_start_api)
    app.router.add_post("/api/blackjack/action", mini_blackjack_action_api)
    app.router.add_post("/api/dice/play", mini_dice_play_api)
    app.router.add_post("/api/slots/play", mini_slots_play_api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    print(f"Mini App server запущено на порту {WEB_PORT}")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main():
    print("Бот запущено!")
    await asyncio.gather(
        dp.start_polling(bot),
        run_web_server(),
    )

if __name__ == "__main__":
    asyncio.run(main())