import os
import re
import time
import logging
import sqlite3

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes
)

# === Пути и логирование в папку проекта ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "bot.log")
DB_NAME = os.path.join(BASE_DIR, "bot.db")
ENV_FILE = os.path.join(BASE_DIR, ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# === Секреты из .env с фолбэком (готово к Railway: переменные окружения) ===
load_dotenv()
load_dotenv(ENV_FILE)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "481441820"))
SBP_PAYMENT_URL = os.getenv("SBP_PAYMENT_URL", "https://pay.platega.io/sbp_placeholder")
CARD_PAYMENT_URL = os.getenv("CARD_PAYMENT_URL", "https://pay.platega.io/card_placeholder")
CRYPTO_PAYMENT_URL = os.getenv("CRYPTO_PAYMENT_URL", "https://t.me/send?start=crypto_placeholder")

# === Настройки ===
DEFAULT_RATE = 1.4  # курс по умолчанию: 1.4₽ за одну звезду (меняется в админке)
MIN_STARS = 50
MAX_STARS = 50000
RATE_LIMIT_SECONDS = 1.0  # анти-флуд: не чаще одного действия в секунду

REVIEWS_LINK = "https://t.me/starsdefolt"

USERNAME_RE = re.compile(r"^@[a-zA-Z0-9_]{5,32}$")

MAIN_TEXT_TEMPLATE = """⚡ <b>Telegram Stars &amp; Premium — быстро и выгодно</b>

🔥 <b>Курс {rate}₽ за звезду</b> — дешевле, чем в других ботах!
🚀 <b>Моментально</b> — зачисление по @username за 1–2 минуты.
🛡 <b>Безопасно</b> — без входа в аккаунт и лишних данных.
💳 <b>Любая оплата</b> — СБП, карты РФ, криптовалюта.
💬 <b>Нам доверяют</b> — отзывы клиентов: @starsdefolt

👇 <b>Выберите нужный раздел:</b>"""


def get_rate() -> float:
    """Текущий курс из таблицы settings (ключ star_rate), иначе DEFAULT_RATE."""
    raw = get_setting('star_rate')
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        rate = DEFAULT_RATE
    return rate


def get_menu_emoji() -> tuple:
    """(custom_emoji_id, fallback_emoji) для акцента в заголовке меню."""
    emoji_id = get_setting('menu_emoji_id')
    fallback = get_setting('menu_emoji_fallback') or '⚡'
    return emoji_id, fallback


def main_text() -> str:
    emoji_id, fallback = get_menu_emoji()
    prefix = f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji> ' if emoji_id else ''

    # Кастомный текст приветствия из БД (задаётся в /admin → "Изменить текст приветствия")
    custom = get_setting('custom_welcome_text')
    if custom:
        body = custom.replace('{rate}', f'{get_rate():g}')
    else:
        body = MAIN_TEXT_TEMPLATE.format(rate=get_rate())
    return prefix + body


def main_keyboard():
    return [
        [InlineKeyboardButton("🌟 Купить Звёзды", callback_data='catalog_stars')],
        [InlineKeyboardButton("💎 Telegram Premium", callback_data='catalog_premium')],
        [InlineKeyboardButton("⭐ Отзывы покупателей", callback_data='reviews')]
    ]


def build_payment_url(base_url: str, order_id, total) -> str:
    """Персональная ссылка на страницу оплаты с ID заказа и суммой."""
    if not base_url:
        return "https://t.me/starsdefolt"
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}order_id={order_id}&amount={total}"


def is_rate_limited(context, user_id) -> bool:
    """Анти-флуд: True, если действие пришло слишком часто."""
    now = time.time()
    last = context.user_data.get('last_action_time', 0)
    if now - last < RATE_LIMIT_SECONDS:
        logger.warning("Анти-флуд: проигнорировано действие от id=%s", user_id)
        return True
    context.user_data['last_action_time'] = now
    return False


# === БД: настройки + заказы ===
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DROP TABLE IF EXISTS catalog")
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, text TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, buyer TEXT, recipient TEXT, stars INTEGER, total REAL, status TEXT)''')
    # Миграция: колонка buyer_id (для уведомлений покупателю)
    try:
        c.execute("ALTER TABLE orders ADD COLUMN buyer_id INTEGER")
        logger.info("Добавлена колонка orders.buyer_id")
    except sqlite3.OperationalError:
        pass  # колонка уже существует
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована: %s", DB_NAME)


def set_setting(key, value):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value)
    )
    conn.commit()
    conn.close()


def get_setting(key):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def delete_setting(key):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM settings WHERE key = ?", (key,))
    conn.commit()
    conn.close()


def buyer_name(user) -> str:
    if user is None:
        return "?"
    return f"@{user.username}" if user.username else f"id:{user.id}"


# === Меню ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info("Команда /start от пользователя id=%s (@%s)", user.id, user.username)
    context.user_data.clear()

    # Приветственный стикер (если владелец установил)
    sticker_id = get_setting('welcome_sticker')
    if sticker_id:
        try:
            await update.message.reply_sticker(sticker_id)
        except Exception as exc:
            logger.warning("Не удалось отправить приветственный стикер: %s", exc)

    await update.message.reply_text(
        main_text(),
        reply_markup=InlineKeyboardMarkup(main_keyboard()),
        parse_mode='HTML'
    )


# === Обработчики кнопок ===
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if is_rate_limited(context, user.id):
        return

    await query.answer()
    logger.info("Нажата кнопка '%s' пользователем id=%s (@%s)", query.data, user.id, user.username)

    handlers = {
        'catalog_stars': show_catalog_prompt,
        'catalog': show_catalog_prompt,  # совместимость со старыми меню
        'catalog_premium': show_premium_prompt,
        'reviews': show_reviews,
        'payment': show_payment,
        'back': back_to_menu,
        'stars_100': lambda q, c: select_stars(q, c, 100),
        'stars_1000': lambda q, c: select_stars(q, c, 1000),
        'stars_10000': lambda q, c: select_stars(q, c, 10000),
        'stars_custom': stars_custom,
        'prem_3_1390': lambda q, c: select_premium(q, c, 3, 1390),
        'prem_6_1850': lambda q, c: select_premium(q, c, 6, 1850),
        'prem_12_3190': lambda q, c: select_premium(q, c, 12, 3190),
        'payment_sbp': payment_sbp,
        'payment_card': payment_card,
        'payment_crypto': payment_crypto,
        'adm_orders': admin_orders,
        'adm_pending': admin_pending,
        'adm_stats': admin_stats,
        'adm_rate': admin_rate,
        'adm_emoji': admin_emoji,
        'adm_emoji_clear': admin_emoji_clear,
        'adm_edit_text': admin_edit_text,
        'adm_reset_text': admin_reset_text,
        'adm_pay_links': admin_pay_links,
        'adm_back': admin_back,
    }

    data = query.data or ''

    # Динамические callback'и с параметрами
    if data.startswith('paid_'):
        order_id = data.split('paid_', 1)[1]
        if order_id.isdigit():
            await customer_paid(query, context, int(order_id))
            return
    elif data.startswith('adm_approve_'):
        order_id = data.split('adm_approve_', 1)[1]
        if order_id.isdigit():
            await admin_approve(query, context, int(order_id))
            return
    elif data.startswith('adm_reject_'):
        order_id = data.split('adm_reject_', 1)[1]
        if order_id.isdigit():
            await admin_reject(query, context, int(order_id))
            return

    handler = handlers.get(data)
    if handler:
        await handler(query, context)


# === Блок 1: Звёзды ===
async def show_catalog_prompt(query, context):
    context.user_data['state'] = 'WAIT_USERNAME'
    keyboard = [
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.message.edit_text(
        "🌟 Введите ваш @username в Telegram:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state', '')
    user = update.effective_user

    # Премиум-эмодзи от владельца: сначала проверяем режим установки, потом эхо
    if (user.id == ADMIN_ID and update.message.entities
            and state not in ('WAIT_WELCOME_TEXT',)):
        custom_entities = [
            e for e in update.message.entities
            if e.type == 'custom_emoji' and getattr(e, 'custom_emoji_id', None)
        ]
        if custom_entities:
            cid = custom_entities[0].custom_emoji_id

            # Режим установки эмодзи в меню (ШАГ 1: состояние теперь срабатывает!)
            if state == 'WAIT_EMOJI':
                ent = custom_entities[0]
                raw = update.message.text or ''
                fallback = raw[ent.offset:ent.offset + ent.length] or '⚡'
                set_setting('menu_emoji_id', cid)
                set_setting('menu_emoji_fallback', fallback)
                context.user_data['state'] = ''
                logger.info("Админ %s установил премиум-эмодзи в меню: %s (фолбэк %s)",
                            user.id, cid, fallback)
                await update.message.reply_text(
                    "✅ <b>Премиум-эмодзи успешно установлен в заголовок меню!</b>\n\n"
                    f"ID: <code>{cid}</code>\n"
                    f"Запасной символ: {fallback}\n\n"
                    "Проверьте командой /start.",
                    parse_mode='HTML'
                )
                return

            logger.info("Админ %s прислал custom_emoji_id: %s", user.id, cid)
            await update.message.reply_text(f"custom_emoji_id: '{cid}'")
            return

    if is_rate_limited(context, user.id):
        return

    text_in = update.message.text.strip()
    logger.info("Текстовое сообщение от id=%s в состоянии '%s': %s", user.id, state, text_in)

    if state in ('WAIT_USERNAME', 'WAIT_USERNAME_PREMIUM'):
        username = text_in if text_in.startswith('@') else '@' + text_in
        if not USERNAME_RE.match(username):
            logger.warning("Некорректный username от id=%s: %s", user.id, text_in)
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back')]]
            await update.message.reply_text(
                "❌ Некорректный формат! Введите настоящий @username Telegram "
                "(только латинские буквы, цифры и _, от 5 до 32 символов).",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        context.user_data['recipient'] = username
        logger.info("Установлен получатель: %s", username)
        if state == 'WAIT_USERNAME':
            await show_star_options(update, context)
        else:
            await show_premium_tariffs(update, context)

    elif state == 'WAIT_NEW_RATE':
        if user.id != ADMIN_ID:
            await update.message.reply_text("❌ Введите число.")
            return
        try:
            new_rate = float(text_in.replace(',', '.'))
            if new_rate <= 0 or new_rate > 100:
                raise ValueError
        except ValueError:
            logger.warning("Админ %s ввёл некорректный курс: %s", user.id, text_in)
            await update.message.reply_text("❌ Введите положительное число (например 1.45):")
            return
        set_setting('star_rate', str(new_rate))
        context.user_data['state'] = ''
        logger.info("Админ %s изменил курс на %s₽", user.id, new_rate)
        await update.message.reply_text(
            f"✅ <b>Курс обновлён!</b>\n\n"
            f"💱 Новый курс: <b>{new_rate}₽</b> за звезду\n\n"
            "Он уже применяется во всём боте.",
            parse_mode='HTML'
        )

    elif state == 'WAIT_WELCOME_TEXT':
        if user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Нет прав.")
            return
        # text_html сохраняет жирный/курсив/ссылки и теги <tg-emoji>
        html = update.message.text_html
        if not html:
            await update.message.reply_text("❌ Отправьте текстовое сообщение.")
            return
        set_setting('custom_welcome_text', html)
        context.user_data['state'] = ''
        logger.info("Админ %s обновил текст приветствия (%s символов)", user.id, len(html))
        await update.message.reply_text(
            "✅ <b>Текст приветствия обновлён!</b>\n\n"
            "Проверьте командой /start. Кнопка «🔄 Сбросить текст по умолчанию» "
            "вернёт стандартный шаблон.",
            parse_mode='HTML'
        )

    elif state == 'WAIT_PAY_URL':
        if user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Нет прав.")
            return
        parts = text_in.split(maxsplit=1)
        if len(parts) != 2 or parts[0] not in ('1', '2', '3') or not parts[1].startswith('http'):
            await update.message.reply_text(
                "❌ Неверный формат. Пример:\n<code>1 https://pay.example.ru/sbp</code>",
                parse_mode='HTML'
            )
            return
        keys = {'1': 'sbp_url', '2': 'card_url', '3': 'crypto_url'}
        names = {'1': 'СБП', '2': 'карта РФ', '3': 'криптовалюта'}
        key = keys[parts[0]]
        set_setting(key, parts[1].strip())
        context.user_data['state'] = ''
        logger.info("Админ %s обновил ссылку оплаты %s: %s", user.id, key, parts[1])
        await update.message.reply_text(
            f"✅ <b>Ссылка для «{names[parts[0]]}» обновлена!</b>\n\n<code>{parts[1]}</code>",
            parse_mode='HTML'
        )

    elif state == 'WAIT_STARS':
        try:
            count = int(text_in)
        except ValueError:
            logger.warning("Введено не число от id=%s: %s", user.id, text_in)
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back')]]
            await update.message.reply_text(
                "❌ Введите число.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        if count < MIN_STARS or count > MAX_STARS or count % 2 != 0:
            logger.warning("Некорректное число звёзд от id=%s: %s", user.id, count)
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back')]]
            await update.message.reply_text(
                f"❌ Введите чётное число от {MIN_STARS} до {MAX_STARS}:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        context.user_data['stars'] = count
        await calculate_cost(update, context)


async def show_star_options(query_or_update, context):
    username = context.user_data.get('recipient', '@----')
    rate = get_rate()
    keyboard = [
        [InlineKeyboardButton(f"🌟 100 звёзд ({round(100 * rate, 2):g}₽)", callback_data='stars_100')],
        [InlineKeyboardButton(f"🌟 1000 звёзд ({round(1000 * rate, 2):g}₽)", callback_data='stars_1000')],
        [InlineKeyboardButton(f"🌟 10000 звёзд ({round(10000 * rate, 2):g}₽)", callback_data='stars_10000')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]

    text = (
        f"✅ Получатель: {username}\n\n"
        "Выберите готовый пакет или укажите любое количество звёзд "
        f"(чётное число, от {MIN_STARS} до {MAX_STARS}) ответом в чат "
        f"(курс: {get_rate()}₽/звезда):"
    )

    context.user_data['state'] = 'WAIT_STARS'

    if hasattr(query_or_update, 'edit_message_text'):
        await query_or_update.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif hasattr(query_or_update, 'message') and query_or_update.message:
        await query_or_update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await query_or_update.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def select_stars(query, context, count):
    context.user_data['stars'] = count
    await calculate_cost(query, context)


async def stars_custom(query, context):
    context.user_data['state'] = 'WAIT_STARS'
    keyboard = [
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.message.edit_text(
        f"Укажите любое количество звёзд (чётное, от {MIN_STARS} до {MAX_STARS}):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def calculate_cost(update_or_query, context):
    stars = context.user_data.get('stars', 100)
    recipient = context.user_data.get('recipient', '@----')
    rate = get_rate()
    total = round(stars * rate, 2)
    context.user_data['total'] = total
    context.user_data['state'] = ''
    context.user_data['item_title'] = f"Звёзды: {stars}x{rate}₽"

    # Определяем реального покупателя
    user = getattr(update_or_query, 'effective_user', None)
    if user is None:
        user = getattr(update_or_query, 'from_user', None)
    buyer = buyer_name(user)
    buyer_id = user.id if user else None

    # Сохраняем заказ в БД
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT INTO orders (buyer, buyer_id, recipient, stars, total, status) VALUES (?, ?, ?, ?, ?, ?)",
        (buyer, buyer_id, recipient, stars, total, "created")
    )
    order_id = c.lastrowid
    conn.commit()
    conn.close()

    context.user_data['order_id'] = order_id

    logger.info(
        "Расчёт заказа #%s: покупатель=%s, получатель=%s, звёзд=%s, курс=%s₽, сумма=%s₽",
        order_id, buyer, recipient, stars, rate, total
    )

    text = f"""🌟 <b>Информация о заказе</b> #{order_id}

👤 Получатель: <code>{recipient}</code>
🔢 Количество звёзд: {stars}
💱 Курс: {rate}₽ за звезду
💰 <b>Стоимость: {total}₽</b>"""

    keyboard = [
        [InlineKeyboardButton("🔄 Изменить количество", callback_data='stars_custom')],
        [InlineKeyboardButton("💳 Перейти к оплате", callback_data='payment')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]

    if hasattr(update_or_query, 'edit_message_text'):
        await update_or_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    elif hasattr(update_or_query, 'message') and update_or_query.message:
        await update_or_query.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else:
        await update_or_query.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# === Блок 2: Telegram Premium ===
async def show_premium_prompt(query, context):
    context.user_data['state'] = 'WAIT_USERNAME_PREMIUM'
    keyboard = [
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.message.edit_text(
        "💎 Введите @username получателя Telegram Premium:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_premium_tariffs(query_or_update, context):
    username = context.user_data.get('recipient', '@----')
    keyboard = [
        [InlineKeyboardButton("💎 3 месяца — 1390₽", callback_data='prem_3_1390')],
        [InlineKeyboardButton("💎 6 месяцев — 1850₽", callback_data='prem_6_1850')],
        [InlineKeyboardButton("🔥 12 месяцев — 3190₽ (Выгодно)", callback_data='prem_12_3190')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]

    text = f"✅ Получатель: {username}\n\n💎 Выберите тариф Telegram Premium:"
    context.user_data['state'] = ''

    if hasattr(query_or_update, 'edit_message_text'):
        await query_or_update.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif hasattr(query_or_update, 'message') and query_or_update.message:
        await query_or_update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await query_or_update.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def select_premium(query, context, months, price):
    recipient = context.user_data.get('recipient', '@----')
    context.user_data['total'] = price
    context.user_data['state'] = ''
    context.user_data['item_title'] = f"Telegram Premium на {months} мес."
    context.user_data['months'] = months

    # Определяем реального покупателя
    user = getattr(query, 'from_user', None)
    buyer = buyer_name(user)
    buyer_id = user.id if user else None

    # Сохраняем заказ в БД (stars=0, тип в status)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT INTO orders (buyer, buyer_id, recipient, stars, total, status) VALUES (?, ?, ?, ?, ?, ?)",
        (buyer, buyer_id, recipient, 0, price, f"premium_{months}m")
    )
    order_id = c.lastrowid
    conn.commit()
    conn.close()

    context.user_data['order_id'] = order_id

    logger.info(
        "Заказ Premium #%s: покупатель=%s, получатель=%s, тариф=%s мес., сумма=%s₽",
        order_id, buyer, recipient, months, price
    )

    text = f"""💎 <b>Информация о заказе</b> #{order_id}

👤 Получатель: <code>{recipient}</code>
📦 Тариф: Telegram Premium на {months} мес.
💰 <b>Стоимость: {price}₽</b>"""

    keyboard = [
        [InlineKeyboardButton("💳 Перейти к оплате", callback_data='payment')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# === Блок 3: Отзывы ===
async def show_reviews(query, context):
    text = """⭐ <b>Отзывы покупателей</b>

Нажмите кнопку ниже, чтобы перейти на наш канал с отзывами:"""
    keyboard = [
        [InlineKeyboardButton("📢 Перейти к отзывам", url=REVIEWS_LINK)],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# === Блок 4: Оплата (только внутри оформленного заказа) ===
async def show_payment(query, context):
    total = context.user_data.get('total')
    order_id = context.user_data.get('order_id')

    if not total or not order_id:
        text = ("💳 <b>Оплата</b>\n\n"
                "Сначала оформите заказ: выберите количество звёзд или тариф Telegram Premium.")
        keyboard = [
            [InlineKeyboardButton("🔙 Назад", callback_data='back')]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return

    item_title = context.user_data.get('item_title', '')
    text = f"""💳 <b>Оплата</b>

🧾 Заказ #{order_id}
📦 {item_title}
💰 <b>Сумма к оплате: {total}₽</b>

Выберите удобный способ оплаты — вся оплата проходит на защищённой странице кассы:"""

    keyboard = [
        [InlineKeyboardButton("💸 СБП (Система Быстрых Платежей)", callback_data='payment_sbp')],
        [InlineKeyboardButton("💳 Банковская карта (РФ)", callback_data='payment_card')],
        [InlineKeyboardButton("💎 Криптовалюта (USDT / TON / BTC)", callback_data='payment_crypto')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


def get_payment_url(setting_key: str, env_default: str) -> str:
    """Ссылка оплаты: сначала settings в БД (задаётся в /admin), потом переменная окружения."""
    return get_setting(setting_key) or env_default


async def payment_sbp(query, context):
    total = context.user_data.get('total', 0)
    order_id = context.user_data.get('order_id', 0)
    url = build_payment_url(get_payment_url('sbp_url', SBP_PAYMENT_URL), order_id, total)
    logger.info("Оплата СБП: заказ=%s, сумма=%s₽, ссылка=%s", order_id, total, url)

    keyboard = [
        [InlineKeyboardButton(f"💳 Перейти на страницу оплаты ({total}₽)", url=url)],
        [InlineKeyboardButton("✅ Я оплатил", callback_data=f"paid_{order_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data='payment')]
    ]
    await query.edit_message_text(
        f"💸 <b>Оплата через СБП</b>\n\n💰 Сумма: {total}₽\n\n"
        "Нажмите кнопку ниже, чтобы перейти на защищённую страницу оплаты.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def payment_card(query, context):
    total = context.user_data.get('total', 0)
    order_id = context.user_data.get('order_id', 0)
    url = build_payment_url(get_payment_url('card_url', CARD_PAYMENT_URL), order_id, total)
    logger.info("Оплата картой РФ: заказ=%s, сумма=%s₽, ссылка=%s", order_id, total, url)

    keyboard = [
        [InlineKeyboardButton(f"💳 Перейти на страницу оплаты ({total}₽)", url=url)],
        [InlineKeyboardButton("✅ Я оплатил", callback_data=f"paid_{order_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data='payment')]
    ]
    await query.edit_message_text(
        f"💳 <b>Оплата банковской картой (РФ)</b>\n\n💰 Сумма: {total}₽\n\n"
        "Нажмите кнопку ниже, чтобы перейти на защищённую страницу оплаты.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def payment_crypto(query, context):
    total = context.user_data.get('total', 0)
    order_id = context.user_data.get('order_id', 0)
    url = build_payment_url(get_payment_url('crypto_url', CRYPTO_PAYMENT_URL), order_id, total)
    logger.info("Оплата криптовалютой: заказ=%s, сумма=%s₽, ссылка=%s", order_id, total, url)

    keyboard = [
        [InlineKeyboardButton(f"💳 Перейти на страницу оплаты ({total}₽)", url=url)],
        [InlineKeyboardButton("✅ Я оплатил", callback_data=f"paid_{order_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data='payment')]
    ]
    await query.edit_message_text(
        f"💎 <b>Оплата криптовалютой (USDT / TON / BTC)</b>\n\n💰 Сумма: {total}₽\n\n"
        "Нажмите кнопку ниже, чтобы перейти на защищённую страницу оплаты.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# === Стикер владельца + /remove_sticker ===
async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        return

    if is_rate_limited(context, user.id):
        return

    sticker = update.message.sticker
    set_setting('welcome_sticker', sticker.file_id)
    logger.info("Админ %s установил приветственный стикер: %s", user.id, sticker.file_id)
    await update.message.reply_text(
        "✅ Приветственный стикер обновлён! Теперь он будет отправляться при каждом /start."
    )


async def remove_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ У вас нет прав администратора.")
        return

    delete_setting('welcome_sticker')
    logger.info("Админ %s удалил приветственный стикер", user.id)
    await update.message.reply_text("🗑 Приветственный стикер удалён.")


# === Заказы: хелперы ===
def get_order(order_id):
    """Возвращает dict заказа или None."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT id, buyer, buyer_id, recipient, stars, total, status FROM orders WHERE id = ?",
        (order_id,)
    )
    row = c.fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def set_order_status(order_id, status):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()


def free_text_of(row) -> str:
    """Товар заказа: звёзды или Premium-тариф (по status)."""
    status = row.get('status') or ''
    if status.startswith('premium_'):
        months = status.replace('premium_', '').replace('m', '')
        return f"Telegram Premium на {months} мес."
    return f"Звёзды: {row.get('stars')}"


def order_card(row, with_admin_buttons=False):
    text = (
        f"🧾 <b>Заказ #{row['id']}</b>\n\n"
        f"👤 Покупатель: {row['buyer']}\n"
        f"🎯 Получатель: <code>{row['recipient']}</code>\n"
        f"📦 Товар: {free_text_of(row)}\n"
        f"💰 Сумма: <b>{row['total']}₽</b>\n"
        f"📌 Статус: {row['status']}"
    )
    if with_admin_buttons:
        keyboard = [
            [InlineKeyboardButton("✅ Выполнено (Выдано)", callback_data=f"adm_approve_{row['id']}")],
            [InlineKeyboardButton("❌ Отклонить (Не оплачено)", callback_data=f"adm_reject_{row['id']}")]
        ]
        return text, InlineKeyboardMarkup(keyboard)
    return text, None


# === Покупатель нажал «Я оплатил» ===
async def customer_paid(query, context, order_id):
    row = get_order(order_id)
    if row is None:
        await query.edit_message_text("❌ Заказ не найден.")
        return

    set_order_status(order_id, 'waiting_check')
    logger.info("Заказ #%s отправлен на проверку (waiting_check)", order_id)

    await query.edit_message_text(
        "⏳ <b>Заявка на проверку оплаты отправлена!</b>\n"
        "Администратор проверит поступление средств и начислит ваш заказ в ближайшее время.",
        parse_mode='HTML'
    )

    # Уведомление владельцу
    admin_text = (
        "🔔 <b>Новый заказ ожидает проверки!</b>\n\n"
        f"🧾 Заказ: #{order_id}\n"
        f"👤 Покупатель: {row['buyer']}\n"
        f"🎯 Получатель: <code>{row['recipient']}</code>\n"
        f"📦 Товар: {free_text_of(row)}\n"
        f"💰 Сумма: <b>{row['total']}₽</b>"
    )
    keyboard = [
        [InlineKeyboardButton("✅ Выполнено (Выдано)", callback_data=f"adm_approve_{order_id}")],
        [InlineKeyboardButton("❌ Отклонить (Не оплачено)", callback_data=f"adm_reject_{order_id}")]
    ]
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    except Exception as exc:
        logger.error("Не удалось уведомить админа о заказе #%s: %s", order_id, exc)


# === Админ: подтвердить / отклонить ===
async def admin_approve(query, context, order_id):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    row = get_order(order_id)
    if row is None:
        await query.edit_message_text("❌ Заказ не найден.")
        return

    set_order_status(order_id, 'completed')
    logger.info("Админ подтвердил заказ #%s (completed)", order_id)

    await query.edit_message_text(
        f"✅ <b>Заказ #{order_id} выполнен!</b>\n\n"
        f"🎯 Получатель: <code>{row['recipient']}</code>\n"
        f"📦 {free_text_of(row)}\n"
        f"💰 {row['total']}₽",
        parse_mode='HTML'
    )

    if row.get('buyer_id'):
        try:
            await context.bot.send_message(
                chat_id=row['buyer_id'],
                text=(
                    f"🎉 <b>Ваш заказ #{order_id} успешно выполнен!</b>\n"
                    "Спасибо за покупку! Будем рады вашему отзыву: @starsdefolt"
                ),
                parse_mode='HTML'
            )
        except Exception as exc:
            logger.error("Не удалось уведомить покупателя заказа #%s: %s", order_id, exc)


async def admin_reject(query, context, order_id):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    row = get_order(order_id)
    if row is None:
        await query.edit_message_text("❌ Заказ не найден.")
        return

    set_order_status(order_id, 'rejected')
    logger.info("Админ отклонил заказ #%s (rejected)", order_id)

    await query.edit_message_text(
        f"❌ <b>Заказ #{order_id} отклонён.</b>",
        parse_mode='HTML'
    )

    if row.get('buyer_id'):
        try:
            await context.bot.send_message(
                chat_id=row['buyer_id'],
                text=(
                    f"❌ Оплата по заказу #{order_id} не найдена. "
                    "Если вы совершили перевод, свяжитесь с поддержкой."
                )
            )
        except Exception as exc:
            logger.error("Не удалось уведомить покупателя заказа #%s: %s", order_id, exc)


# === Админ-панель ===
def admin_menu_keyboard():
    return [
        [InlineKeyboardButton("📋 Последние 10 заказов", callback_data='adm_orders')],
        [InlineKeyboardButton("⏳ Заказы на проверке", callback_data='adm_pending')],
        [InlineKeyboardButton("📊 Статистика бота", callback_data='adm_stats')],
        [InlineKeyboardButton("💱 Изменить курс звезды", callback_data='adm_rate')],
        [InlineKeyboardButton("📝 Изменить текст приветствия", callback_data='adm_edit_text')],
        [InlineKeyboardButton("🔄 Сбросить текст по умолчанию", callback_data='adm_reset_text')],
        [InlineKeyboardButton("💳 Настроить ссылки оплаты", callback_data='adm_pay_links')],
        [InlineKeyboardButton("✨ Премиум-эмодзи в меню", callback_data='adm_emoji')]
    ]


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ У вас нет прав администратора.")
        return

    logger.info("Админ %s открыл админ-панель", user.id)
    context.user_data['state'] = ''
    await update.message.reply_text(
        f"🛠 <b>Админ-панель</b>\n\n"
        f"💱 Текущий курс: <b>{get_rate()}₽</b> за звезду\n\n"
        "Выберите раздел:",
        reply_markup=InlineKeyboardMarkup(admin_menu_keyboard()),
        parse_mode='HTML'
    )


async def admin_back(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return
    await query.edit_message_text(
        f"🛠 <b>Админ-панель</b>\n\n"
        f"💱 Текущий курс: <b>{get_rate()}₽</b> за звезду\n\n"
        "Выберите раздел:",
        reply_markup=InlineKeyboardMarkup(admin_menu_keyboard()),
        parse_mode='HTML'
    )


async def admin_orders(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT id, buyer, buyer_id, recipient, stars, total, status "
        "FROM orders ORDER BY id DESC LIMIT 10"
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    if not rows:
        text = "📋 <b>Последние заказы</b>\n\nЗаказов пока нет."
    else:
        lines = ["📋 <b>Последние 10 заказов</b>\n"]
        for r in rows:
            lines.append(
                f"#{r['id']} — <code>{r['recipient']}</code>\n"
                f"   📦 {free_text_of(r)} | 💰 {r['total']}₽ | 📌 {r['status']}"
            )
        text = "\n".join(lines)

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='adm_back')]]),
        parse_mode='HTML'
    )


async def admin_pending(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT id, buyer, buyer_id, recipient, stars, total, status "
        "FROM orders WHERE status = 'waiting_check' ORDER BY id ASC"
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    if not rows:
        await query.edit_message_text(
            "⏳ <b>Заказы на проверке</b>\n\nНет заказов, ожидающих проверки.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='adm_back')]]),
            parse_mode='HTML'
        )
        return

    lines = ["⏳ <b>Заказы на проверке</b>\n"]
    for r in rows:
        lines.append(
            f"#{r['id']} — <code>{r['recipient']}</code>\n"
            f"   📦 {free_text_of(r)} | 💰 {r['total']}₽"
        )
    text = "\n".join(lines)

    keyboard = []
    for r in rows:
        keyboard.append([
            InlineKeyboardButton(f"✅ #{r['id']}", callback_data=f"adm_approve_{r['id']}"),
            InlineKeyboardButton(f"❌ #{r['id']}", callback_data=f"adm_reject_{r['id']}")
        ])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='adm_back')])

    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def admin_stats(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    total_count = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    completed = c.execute("SELECT COUNT(*) FROM orders WHERE status = 'completed'").fetchone()[0]
    pending = c.execute("SELECT COUNT(*) FROM orders WHERE status = 'waiting_check'").fetchone()[0]
    revenue = c.execute("SELECT COALESCE(SUM(total), 0) FROM orders WHERE status = 'completed'").fetchone()[0]
    conn.close()

    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"🧾 Всего заказов: <b>{total_count}</b>\n"
        f"✅ Выполнено: <b>{completed}</b>\n"
        f"⏳ На проверке: <b>{pending}</b>\n"
        f"💰 Выручка: <b>{round(revenue, 2)}₽</b>"
    )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='adm_back')]]),
        parse_mode='HTML'
    )


async def admin_rate(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    context.user_data['state'] = 'WAIT_NEW_RATE'
    await query.edit_message_text(
        f"💱 <b>Изменение курса звезды</b>\n\n"
        f"Текущий курс: <b>{get_rate()}₽</b> за звезду\n\n"
        "Отправьте новое значение в чат (например: <code>1.45</code>):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='adm_back')]]),
        parse_mode='HTML'
    )


async def admin_emoji(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    context.user_data['state'] = 'WAIT_EMOJI'
    emoji_id, fallback = get_menu_emoji()
    current = f"<code>{emoji_id}</code>" if emoji_id else "не задан"

    keyboard = [
        [InlineKeyboardButton("🗑 Убрать эмодзи", callback_data='adm_emoji_clear')],
        [InlineKeyboardButton("🔙 Назад", callback_data='adm_back')]
    ]
    await query.edit_message_text(
        f"✨ <b>Премиум-эмодзи в заголовке меню</b>\n\n"
        f"Текущий: {current}\n\n"
        "Отправьте в чат сообщение с <b>кастомным премиум-эмодзи</b> — "
        "бот возьмёт его ID и поставит в начало главного экрана.\n\n"
        "⚠️ Требуется Telegram Premium у владельца бота.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def admin_emoji_clear(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    delete_setting('menu_emoji_id')
    delete_setting('menu_emoji_fallback')
    context.user_data['state'] = ''
    logger.info("Админ %s убрал премиум-эмодзи из меню", query.from_user.id)
    await query.edit_message_text(
        "🗑 Премиум-эмодзи убран из заголовка меню.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='adm_back')]]),
        parse_mode='HTML'
    )


async def admin_edit_text(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    context.user_data['state'] = 'WAIT_WELCOME_TEXT'
    await query.edit_message_text(
        "📝 <b>Редактор стартового сообщения</b>\n\n"
        "Отправьте в чат новый текст приветствия именно в том виде, "
        "как его должны видеть покупатели.\n\n"
        "💡 Вы можете использовать прямо в Telegram: <b>жирный шрифт</b>, "
        "<i>курсив</i>, ссылки и любые <b>Премиум-эмодзи</b>!\n"
        "Чтобы в тексте автоматически подставлялся актуальный курс, "
        "напишите <code>{rate}</code> (или просто укажите цифру).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='adm_back')]]),
        parse_mode='HTML'
    )


async def admin_reset_text(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    delete_setting('custom_welcome_text')
    context.user_data['state'] = ''
    logger.info("Админ %s сбросил текст приветствия на стандартный", query.from_user.id)
    await query.edit_message_text(
        "🔄 Текст приветствия сброшен на стандартный шаблон.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='adm_back')]]),
        parse_mode='HTML'
    )


async def admin_pay_links(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    sbp = get_payment_url('sbp_url', SBP_PAYMENT_URL)
    card = get_payment_url('card_url', CARD_PAYMENT_URL)
    crypto = get_payment_url('crypto_url', CRYPTO_PAYMENT_URL)
    context.user_data['state'] = 'WAIT_PAY_URL'

    await query.edit_message_text(
        "💳 <b>Настройка ссылок оплаты</b>\n\n"
        f"1️⃣ СБП: <code>{sbp}</code>\n"
        f"2️⃣ Карта РФ: <code>{card}</code>\n"
        f"3️⃣ Крипта: <code>{crypto}</code>\n\n"
        "Отправьте в чат новую ссылку в формате:\n"
        "<code>1 https://pay.example.ru/sbp</code>\n\n"
        "Где 1 = СБП, 2 = карта РФ, 3 = крипта.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='adm_back')]]),
        parse_mode='HTML'
    )


# === Возврат в меню ===
async def back_to_menu(query, context):
    context.user_data.clear()
    await query.edit_message_text(
        main_text(),
        reply_markup=InlineKeyboardMarkup(main_keyboard()),
        parse_mode='HTML'
    )


# === Main ===
def main():
    init_db()
    logger.info("Администратор (ADMIN_ID): %s", ADMIN_ID)

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("remove_sticker", remove_sticker))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен и готов к работе...")
    app.run_polling()


if __name__ == '__main__':
    main()