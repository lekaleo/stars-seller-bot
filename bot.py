import os
import re
import html
import json
import time
import bisect
import asyncio
import logging
import sqlite3

from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from urllib.parse import quote, urlencode

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes
)

# === Пути и логирование в папку проекта ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "bot.log")
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

# Путь к БД: на облачных хостингах можно указать DB_PATH (например, на persistent disk),
# иначе — bot.db рядом с bot.py
DB_NAME = os.getenv("DB_PATH") or os.path.join(BASE_DIR, "bot.db")

# PostgreSQL (бесплатный Neon) — постоянное хранение заказов и настроек в облаке.
# Если DATABASE_URL не задан, бот работает на локальном SQLite (как раньше).
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
DB_IS_POSTGRES = bool(DATABASE_URL)

try:
    import psycopg  # требуется только при DATABASE_URL
except ImportError:  # локальный запуск без psycopg — норм
    psycopg = None

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "481441820"))
SBP_PAYMENT_URL = os.getenv("SBP_PAYMENT_URL", "https://pay.platega.io/sbp_placeholder")
CARD_PAYMENT_URL = os.getenv("CARD_PAYMENT_URL", "https://pay.platega.io/card_placeholder")
CRYPTO_PAYMENT_URL = os.getenv("CRYPTO_PAYMENT_URL", "https://t.me/send?start=crypto_placeholder")

# Fragment (автовыдача звёзд): доступ задаётся в /admin → 🤖 Автовыдача Fragment
# (settings: fragment_api_key / fragment_hash / fragment_cookies / fragment_ton_mnemonic)
# либо переменными окружения FRAGMENT_API_KEY / FRAGMENT_HASH / FRAGMENT_COOKIES.
# Значения читаются через fragment_conf() — код и дефолты от них не зависят.

# === Настройки ===
DEFAULT_RATE = 1.4  # курс по умолчанию: 1.4₽ за одну звезду (меняется в админке)
MIN_STARS = 50
MAX_STARS = 50000
RATE_LIMIT_SECONDS = 1.0  # анти-флуд: не чаще одного действия в секунду

REVIEWS_LINK = "https://t.me/starsdefolt"

USERNAME_RE = re.compile(r"^@[a-zA-Z][a-zA-Z0-9_]{4,31}$")

# Заглушки-примеры, которые нельзя принимать за настоящий ник
FORBIDDEN_USERNAMES = {
    '@username', '@user', '@admin', '@telegram', '@support', '@test', '@bot',
    '@starsdefolt', '@durov',
}

# Юзернейм самого бота (заполняется через get_me() при старте)
BOT_USERNAME = (os.getenv('BOT_USERNAME') or '').strip()


def normalize_username(raw: str) -> str:
    value = (raw or '').strip()
    if value and not value.startswith('@'):
        value = '@' + value
    return value


def validate_username(raw: str):
    """Проверка @username по правилам Telegram. Возвращает (username | None, текст ошибки | None)."""
    username = normalize_username(raw)
    low = username.lower()

    if low in FORBIDDEN_USERNAMES:
        return None, ("❌ Вы ввели слово-пример «@username» вместо настоящего ника! "
                      "Укажите реальный @никнейм аккаунта Telegram, куда нужно начислить покупку.")
    if BOT_USERNAME and low == '@' + BOT_USERNAME.lstrip('@').lower():
        return None, ("❌ Это юзернейм самого бота — укажите @никнейм аккаунта, "
                      "куда нужно начислить покупку.")
    if username.endswith('_') or '__' in username:
        return None, ("❌ Юзернейм не может заканчиваться на «_» или содержать два «_» подряд.\n"
                      "Введите настоящий @никнейм аккаунта Telegram.")
    if not USERNAME_RE.match(username):
        return None, ("❌ Некорректный формат! Введите настоящий @username Telegram\n"
                      "(начинается с латинской буквы, 5–32 символа: латинские буквы, цифры и _).")
    return username, None


def verify_telegram_username(username: str) -> bool:
    """Проверяет, что аккаунт существует: у реальной страницы t.me есть og:title/tgme_page_title.

    При сетевой ошибке возвращает True (не блокируем продажу из-за нашего таймаута),
    при 404 — False (аккаунта нет).
    """
    import urllib.error as _urlerr
    import urllib.request as _urlreq

    name = (username or '').lstrip('@').strip()
    if not name:
        return False
    url = f"https://t.me/{name}"
    request = _urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; StarsBot/1.0)'})
    try:
        with _urlreq.urlopen(request, timeout=10) as response:
            page = response.read(60000).decode('utf-8', errors='replace')
        if 'tgme_page_title' in page or 'property="og:title"' in page or "property='og:title'" in page:
            return True
        logger.warning("Проверка @%s: страница без og:title — аккаунт не найден", name)
        return False
    except _urlerr.HTTPError as exc:
        if exc.code == 404:
            logger.warning("Проверка @%s: 404 — аккаунта нет", name)
            return False
        logger.warning("Проверка @%s: HTTP %s — считаем аккаунт существующим", name, exc.code)
        return True
    except Exception as exc:
        logger.warning("Проверка @%s не удалась (%s) — не блокируем заказ", name, exc)
        return True

# === Редактируемые тексты всех экранов (стандартные = утверждённые владельцем 28.10.2026) ===
TEXT_DEFAULTS = {
    'welcome': ('<tg-emoji emoji-id="5875465628285931233">✈️</tg-emoji><b>Добро пожаловать!</b>\n'
                '<b><i>Здесь вы можете быстро приобрести Telegram Stars и Premium подписку на свой аккаунт</i></b>\n'
                '<blockquote><tg-emoji emoji-id="5936259309812846957">⭐️</tg-emoji><b>{rate}₽/звезда</b>.\n'
                '<b><tg-emoji emoji-id="5778202206922608769">🔄</tg-emoji></b><b>1–2 минуты по </b><b>@username</b><b>.</b>\n'
                '<tg-emoji emoji-id="6019523512908124649">🔒</tg-emoji><b> Без входа в аккаунт.</b></blockquote>\n'
                '<tg-emoji emoji-id="5874960879434338403">🔎</tg-emoji> <b>Выберите нужный раздел:</b>'),
    'stars_prompt': '<tg-emoji emoji-id="5879841310902324730">✏️</tg-emoji> Введите ваш @username в <b>Telegram</b>:',
    'stars_options': ('<tg-emoji emoji-id="5936259309812846957">⭐️</tg-emoji> <b>Покупка Telegram stars</b> \n\n'
                      '<tg-emoji emoji-id="5954175920506933873">👤</tg-emoji> Получатель: {recipient}\n \n'
                      '<blockquote>Выберите готовый пакет или укажите любое кол-во звезд текстом '
                      '<tg-emoji emoji-id="5877307202888273539">📥</tg-emoji>(от {min} до {max})</blockquote>'),
    'stars_custom': "Укажите любое количество звёзд (чётное, от {min} до {max}):",
    'stars_order': ('<tg-emoji emoji-id="5936259309812846957">⭐️</tg-emoji> Информация о заказе {order_id}\n\n'
                    '<tg-emoji emoji-id="5879770735999717115">👤</tg-emoji> Получатель: {recipient}\n'
                    '<tg-emoji emoji-id="5958413064658228696">⭐️</tg-emoji> Количество звезд: {stars} \n'
                    '<tg-emoji emoji-id="5778318458802409852">💰</tg-emoji> Курс: {rate}₽ за звезду \n'
                    '<tg-emoji emoji-id="5811989245761426317">💰</tg-emoji> Стоимость: {total}₽'),
    'premium_prompt': '<tg-emoji emoji-id="5989981173068077484">⭐️</tg-emoji> Введите @username получателя Telegram Premium:',
    'premium_tariffs': ('<tg-emoji emoji-id="5879770735999717115">👤</tg-emoji> Получатель: {recipient}\n\n'
                        '<tg-emoji emoji-id="5839394435644788150">⭐️</tg-emoji> Выберите тариф Telegram Premium:'),
    'premium_order': ('<tg-emoji emoji-id="5989981173068077484">⭐️</tg-emoji> Информация о заказе #{order_id} \n\n'
                      '<tg-emoji emoji-id="5954175920506933873">👤</tg-emoji> Получатель: {recipient} \n'
                      '<tg-emoji emoji-id="5839116473951328489">⭐️</tg-emoji> Тариф: Telegram Premium на {months} мес.\n'
                      '<tg-emoji emoji-id="5811989245761426317">💰</tg-emoji> Стоимость: {total}₽'),
    'reviews_text': ('<tg-emoji emoji-id="5884510167986343350">💬</tg-emoji><b> Отзывы покупателей </b>\n'
                     '<blockquote>Нажмите кнопку ниже, чтобы перейти на наш канал с отзывами: @starsdefolt</blockquote>'),
    'payment_menu': ('<tg-emoji emoji-id="5927169041595634481">💳</tg-emoji> <b>Оплата </b>\n\n'
                     '<tg-emoji emoji-id="5967548335542767952">💳</tg-emoji> Заказ #{order_id}\n'
                     '<tg-emoji emoji-id="5985472565508838112">🎁</tg-emoji> {item_title} \n'
                     '<tg-emoji emoji-id="5811989245761426317">💰</tg-emoji> Сумма к оплате: {total}₽'),
    'payment_sbp_text': ('<tg-emoji emoji-id="5927169041595634481">💳</tg-emoji><b> Оплата СБП </b>\n\n'
                         '<tg-emoji emoji-id="5811989245761426317">💰</tg-emoji> Сумма: {total}₽'),
    'payment_card_text': ('<tg-emoji emoji-id="5927169041595634481">💳</tg-emoji><b> Оплата банковской картой (РФ) </b>\n\n'
                          '<tg-emoji emoji-id="5811989245761426317">💰</tg-emoji> Сумма: {total}₽'),
    'payment_crypto_text': ('<tg-emoji emoji-id="5807499888245612254">💎</tg-emoji><b> Оплата криптовалютой (USDT / TON)</b> \n\n'
                            '<tg-emoji emoji-id="5811989245761426317">💰</tg-emoji> Сумма: {total}₽'),
    'paid_wait_text': ('<tg-emoji emoji-id="5778202206922608769">🔄</tg-emoji> <b>Заявка на проверку оплаты отправлена! </b>\n'
                       '<blockquote>Администратор проверит поступление средств и начислит ваш заказ в ближайшее время</blockquote>'),
}

# Доступные переменные по экранам (для подсказок в редакторе)
SCREEN_VARS = {
    'welcome': '{rate} — текущий курс звезды',
    'stars_prompt': '(без переменных)',
    'stars_options': '{recipient}, {rate}, {min}, {max}',
    'stars_custom': '{min}, {max}',
    'stars_order': '{order_id}, {recipient}, {stars}, {rate}, {total}',
    'premium_prompt': '(без переменных)',
    'premium_tariffs': '{recipient}',
    'premium_order': '{order_id}, {recipient}, {months}, {total}',
    'reviews_text': '(без переменных)',
    'payment_menu': '{order_id}, {item_title}, {total}',
    'payment_sbp_text': '{total}',
    'payment_card_text': '{total}',
    'payment_crypto_text': '{total}',
    'paid_wait_text': '(без переменных)',
}

SCREEN_TITLES = {
    'welcome': '🏠 Главный экран (/start)',
    'stars_prompt': '🌟 Звёзды: ввод @username',
    'stars_options': '🌟 Звёзды: выбор пакета',
    'stars_custom': '🌟 Звёзды: ручной ввод кол-ва',
    'stars_order': '🌟 Звёзды: карточка заказа',
    'premium_prompt': '💎 Premium: ввод @username',
    'premium_tariffs': '💎 Premium: выбор тарифа',
    'premium_order': '💎 Premium: карточка заказа',
    'reviews_text': '⭐ Отзывы: пост раздела',
    'payment_menu': '💳 Оплата: выбор способа',
    'payment_sbp_text': '💳 Оплата: СБП',
    'payment_card_text': '💳 Оплата: Карта РФ',
    'payment_crypto_text': '💳 Оплата: Криптовалюта',
    'paid_wait_text': '⏳ Пост «Я оплатил» (ожидание)',
}


def screen_text(key: str, **variables) -> str:
    """Текст экрана: settings['txt_<key>'] -> custom_welcome_text (welcome) -> TEXT_DEFAULTS.
    Подстановка переменных через .replace() — отсутствие {var} в тексте не вызывает ошибку."""
    text = get_setting('txt_' + key)
    if text is None and key == 'welcome':
        text = get_setting('custom_welcome_text')  # обратная совместимость (ШАГ 4)
    if text is None:
        text = TEXT_DEFAULTS[key]
    for var, val in variables.items():
        text = text.replace('{' + var + '}', str(val))
    return text


# === Сборка HTML из сообщения с Премиум-эмодзи (по entities) ===
ENTITY_TAGS = {
    'bold': ('<b>', '</b>'),
    'italic': ('<i>', '</i>'),
    'underline': ('<u>', '</u>'),
    'strikethrough': ('<s>', '</s>'),
    'spoiler': ('<tg-spoiler>', '</tg-spoiler>'),
    'code': ('<code>', '</code>'),
    'pre': ('<pre>', '</pre>'),
    'blockquote': ('<blockquote>', '</blockquote>'),
    'expandable_blockquote': ('<blockquote expandable>', '</blockquote>'),
}


def _utf16_offsets(text: str):
    """Префиксные UTF-16 смещения: Telegram считает offset/length в UTF-16, а Python — в символах.
    Возвращает список длиной len(text)+1, где [i] — UTF-16 смещение начала символа i."""
    offsets = [0]
    pos = 0
    for ch in text:
        pos += 2 if ord(ch) > 0xFFFF else 1
        offsets.append(pos)
    return offsets


def get_message_html_with_custom_emojis(message) -> str:
    """HTML сообщения, в котором Премиум-эмодзи намертво вшиты тегами
    <tg-emoji emoji-id="ID">СИМВОЛ</tg-emoji>.
    Работает по entities/caption_entities и корректно учитывает UTF-16 смещения Telegram,
    поэтому не зависит от того, как .text_html обработал кастомные эмодзи."""
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    if not text:
        return ""

    total = len(text)
    offsets = _utf16_offsets(text)

    opens = {}   # индекс символа -> [(UTF-16 конец, открывающий тег)]
    closes = {}  # индекс символа -> [(UTF-16 начало, закрывающий тег)]
    custom_count = 0
    custom_seen = 0

    for e in entities:
        start = bisect.bisect_left(offsets, e.offset)
        end = bisect.bisect_left(offsets, e.offset + e.length)
        start = max(0, min(start, total))
        end = max(0, min(end, total))
        if start >= end:
            continue

        if e.type == 'custom_emoji':
            custom_seen += 1
            emoji_id = str(getattr(e, 'custom_emoji_id', '') or '')
            if not emoji_id.isdigit():
                logger.warning("Премиум-эмодзи без корректного custom_emoji_id — пропущен")
                continue
            pair = (f'<tg-emoji emoji-id="{emoji_id}">', '</tg-emoji>')
            custom_count += 1
        elif e.type == 'text_link' and getattr(e, 'url', None):
            pair = (f'<a href="{html.escape(e.url, quote=True)}">', '</a>')
        elif e.type in ENTITY_TAGS:
            pair = ENTITY_TAGS[e.type]
        else:
            continue  # mention/hashtag/url/cashtag и т.п. тегов не требуют

        opens.setdefault(start, []).append((e.offset + e.length, pair[0]))
        closes.setdefault(end, []).append((e.offset, pair[1]))

    parts = []
    for i in range(total + 1):
        # Закрываем сущности: сначала внутренние (начались позже)
        for _, tag in sorted(closes.get(i, []), key=lambda x: -x[0]):
            parts.append(tag)
        # Открываем сущности: сначала внешние (заканчиваются позже)
        for _, tag in sorted(opens.get(i, []), key=lambda x: -x[0]):
            parts.append(tag)
        if i < total:
            parts.append(html.escape(text[i]))

    result = ''.join(parts)
    if custom_seen and not custom_count:
        logger.warning("В сообщении %s Премиум-эмодзи, но ни один не сохранён тегом", custom_seen)
    elif custom_count:
        logger.info("Сохранено Премиум-эмодзи тегами <tg-emoji>: %s", custom_count)
    return result


# === Редактируемые названия кнопок (ШАГ 2) ===
BUTTON_DEFAULTS = {
    'btn_main_stars': "Звёзды",
    'btn_main_premium': "Telegram Premium",
    'btn_main_reviews': "Отзывы",
    'btn_stars_100': "🌟 100 звёзд",
    'btn_stars_1000': "🌟 1000 звёзд",
    'btn_stars_10000': "🌟 10000 звёзд",
    'btn_prem_3': "💎 3 месяца — 1390₽",
    'btn_prem_6': "💎 6 месяцев — 1850₽",
    'btn_prem_12': "🔥 12 месяцев — 3190₽ (Выгодно)",
    'btn_pay_sbp': "💸 СБП (Система Быстрых Платежей)",
    'btn_pay_card': "💳 Банковская карта (РФ)",
    'btn_pay_crypto': "💎 Криптовалюта (USDT / TON / BTC)",
    'btn_reviews_url': "📢 Перейти к отзывам",
}

# Премиум-иконки кнопок (рисуются нативно через icon_custom_emoji_id)
BUTTON_ICON_DEFAULTS = {
    'btn_main_stars': '5936259309812846957',    # Премиум ⭐️
    'btn_main_premium': '6039402906476613311',  # Премиум ⭐️
    'btn_main_reviews': '5793933761594789855',  # Премиум 💬
}

BUTTON_TITLES = {
    'btn_main_stars': '1-й блок главного меню',
    'btn_main_premium': '2-й блок главного меню',
    'btn_main_reviews': '3-й блок главного меню',
    'btn_stars_100': 'Пакет 100 звёзд (цена считается сама)',
    'btn_stars_1000': 'Пакет 1000 звёзд (цена считается сама)',
    'btn_stars_10000': 'Пакет 10000 звёзд (цена считается сама)',
    'btn_prem_3': 'Premium 3 месяца',
    'btn_prem_6': 'Premium 6 месяцев',
    'btn_prem_12': 'Premium 12 месяцев',
    'btn_pay_sbp': 'Кнопка способа оплаты: СБП',
    'btn_pay_card': 'Кнопка способа оплаты: Карта РФ',
    'btn_pay_crypto': 'Кнопка способа оплаты: Крипта',
    'btn_reviews_url': 'Кнопка перехода в канал отзывов',
}

BUTTON_VARS = {
    'btn_stars_100': 'цену писать не нужно — считается сама по курсу',
    'btn_stars_1000': 'цену писать не нужно — считается сама по курсу',
    'btn_stars_10000': 'цену писать не нужно — считается сама по курсу',
}

# Количество звёзд в пакетах — цена на кнопке считается автоматически
STAR_PACK_COUNTS = {
    'btn_stars_100': 100,
    'btn_stars_1000': 1000,
    'btn_stars_10000': 10000,
}


def _star_pack_price(key: str) -> str:
    count = STAR_PACK_COUNTS[key]
    return f"{round(count * get_rate(), 2):g}"


def btn_label(key: str, **variables) -> str:
    """Название кнопки: settings['btn_<key>'] -> BUTTON_DEFAULTS.
    Для пакетов звёзд цена ВСЕГДА считается сама по курсу — владельцу не нужно
    писать {price} или формулы: бот сам подставит сумму и допишет «(140₽)»."""
    label = get_setting('btn_' + key) or BUTTON_DEFAULTS[key]
    label = re.sub(r'<[^>]+>', '', label)
    label = html.unescape(label).strip()
    for var, val in variables.items():
        label = label.replace('{' + var + '}', str(val))

    if key in STAR_PACK_COUNTS:
        calc_price = _star_pack_price(key)
        # убираем любые варианты формулы «курс × 100 / курс * звёзды» и плейсхолдер {price}
        label = re.sub(r'курс\s*[×x*]\s*(?:звёзды|звезды|\d+)', calc_price, label, flags=re.I)
        label = label.replace('{price}', calc_price)
        # убираем старую цену в скобках, чтобы она не устарела при смене курса
        label = re.sub(r'\s*\(\s*[\d.,]+\s*₽\s*\)', '', label)
        label = re.sub(r'\s+', ' ', label).strip()
        label = f"{label} ({calc_price}₽)"
    return label


def btn_icon(key: str):
    """ID Премиум-эмодзи для кнопки: настройка btn_icon_<key> -> BUTTON_ICON_DEFAULTS.
    Пустая строка в настройке означает «без иконки» (None)."""
    val = get_setting('btn_icon_' + key)
    if val is None:
        return BUTTON_ICON_DEFAULTS.get(key)
    val = str(val).strip()
    return val or None


def make_btn(key: str, callback_data=None, url=None, **variables) -> InlineKeyboardButton:
    """Кнопка с названием и Премиум-иконкой (icon_custom_emoji_id, PTB 22.x)."""
    label = btn_label(key, **variables)
    icon_id = btn_icon(key)
    kwargs = {}
    if callback_data is not None:
        kwargs['callback_data'] = callback_data
    if url is not None:
        kwargs['url'] = url
    if icon_id:
        try:
            return InlineKeyboardButton(text=label, icon_custom_emoji_id=icon_id, **kwargs)
        except TypeError:  # старая версия PTB без поддержки иконок
            logger.warning("PTB не поддерживает icon_custom_emoji_id — кнопка '%s' без иконки", key)
    return InlineKeyboardButton(text=label, **kwargs)


def extract_button_label_and_icon(message):
    """Разбирает сообщение владельца: (чистый текст кнопки, id Премиум-эмодзи или None).
    Фолбэк-символ Премиум-эмодзи вырезается из текста, чтобы не дублировать смайлик."""
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    icon_id = None
    clean = text
    for e in entities:
        if e.type == 'custom_emoji' and getattr(e, 'custom_emoji_id', None):
            icon_id = str(e.custom_emoji_id)
            offsets = _utf16_offsets(text)
            start = bisect.bisect_left(offsets, e.offset)
            end = bisect.bisect_left(offsets, e.offset + e.length)
            start = max(0, min(start, len(text)))
            end = max(0, min(end, len(text)))
            clean = (text[:start] + text[end:]).strip()
            break
    clean = re.sub(r'<[^>]+>', '', clean).strip()
    return clean, icon_id


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
    return prefix + screen_text('welcome', rate=f'{get_rate():g}')


def main_keyboard():
    return [
        [make_btn('btn_main_stars', callback_data='catalog_stars')],
        [make_btn('btn_main_premium', callback_data='catalog_premium')],
        [make_btn('btn_main_reviews', callback_data='reviews')]
    ]


def _fmt_total(total) -> str:
    """Сумма без хвостовых нулей: 140.0 -> '140', 140.5 -> '140.5'."""
    try:
        return f"{float(total):g}"
    except (TypeError, ValueError):
        return str(total)


def is_ton_address(value: str) -> bool:
    value = (value or '').strip()
    return bool(re.fullmatch(r'(?:UQ|EQ)[A-Za-z0-9_\-]{46}', value))


def build_payment_url(base_url: str, order_id, total, comment: str = None) -> str:
    """Персональная ссылка на оплату с предзаполненной суммой (ЮMoney и кассы).

    - ЮMoney (yoomoney.ru/to/…) → ?sum=<сумма>&label=order_<id> (+ quickpay-форма)
    - касса (Platega/Lava/AAIO/CloudTips) → ?order_id=<id>&amount=<сумма>
    - TON-адрес или tonkeeper → ссылка Tonkeeper с комментарием Order #id
    """
    if not base_url:
        return "https://t.me/starsdefolt"

    amount = _fmt_total(total)
    comment = comment or f"order_{order_id}"
    low = base_url.strip().lower()

    # Прямая крипто-оплата: адрес TON-кошелька или ссылка tonkeeper
    if is_ton_address(base_url.strip()):
        return f"https://app.tonkeeper.com/transfer/{base_url.strip()}?text={quote(comment)}"
    if 'tonkeeper' in low or low.startswith('ton://'):
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}text={quote(comment)}"

    # ЮMoney: личная страница перевода или быстрая форма
    if 'yoomoney.ru' in low or 'money.yandex' in low:
        if 'quickpay' in low:
            separator = "&" if "?" in base_url else "?"
            return f"{base_url}{separator}sum={amount}&label={quote(comment)}"
        if '/to/' in low:
            return f"https://yoomoney.ru/quickpay/confirm.xml?receiver={quote(base_url)}&sum={amount}&label={quote(comment)}&quickpay-form=button"
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}sum={amount}&label={quote(comment)}"

    # Обычные кассы и всё остальное: order_id + amount (+ comment)
    separator = "&" if "?" in base_url else "?"
    url = f"{base_url}{separator}order_id={order_id}&amount={amount}"
    if comment:
        url += f"&comment={quote(comment)}"
    return url


def is_rate_limited(context, user_id) -> bool:
    """Анти-флуд: True, если действие пришло слишком часто."""
    now = time.time()
    last = context.user_data.get('last_action_time', 0)
    if now - last < RATE_LIMIT_SECONDS:
        logger.warning("Анти-флуд: проигнорировано действие от id=%s", user_id)
        return True
    context.user_data['last_action_time'] = now
    return False


# === Слой БД: SQLite (локально) / PostgreSQL Neon (облако) ===
class Row(dict):
    """Строка результата: row['id'] и row[0] работают одинаково (как sqlite3.Row)."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _pg_row_factory(cursor):
    """Фабрика строк psycopg: dict-подобная Row."""
    values = cursor.fetchone()
    if values is None:
        return None
    return Row(zip([d.name for d in cursor.description], values))


def adapt_sql(sql: str) -> str:
    """SQLite-SQL -> PostgreSQL-SQL: ? -> %s, AUTOINCREMENT -> SERIAL."""
    if not DB_IS_POSTGRES:
        return sql
    sql = re.sub(r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT', 'SERIAL PRIMARY KEY', sql, flags=re.I)
    return sql.replace('?', '%s')


class _PgCursor:
    """Мини-совместимость с sqlite3.Cursor: execute/fetchone/fetchall/lastrowid."""

    def __init__(self, conn):
        self._conn = conn
        self._cur = None
        self._cached = None
        self.lastrowid = None

    def execute(self, sql, params=()):
        sql = adapt_sql(sql)
        table = None
        insert = sql.lstrip()[:6].upper() == 'INSERT'
        if insert:
            match = re.search(r'INSERT\s+INTO\s+"?(\w+)"?', sql, re.I)
            table = match.group(1).lower() if match else None
        wants_id = table in ('orders', 'reviews') and 'RETURNING' not in sql.upper()
        if wants_id:
            sql = sql.rstrip().rstrip(';') + ' RETURNING id'
        try:
            self._cur = self._conn._conn.cursor()
            self._cur.execute(sql, params)
            if wants_id:
                row = self._cur.fetchone()
                self.lastrowid = row['id'] if row else None
        except Exception:
            self._conn._conn.rollback()
            raise
        return self

    def fetchone(self):
        if self._cached is not None:
            row, self._cached = self._cached, None
            return row
        return self._cur.fetchone() if self._cur else None

    def fetchall(self):
        return self._cur.fetchall() if self._cur else []


class _PgConn:
    def __init__(self, dsn):
        self._conn = psycopg.connect(dsn, row_factory=_pg_row_factory)
        self.row_factory = None  # совместимость с sqlite3 (присваивание игнорируется)

    def cursor(self):
        return _PgCursor(self)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def connect_db():
    """PostgreSQL при заданном DATABASE_URL, иначе локальный SQLite."""
    if DB_IS_POSTGRES:
        if psycopg is None:
            raise RuntimeError("DATABASE_URL задан, но пакет psycopg не установлен")
        return _PgConn(DATABASE_URL)
    return sqlite3.connect(DB_NAME)


def mask_secret(value: str, keep: int = 6) -> str:
    """Маскирует секрет для показа в чате: postgresql://user:••••@host/db."""
    value = (value or '').strip()
    if not value:
        return "— не задано —"
    if '://' in value:
        head, rest = value.split('://', 1)
        if '@' in rest:
            creds, host = rest.split('@', 1)
            user = creds.split(':', 1)[0]
            return f"{head}://{user}:••••@{host}"
    return value[:keep] + "••••" if len(value) > keep else "••••"


def apply_database_url(url: str, persist: bool = True) -> bool:
    """Переключает connect_db() на PostgreSQL-базу владельца без перезапуска сервера.

    Проверяет соединение, создаёт таблицы, переносит текущий кэш настроек в новую БД.
    При любой ошибке возвращает False и оставляет прежнюю базу — бот продолжает работать.
    """
    global DATABASE_URL, DB_IS_POSTGRES

    url = (url or '').strip()
    if not url.lower().startswith(('postgres://', 'postgresql://')):
        logger.error("DATABASE_URL отклонён: нужна строка postgresql://user:pass@host/db")
        return False
    if psycopg is None:
        logger.error("DATABASE_URL не применён: пакет psycopg не установлен")
        return False

    old_url, old_flag = DATABASE_URL, DB_IS_POSTGRES
    DATABASE_URL, DB_IS_POSTGRES = url, True
    try:
        connect_db().close()
    except Exception as exc:
        DATABASE_URL, DB_IS_POSTGRES = old_url, old_flag
        logger.error("DATABASE_URL не применён (нет соединения): %s", exc)
        return False

    try:
        init_db()  # создаёт таблицы в новой базе и подтягивает settings_backup.json
    except Exception as exc:
        logger.error("Не удалось создать таблицы в новой БД: %s", exc)

    if persist:
        _SETTINGS_CACHE['database_url'] = url
    for key, value in list(_SETTINGS_CACHE.items()):
        set_setting(key, value)

    logger.info("База данных переключена на PostgreSQL: %s", mask_secret(url))
    return True


def ensure_database_from_settings():
    """При старте подхватывает DATABASE_URL, сохранённый владельцем через /admin."""
    if DB_IS_POSTGRES:
        return
    saved = (os.getenv('DATABASE_URL') or get_setting('database_url') or '').strip()
    if saved:
        logger.info("Найден сохранённый DATABASE_URL — подключаю PostgreSQL")
        apply_database_url(saved, persist=False)


# === БД: настройки + заказы ===
# ВАЖНО: базовые тексты (TEXT_DEFAULTS) и названия кнопок (BUTTON_DEFAULTS) зашиты в код.
# БД используется для истории заказов; settings — лишь необязательный кэш правок админа.
# Если БД пуста/удалена/недоступна, бот стартует и работает на константах из кода.
_SETTINGS_CACHE = {}  # оперативные правки админа в памяти (когда БД недоступна)
SETTINGS_BACKUP_FILE = os.path.join(BASE_DIR, "settings_backup.json")


def _save_settings_backup():
    """Дублирует все настройки в settings_backup.json (переживает очистку БД и контейнера)."""
    try:
        data = {}
        try:
            conn = connect_db()
            c = conn.cursor()
            c.execute("SELECT key, value FROM settings")
            for row in c.fetchall():
                data[str(row[0])] = row[1]
            conn.close()
        except Exception as exc:
            logger.warning("Бэкап настроек: БД недоступна (%s) — сохраняю кэш памяти", exc)
        data.update(_SETTINGS_CACHE)  # свежие правки важнее
        tmp = SETTINGS_BACKUP_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_BACKUP_FILE)
    except OSError as exc:
        logger.warning("Не удалось записать %s: %s", SETTINGS_BACKUP_FILE, exc)


def _load_settings_backup() -> dict:
    try:
        with open(SETTINGS_BACKUP_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Не удалось прочитать %s: %s", SETTINGS_BACKUP_FILE, exc)
        return {}


def init_db():
    try:
        conn = connect_db()
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
        except Exception:
            conn.rollback()  # колонка уже существует (SQLite/PostgreSQL)
        conn.commit()

        # Восстановление настроек из JSON, если БД пустая/стёрлась
        backup = _load_settings_backup()
        restored = 0
        for k, v in backup.items():
            if get_setting(k) is None:
                _SETTINGS_CACHE[str(k)] = v
                try:
                    c.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT(\"key\") DO UPDATE SET value = excluded.value", (k, v))
                    restored += 1
                except Exception:
                    conn.rollback()
        if restored:
            conn.commit()
            logger.info("Настройки восстановлены из settings_backup.json: %s", restored)

        conn.close()
        logger.info("База данных инициализирована: %s",
                    "PostgreSQL" if DB_IS_POSTGRES else DB_NAME)
    except Exception as exc:
        logger.error(
            "БД недоступна (%s): %s — продолжаем работу на дефолтах из кода",
            "PostgreSQL" if DB_IS_POSTGRES else DB_NAME, exc)


def set_setting(key, value):
    """Пишет настройку: в память (всегда) и в БД (если доступна). БД не обязательна."""
    _SETTINGS_CACHE[key] = value
    try:
        conn = connect_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(\"key\") DO UPDATE SET value = excluded.value",
            (key, value)
        )
        conn.commit()
        conn.close()
        _save_settings_backup()
        return True
    except Exception as exc:
        logger.error("Не удалось сохранить настройку '%s' в БД: %s (держим в памяти)", key, exc)
        _save_settings_backup()
        return False


def get_setting(key):
    """Настройка из БД (если есть), иначе из памяти, иначе None -> дефолт из кода."""
    try:
        conn = connect_db()
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = c.fetchone()
        conn.close()
        if row:
            return row[0]
    except Exception as exc:
        logger.warning("Не удалось прочитать настройку '%s': %s", key, exc)
    return _SETTINGS_CACHE.get(key)


def delete_setting(key):
    _SETTINGS_CACHE.pop(key, None)
    try:
        conn = connect_db()
        c = conn.cursor()
        c.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()
        conn.close()
        _save_settings_backup()
        return True
    except Exception as exc:
        logger.error("Не удалось удалить настройку '%s': %s", key, exc)
        _save_settings_backup()
        return False


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
        'use_my_username_stars': lambda q, c: use_my_username(q, c, False),
        'use_my_username_prem': lambda q, c: use_my_username(q, c, True),
        'adm_rate': admin_rate,
        'adm_emoji': admin_emoji,
        'adm_emoji_clear': admin_emoji_clear,
        'adm_edit_text': admin_edit_text,  # совместимость: эквивалент редактирования welcome
        'adm_reset_text': admin_reset_text,  # совместимость: сброс welcome
        'adm_texts_menu': admin_texts_menu,
        'adm_buttons_menu': admin_buttons_menu,
        'adm_pay_links': admin_pay_links,
        'adm_fragment': admin_fragment,
        'adm_cloud': admin_cloud,
        'adm_set_db_url': admin_set_db_url,
        'adm_set_deploy_hook': admin_set_deploy_hook,
        'adm_restart_render': admin_restart_render,
        'adm_ping_now': admin_ping_now,
        'adm_fragment_mode': admin_fragment_mode,
        'adm_fragment_clear': admin_fragment_clear,
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
    elif data.startswith('adm_txtrst_'):
        key = data.split('adm_txtrst_', 1)[1]
        if key in TEXT_DEFAULTS:
            await admin_text_reset(query, context, key)
            return
    elif data.startswith('adm_txt_'):
        key = data.split('adm_txt_', 1)[1]
        if key in TEXT_DEFAULTS:
            await admin_edit_screen(query, context, key)
            return
    elif data.startswith('adm_btnrst_'):
        key = data.split('adm_btnrst_', 1)[1]
        if key in BUTTON_DEFAULTS:
            await admin_button_reset(query, context, key)
            return
    elif data.startswith('adm_btn_'):
        key = data.split('adm_btn_', 1)[1]
        if key in BUTTON_DEFAULTS:
            await admin_edit_button(query, context, key)
            return
    elif data.startswith('adm_setpay_'):
        pay_type = data.split('adm_setpay_', 1)[1]
        if pay_type in PAY_TYPES:
            await admin_setpay(query, context, pay_type)
            return
    elif data.startswith('adm_fragment_set_'):
        field = data.split('adm_fragment_set_', 1)[1]
        if field in FRAGMENT_FIELDS:
            await admin_fragment_prompt(query, context, field)
            return

    handler = handlers.get(data)
    if handler:
        await handler(query, context)


# === Блок 1: Звёзды ===
async def show_catalog_prompt(query, context):
    context.user_data['state'] = 'WAIT_USERNAME'
    keyboard = []
    own_username = getattr(query.from_user, 'username', None) if getattr(query, 'from_user', None) else None
    if own_username:
        keyboard.append([InlineKeyboardButton(
            f"👤 Купить себе (@{own_username})", callback_data='use_my_username_stars')])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back')])
    await query.message.edit_text(
        screen_text('stars_prompt'),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def use_my_username(query, context, for_premium: bool):
    """Быстрый выбор своего аккаунта: покупателю не нужно печатать ник."""
    username = getattr(query.from_user, 'username', None)
    if not username:
        await query.answer("У вашего аккаунта нет @username — введите ник вручную.",
                           show_alert=True)
        return
    context.user_data['recipient'] = '@' + username
    logger.info("Покупатель id=%s выбрал свой аккаунт @%s", query.from_user.id, username)
    if for_premium:
        await show_premium_tariffs(query, context)
    else:
        await show_star_options(query, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state', '')
    user = update.effective_user

    # Премиум-эмодзи от владельца: сначала проверяем режим установки, потом эхо
    if (user.id == ADMIN_ID and update.message.entities
            and state not in ('WAIT_WELCOME_TEXT', 'WAIT_SCREEN_TEXT', 'WAIT_BUTTON_TEXT')):
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
        username, error = validate_username(text_in)
        if error:
            logger.warning("Некорректный username от id=%s: %r", user.id, text_in)
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back')]]
            await update.message.reply_text(
                error, reply_markup=InlineKeyboardMarkup(keyboard))
            return
        exists = await asyncio.to_thread(verify_telegram_username, username)
        if not exists:
            logger.warning("Аккаунт %s не найден в Telegram (id=%s)", username, user.id)
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back')]]
            await update.message.reply_text(
                f"❌ Аккаунт <code>{html.escape(username)}</code> не найден в Telegram.\n"
                "Проверьте ник и отправьте его ещё раз:",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
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

    elif state == 'WAIT_SCREEN_TEXT':
        if user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Нет прав.")
            return
        key = context.user_data.get('edit_key')
        if key not in TEXT_DEFAULTS:
            context.user_data['state'] = ''
            return
        # Собираем HTML вручную по entities: .text_html не гарантирует <tg-emoji>
        # для Премиум-эмодзи, присланных в диалоге с ботом
        text_html = get_message_html_with_custom_emojis(update.message) or update.message.text_html
        if not text_html:
            await update.message.reply_text("❌ Отправьте текстовое сообщение.")
            return
        saved_ok = set_setting('txt_' + key, text_html)
        context.user_data['state'] = ''
        logger.info("Админ %s обновил текст экрана '%s' (%s символов)", user.id, key, len(text_html))
        warn = "" if saved_ok else (
            "\n\n⚠️ БД недоступна: текст применён сейчас, но после перезапуска "
            "вернётся дефолт из кода.")
        await update.message.reply_text(
            f"✅ <b>Текст экрана «{SCREEN_TITLES[key]}» обновлён!</b>\n\n"
            "Проверьте соответствующий раздел бота. Изменения применяются сразу.\n"
            "Ниже — как текст выглядит для покупателей:" + warn,
            parse_mode='HTML'
        )
        try:
            await update.message.reply_text(text_html, parse_mode='HTML')
        except Exception as exc:
            logger.warning("Не удалось показать предпросмотр текста '%s': %s", key, exc)

    elif state == 'WAIT_BUTTON_TEXT':
        if user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Нет прав.")
            return
        key = context.user_data.get('edit_key')
        if key not in BUTTON_DEFAULTS:
            context.user_data['state'] = ''
            return

        # Владелец просто шлёт текст с Премиум-эмодзи (или обычным смайликом) —
        # эмодзи становится нативной иконкой кнопки icon_custom_emoji_id,
        # а его фолбэк-символ вырезается из текста, чтобы не дублироваться.
        clean_label, icon_id = extract_button_label_and_icon(update.message)
        if not clean_label and not icon_id:
            await update.message.reply_text("❌ Отправьте текстовое сообщение.")
            return

        if icon_id:
            set_setting('btn_icon_' + key, icon_id)
            new_label = clean_label or BUTTON_DEFAULTS[key]
        else:
            set_setting('btn_icon_' + key, '')
            new_label = clean_label or BUTTON_DEFAULTS[key]

        saved_ok = set_setting('btn_' + key, new_label)
        context.user_data['state'] = ''
        logger.info(
            "Админ %s обновил кнопку '%s': текст=%r, icon_custom_emoji_id=%s",
            user.id, key, new_label, icon_id or '(нет)')

        warn = "" if saved_ok else (
            "\n\n⚠️ БД недоступна: название применено сейчас, но после перезапуска "
            "вернётся дефолт из кода.")
        await update.message.reply_text(
            f"✅ <b>Кнопка «{BUTTON_TITLES[key]}» обновлена!</b>\n\n"
            f"Текст: {html.escape(new_label)}\n"
            f"Премиум-иконка: {'есть' if icon_id else 'обычный смайлик'}" + warn,
            parse_mode='HTML'
        )
        # Сразу показываем живую кнопку с Премиум-эмодзи
        try:
            await update.message.reply_text(
                "👇 Так кнопка выглядит теперь:",
                reply_markup=InlineKeyboardMarkup([[make_btn(key, callback_data='back')]])
            )
        except Exception as exc:
            logger.warning("Не удалось показать тестовую кнопку '%s': %s", key, exc)

    elif state == 'WAIT_PAY_URL':
        if user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Нет прав.")
            return
        pay_type = context.user_data.get('pay_type')
        names = {'sbp': 'СБП', 'card': 'Карта РФ', 'crypto': 'Криптовалюта'}
        if pay_type not in names:
            context.user_data['state'] = ''
            await update.message.reply_text("❌ Не выбран способ оплаты — откройте /admin.")
            return
        link = text_in.strip()
        if pay_type == 'crypto' and is_ton_address(link):
            pass  # прямой адрес TON-кошелька разрешён
        elif not link.startswith('http'):
            if pay_type == 'crypto':
                await update.message.reply_text(
                    "❌ Отправьте ссылку (https://…) или адрес TON-кошелька (UQ… / EQ…).\n"
                    "Попробуйте ещё раз:")
            else:
                await update.message.reply_text(
                    "❌ Ссылка должна начинаться с https://\nПопробуйте ещё раз:")
            return
        set_setting(f'{pay_type}_url', link)
        context.user_data['state'] = ''
        context.user_data.pop('pay_type', None)
        logger.info("Админ %s обновил ссылку оплаты %s: %s", user.id, pay_type, link)
        await update.message.reply_text(
            f"✅ <b>Ссылка для «{names[pay_type]}» обновлена!</b>\n\n<code>{html.escape(link)}</code>",
            parse_mode='HTML'
        )

    elif state == 'WAIT_FRAGMENT_VALUE':
        if user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Нет прав.")
            return
        field = context.user_data.get('fragment_field')
        if field not in FRAGMENT_FIELDS:
            context.user_data['state'] = ''
            await update.message.reply_text(
                "❌ Параметр не выбран — откройте /admin → 🤖 Автовыдача Fragment.")
            return
        value = text_in.strip()
        if not value:
            await update.message.reply_text("❌ Пустое значение. Отправьте текст ещё раз:")
            return

        setting_key, title = FRAGMENT_FIELDS[field]
        set_setting(setting_key, value)
        context.user_data['state'] = ''
        context.user_data.pop('fragment_field', None)
        logger.info("Админ %s задал Fragment-параметр %s (значение скрыто)", user.id, setting_key)

        await update.message.reply_text(
            f"✅ <b>{title} — сохранено</b>\n\n"
            f"Значение: <code>{_mask(value)}</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🤖 К автовыдаче Fragment", callback_data='adm_fragment')
            ]])
        )

    elif state == 'WAIT_DB_URL':
        if user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Нет прав.")
            return
        url = text_in.strip()
        if not url.lower().startswith(('postgres://', 'postgresql://')):
            await update.message.reply_text(
                "❌ Нужна строка подключения Neon, начинающаяся с postgresql://\n"
                "Попробуйте ещё раз:")
            return
        ok = await asyncio.to_thread(apply_database_url, url, True)
        if not ok:
            await update.message.reply_text(
                "⚠️ <b>База не подключена</b> — проверьте строку и доступность Neon.\n"
                "Бот продолжает работать на прежней базе.",
                parse_mode='HTML'
            )
            return
        context.user_data['state'] = ''
        await update.message.reply_text(
            "✅ <b>PostgreSQL подключён!</b>\n\n"
            f"<code>{html.escape(mask_secret(DATABASE_URL))}</code>\n\n"
            "Заказы теперь сохраняются постоянно и не стираются при передеплое.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("☁️ К облаку", callback_data='adm_cloud')
            ]])
        )

    elif state == 'WAIT_DEPLOY_HOOK':
        if user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Нет прав.")
            return
        hook = text_in.strip()
        if not hook.startswith('http'):
            await update.message.reply_text(
                "❌ Deploy Hook должен начинаться с https://\nПопробуйте ещё раз:")
            return
        set_setting('render_deploy_hook', hook)
        context.user_data['state'] = ''
        logger.info("Админ %s задал Render Deploy Hook", user.id)
        await update.message.reply_text(
            "✅ <b>Deploy Hook сохранён</b>\n\n"
            f"<code>{html.escape(mask_secret(hook))}</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Перезапустить сервер Render",
                                      callback_data='adm_restart_render')],
                [InlineKeyboardButton("☁️ К облаку", callback_data='adm_cloud')],
            ])
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
        [make_btn('btn_stars_100', callback_data='stars_100', price=f'{round(100 * rate, 2):g}')],
        [make_btn('btn_stars_1000', callback_data='stars_1000', price=f'{round(1000 * rate, 2):g}')],
        [make_btn('btn_stars_10000', callback_data='stars_10000', price=f'{round(10000 * rate, 2):g}')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]

    text = screen_text(
        'stars_options',
        recipient=username, rate=f'{rate:g}', min=MIN_STARS, max=MAX_STARS
    )

    context.user_data['state'] = 'WAIT_STARS'

    if hasattr(query_or_update, 'edit_message_text'):
        await query_or_update.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    elif hasattr(query_or_update, 'message') and query_or_update.message:
        await query_or_update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else:
        await query_or_update.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def select_stars(query, context, count):
    context.user_data['stars'] = count
    await calculate_cost(query, context)


async def stars_custom(query, context):
    context.user_data['state'] = 'WAIT_STARS'
    keyboard = [
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.message.edit_text(
        screen_text('stars_custom', min=MIN_STARS, max=MAX_STARS),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def calculate_cost(update_or_query, context):
    stars = context.user_data.get('stars', 100)
    recipient = context.user_data.get('recipient', '@----')
    rate = get_rate()
    total = round(stars * rate, 2)
    context.user_data['total'] = total
    context.user_data['state'] = ''
    context.user_data['item_title'] = f"{stars} ⭐ Telegram Stars ({total:g}₽)"

    # Определяем реального покупателя
    user = getattr(update_or_query, 'effective_user', None)
    if user is None:
        user = getattr(update_or_query, 'from_user', None)
    buyer = buyer_name(user)
    buyer_id = user.id if user else None

    # Сохраняем заказ в БД
    try:
        conn = connect_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO orders (buyer, buyer_id, recipient, stars, total, status) VALUES (?, ?, ?, ?, ?, ?)",
            (buyer, buyer_id, recipient, stars, total, "created")
        )
        order_id = c.lastrowid
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.exception("Не удалось сохранить заказ звёзд в БД: %s", exc)
        order_id = 0

    context.user_data['order_id'] = order_id

    logger.info(
        "Расчёт заказа #%s: покупатель=%s, получатель=%s, звёзд=%s, курс=%s₽, сумма=%s₽",
        order_id, buyer, recipient, stars, rate, total
    )

    text = screen_text(
        'stars_order',
        order_id=order_id, recipient=recipient, stars=stars,
        rate=f'{rate:g}', total=f'{total:g}'
    )

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
    keyboard = []
    own_username = getattr(query.from_user, 'username', None) if getattr(query, 'from_user', None) else None
    if own_username:
        keyboard.append([InlineKeyboardButton(
            f"👤 Купить себе (@{own_username})", callback_data='use_my_username_prem')])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back')])
    await query.message.edit_text(
        screen_text('premium_prompt'),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def show_premium_tariffs(query_or_update, context):
    username = context.user_data.get('recipient', '@----')
    keyboard = [
        [make_btn('btn_prem_3', callback_data='prem_3_1390')],
        [make_btn('btn_prem_6', callback_data='prem_6_1850')],
        [make_btn('btn_prem_12', callback_data='prem_12_3190')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]

    text = screen_text('premium_tariffs', recipient=username)
    context.user_data['state'] = ''

    if hasattr(query_or_update, 'edit_message_text'):
        await query_or_update.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    elif hasattr(query_or_update, 'message') and query_or_update.message:
        await query_or_update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else:
        await query_or_update.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


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
    try:
        conn = connect_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO orders (buyer, buyer_id, recipient, stars, total, status) VALUES (?, ?, ?, ?, ?, ?)",
            (buyer, buyer_id, recipient, 0, price, f"premium_{months}m")
        )
        order_id = c.lastrowid
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.exception("Не удалось сохранить Premium-заказ в БД: %s", exc)
        order_id = 0

    context.user_data['order_id'] = order_id

    logger.info(
        "Заказ Premium #%s: покупатель=%s, получатель=%s, тариф=%s мес., сумма=%s₽",
        order_id, buyer, recipient, months, price
    )

    text = screen_text(
        'premium_order',
        order_id=order_id, recipient=recipient, months=months, total=f'{price:g}'
    )

    keyboard = [
        [InlineKeyboardButton("💳 Перейти к оплате", callback_data='payment')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# === Блок 3: Отзывы ===
async def show_reviews(query, context):
    text = screen_text('reviews_text')
    keyboard = [
        [make_btn('btn_reviews_url', url=REVIEWS_LINK)],
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
    text = screen_text('payment_menu', order_id=order_id, item_title=item_title, total=f'{total:g}')

    keyboard = [
        [make_btn('btn_pay_sbp', callback_data='payment_sbp')],
        [make_btn('btn_pay_card', callback_data='payment_card')],
        [make_btn('btn_pay_crypto', callback_data='payment_crypto')],
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
        screen_text('payment_sbp_text', total=f'{total:g}'),
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
        screen_text('payment_card_text', total=f'{total:g}'),
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


async def payment_crypto(query, context):
    total = context.user_data.get('total', 0)
    order_id = context.user_data.get('order_id', 0)
    base = get_payment_url('crypto_url', CRYPTO_PAYMENT_URL)
    url = build_payment_url(base, order_id, total, comment=f"Order #{order_id}")
    logger.info("Оплата криптовалютой: заказ=%s, сумма=%s₽, ссылка=%s", order_id, total, url)
    label = "💎 Оплатить в Tonkeeper" if (is_ton_address(base.strip()) or 'tonkeeper' in base.lower()) \
        else f"💳 Перейти на страницу оплаты ({total:g}₽)"

    keyboard = [
        [InlineKeyboardButton(label, url=url)],
        [InlineKeyboardButton("✅ Я оплатил", callback_data=f"paid_{order_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data='payment')]
    ]
    await query.edit_message_text(
        screen_text('payment_crypto_text', total=f'{total:g}'),
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
    conn = connect_db()
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
    conn = connect_db()
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
        try:
            stars_in_order = int(row.get('stars') or 0)
        except (TypeError, ValueError):
            stars_in_order = 0
        if stars_in_order > 0:
            keyboard.insert(1, [InlineKeyboardButton(
                "💎 Выдать на Fragment в 1 клик",
                url=fragment_buy_page_link(row['recipient'], stars_in_order))])
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
        screen_text('paid_wait_text', order_id=order_id),
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
# === Fragment: автоматическая покупка звёзд ===
FRAGMENT_TIMEOUT = 25
FRAGMENT_API_DEFAULT = "https://fragment.com/api"
TONCENTER_RPC = (os.getenv('TONCENTER_RPC') or "https://toncenter.com/api/v2/jsonRPC")


def _fragment_endpoint(frag_hash: str = "") -> str:
    """URL Fragment API: из настроек/окружения либо стандартный, с ?hash= если он задан."""
    base = (get_setting('fragment_api_url')
            or os.getenv('FRAGMENT_API_URL')
            or FRAGMENT_API_DEFAULT).strip().rstrip('/')
    if not base.startswith('http'):
        base = FRAGMENT_API_DEFAULT
    if frag_hash:
        from urllib.parse import quote as _quote
        base = f"{base}{'&' if '?' in base else '?'}hash={_quote(frag_hash)}"
    return base


def _fragment_post(url: str, form: dict, cookies: str = "") -> dict:
    """POST-запрос к Fragment (form-urlencoded, как это делает их веб-клиент)."""
    import urllib.parse as _urlparse
    import urllib.request as _urlreq

    body = _urlparse.urlencode(form).encode('utf-8')
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': 'https://fragment.com',
        'Referer': 'https://fragment.com/stars/buy',
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
    }
    if cookies:
        headers['Cookie'] = cookies
    request = _urlreq.Request(url, data=body, headers=headers, method='POST')
    with _urlreq.urlopen(request, timeout=FRAGMENT_TIMEOUT) as response:
        raw = response.read().decode('utf-8', errors='replace')
    return json.loads(raw) if raw.strip() else {}


def _fragment_buy_via_api(recipient: str, stars: int, api_key: str) -> dict:
    """Способ А: внешний API-сервис автовыдачи (ключ FRAGMENT_API_KEY / настройка в /admin)."""
    import urllib.request as _urlreq

    username = recipient.lstrip('@')
    payload = json.dumps({
        'recipient': username,
        'username': username,
        'quantity': stars,
        'amount': stars,
    }).encode('utf-8')
    request = _urlreq.Request(
        _fragment_endpoint(),
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-API-Key': api_key,
            'Authorization': f'Bearer {api_key}',
        },
        method='POST',
    )
    with _urlreq.urlopen(request, timeout=FRAGMENT_TIMEOUT) as response:
        raw = response.read().decode('utf-8', errors='replace')
    data = json.loads(raw) if raw.strip() else {}

    tx_id = (data.get('tx_id') or data.get('transaction_id') or data.get('id')
             or (data.get('data') or {}).get('tx_id'))
    if data.get('ok') or data.get('success') or tx_id:
        logger.info("Fragment autobuy (API): %s ⭐ для %s — успех, tx=%s",
                    stars, recipient, tx_id)
        return {"success": True, "tx_id": str(tx_id or ''), "link": data.get('link', '')}

    error = data.get('error') or data.get('message') or raw[:300]
    logger.error("Fragment autobuy (API): отказ для %s (%s ⭐): %s", recipient, stars, error)
    return {"success": False, "error": str(error)}


def fragment_buy_page_link(recipient: str, stars) -> str:
    """Публичная страница покупки звёзд на Fragment (работает без ключей и сессии)."""
    from urllib.parse import urlencode
    query = {'recipient': (recipient or '').lstrip('@')}
    try:
        quantity = int(stars)
    except (TypeError, ValueError):
        quantity = 0
    if quantity > 0:
        query['quantity'] = quantity
    return "https://fragment.com/stars/buy?" + urlencode(query)


def tonkeeper_pay_link(to_address: str, amount_nano: int, payload_b64: str = "") -> str:
    """Ссылка на оплату в 1 клик через Tonkeeper (или другой TON-кошелёк)."""
    base = f"https://app.tonkeeper.com/transfer/{to_address}?amount={int(amount_nano)}"
    if payload_b64:
        b64url = payload_b64.replace('+', '-').replace('/', '_').rstrip('=')
        base += f"&bin={b64url}"
    return base


def _ton_rpc(method: str, params: dict) -> dict:
    """Запрос к публичному TON RPC (toncenter.com, бесплатный)."""
    import urllib.request as _urlreq

    body = json.dumps({"id": 1, "jsonrpc": "2.0", "method": method, "params": params}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = (get_setting('toncenter_api_key') or os.getenv('TONCENTER_API_KEY') or '').strip()
    if api_key:
        headers["X-API-Key"] = api_key

    request = _urlreq.Request(TONCENTER_RPC, data=body, headers=headers, method='POST')
    with _urlreq.urlopen(request, timeout=FRAGMENT_TIMEOUT) as response:
        raw = response.read().decode('utf-8', errors='replace')
    data = json.loads(raw) if raw.strip() else {}
    if isinstance(data, dict) and data.get('error'):
        error = data['error']
        message = error.get('message') if isinstance(error, dict) else str(error)
        raise RuntimeError(f"TON RPC {method}: {message}")
    return (data or {}).get('result') or {}


def _ton_send_payment(mnemonic: str, to_address: str, amount_nano: int,
                      payload_b64: str = "") -> dict:
    """Подписывает и отправляет TON-транзакцию кошельком владельца (v4r2).

    Требует пакет tonsdk (pip install tonsdk). Возвращает {"success": True, "tx_id": ...}.
    """
    try:
        from tonsdk.contract.wallet import Wallets, WalletVersionEnum
        from tonsdk.utils import bytes_to_b64str
    except ImportError as exc:
        raise RuntimeError("не установлен пакет tonsdk — автооплата TON недоступна") from exc

    words = (mnemonic or '').replace(',', ' ').split()
    if len(words) not in (12, 24):
        raise RuntimeError(f"в мнемонике {len(words)} слов вместо 12 или 24")

    _, _, _, wallet = Wallets.from_mnemonics(words, version=WalletVersionEnum.v4r2)
    wallet_address = wallet.address.to_string(True, True, True)

    seqno = 0
    try:
        result = _ton_rpc("runGetMethod",
                          {"address": wallet_address, "method": "seqno", "stack": []})
        stack = result.get("stack") or []
        if stack:
            raw_value = stack[0][1] if isinstance(stack[0], (list, tuple)) else stack[0]
            seqno = int(raw_value, 16) if isinstance(raw_value, str) else int(raw_value)
    except Exception as exc:
        logger.warning("TON: seqno не получен (%s) — пробую деплой кошелька", exc)
        seqno = 0

    extra = {}
    if seqno == 0:
        try:
            extra["state_init"] = wallet.create_state_init()["state_init"]
        except Exception as exc:
            logger.warning("TON: state_init не собран (%s)", exc)

    message = wallet.create_transfer_message(
        to_address, int(amount_nano), seqno=seqno, payload=payload_b64 or "", **extra)
    boc_b64 = bytes_to_b64str(message["message"].to_boc(False))
    sent = _ton_rpc("sendBoc", {"boc": boc_b64})

    tx_hash = str((sent or {}).get("hash") or boc_b64[:44])
    logger.info("TON: транзакция отправлена с %s (seqno=%s), tx=%s",
                wallet_address, seqno, tx_hash)
    return {"success": True, "tx_id": tx_hash, "wallet": wallet_address}


def _fragment_buy_direct(recipient: str, stars: int, frag_hash: str, cookies: str) -> dict:
    """Способ Б: прямой 3-шаговый поток fragment.com/api?hash=<hash>."""
    url = _fragment_endpoint(frag_hash)
    username = recipient.lstrip('@')

    # Шаг 1: поиск получателя
    step1 = _fragment_post(url, {
        'method': 'searchStarsRecipient',
        'query': username,
        'quantity': stars,
        'mode': 'receiver',
    }, cookies)
    found = step1.get('found') or {}
    recipient_id = (found.get('recipient') or found.get('id')
                    or step1.get('recipient') or (step1.get('data') or {}).get('recipient'))
    if not recipient_id:
        error = step1.get('error') or step1.get('message') or f"получатель @{username} не найден"
        logger.error("Fragment autobuy (direct): шаг 1 не удался для @%s: %s", username, error)
        return {"success": False, "error": f"Поиск получателя: {error}"}

    # Шаг 2: создание запроса на покупку
    step2 = _fragment_post(url, {
        'method': 'initBuyStarsRequest',
        'recipient': recipient_id,
        'quantity': stars,
    }, cookies)
    req_id = (step2.get('req_id') or step2.get('id')
              or (step2.get('request') or {}).get('id')
              or (step2.get('data') or {}).get('req_id'))
    if not req_id:
        error = step2.get('error') or step2.get('message') or "не удалось создать запрос на покупку"
        logger.error("Fragment autobuy (direct): шаг 2 не удался для @%s: %s", username, error)
        return {"success": False, "error": f"Создание запроса: {error}"}

    # Шаг 3: получение ссылки на оплату TON
    step3 = _fragment_post(url, {
        'method': 'getBuyStarsLink',
        'account': recipient_id,
        'transaction': 1,
        'id': req_id,
    }, cookies)
    transaction = step3.get('transaction') or {}
    messages = transaction.get('messages') or step3.get('messages') or []
    first = messages[0] if messages else {}
    to_address = (first.get('address') or transaction.get('address')
                  or step3.get('address') or '')
    try:
        amount_nano = int(first.get('amount') or transaction.get('amount') or 0)
    except (TypeError, ValueError):
        amount_nano = 0
    payload_b64 = (first.get('payload') or transaction.get('payload')
                   or step3.get('payload') or '')

    link = (step3.get('link') or transaction.get('link')
            or (step3.get('data') or {}).get('link') or '')
    tx_id = link or req_id

    if not (step3.get('ok') or step3.get('success') or link or step3.get('transaction')
            or to_address):
        error = step3.get('error') or step3.get('message') or "Fragment не вернул данные транзакции"
        logger.error("Fragment autobuy (direct): шаг 3 не удался для @%s: %s", username, error)
        return {"success": False, "error": f"Транзакция: {error}",
                "link": fragment_buy_page_link(username, stars)}

    # Персональная ссылка на оплату в 1 клик (Tonkeeper) либо страница Fragment
    ton_link = tonkeeper_pay_link(to_address, amount_nano, payload_b64) if to_address else ""
    pay_link = ton_link or link or fragment_buy_page_link(username, stars)

    # Автооплата: если владелец задал TON-мнемонику — подписываем и шлём транзакцию сами
    mnemonic = fragment_conf('ton_mnemonic')
    if mnemonic and to_address and amount_nano > 0:
        try:
            sent = _ton_send_payment(mnemonic, to_address, amount_nano, payload_b64)
            logger.info("Fragment autobuy (direct): %s ⭐ для @%s оплачено автоматически, tx=%s",
                        stars, username, sent.get('tx_id'))
            return {"success": True, "tx_id": str(sent.get('tx_id') or ''),
                    "link": pay_link, "paid_automatically": True,
                    "wallet": sent.get('wallet', '')}
        except Exception as exc:
            logger.exception("Fragment autobuy (direct): TON-перевод не отправлен: %s", exc)
            return {"success": False,
                    "error": f"TON-перевод не отправлен: {exc}",
                    "link": pay_link}

    logger.info("Fragment autobuy (direct): %s ⭐ для @%s — создана ссылка на оплату (мнемоника не задана)",
                stars, username)
    return {"success": True, "tx_id": str(tx_id), "link": pay_link,
            "paid_automatically": False, "needs_payment": True}


async def fragment_auto_buy(recipient_username: str, stars_amount: int) -> dict:
    """Автоматическая отправка звёзд получателю через Fragment.

    Возвращает:
        {"success": True,  "tx_id": "...", "link": "..."}   — покупка/транзакция создана
        {"success": False, "error": "..."}                  — автовыдача не выполнена

    Конфигурация читается сначала из /admin (таблица settings), затем из окружения:
        fragment_api_key        / FRAGMENT_API_KEY         — способ А (API-сервис);
        fragment_hash           / FRAGMENT_HASH            — способ Б (hash fragment.com/api?hash=);
        fragment_cookies        / FRAGMENT_COOKIES         — способ Б (Cookie сессии);
        fragment_ton_mnemonic   / FRAGMENT_TON_MNEMONIC    — кошелёк для оплаты TON;
        fragment_mode           = 'auto' | 'manual'        — режим работы.

    Если доступ не настроен или включён ручной режим — честно сообщает об этом,
    и админ выдаёт заказ вручную (бот не падает).
    """
    recipient_username = (recipient_username or '').strip()
    if recipient_username and not recipient_username.startswith('@'):
        recipient_username = '@' + recipient_username

    try:
        stars_amount = int(stars_amount)
    except (TypeError, ValueError):
        return {"success": False, "error": f"Некорректное количество звёзд: {stars_amount!r}"}

    if not recipient_username or not USERNAME_RE.match(recipient_username):
        return {"success": False, "error": f"Некорректный @username: {recipient_username!r}"}
    if stars_amount <= 0:
        return {"success": False, "error": "Количество звёзд должно быть больше нуля"}

    mode = (get_setting('fragment_mode') or 'auto').strip().lower()
    if mode == 'manual':
        logger.info("Fragment autobuy: ручной режим — заказ %s ⭐ для %s выдаётся вручную",
                    stars_amount, recipient_username)
        return {"success": False, "error": "Включён ручной режим автовыдачи"}

    api_key = fragment_conf('api_key')
    frag_hash = fragment_conf('hash')
    cookies = fragment_conf('cookies')

    if not api_key and not (frag_hash and cookies):
        logger.warning(
            "Fragment autobuy: доступ не настроен (нет API-ключа либо hash+cookie) — "
            "%s ⭐ для %s выдаётся вручную", stars_amount, recipient_username)
        return {"success": False,
                "error": "Автовыдача не настроена — задайте её в /admin → 🤖 Автовыдача Fragment"}

    try:
        if api_key:
            return await asyncio.to_thread(
                _fragment_buy_via_api, recipient_username, stars_amount, api_key)
        return await asyncio.to_thread(
            _fragment_buy_direct, recipient_username, stars_amount, frag_hash, cookies)
    except Exception as exc:
        logger.exception("Fragment autobuy: ошибка запроса для %s: %s", recipient_username, exc)
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}



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

    # Автовыдача звёзд через Fragment (для звёздных заказов)
    try:
        stars = int(row.get('stars') or 0)
    except (TypeError, ValueError):
        stars = 0

    autobuy_line = "ℹ️ Premium-заказ: выдаётся вручную."
    result = {}
    if stars > 0:
        await query.edit_message_text(
            f"⏳ <b>Заказ #{order_id}: пробую автовыдачу через Fragment…</b>\n\n"
            f"🎯 Получатель: <code>{row['recipient']}</code>\n"
            f"⭐ Звёзды: {stars}",
            parse_mode='HTML'
        )
        result = await fragment_auto_buy(row['recipient'], stars)
        logger.info("Fragment autobuy по заказу #%s (%s ⭐): %s", order_id, stars, result)
        if result.get('success') and result.get('paid_automatically'):
            tx_id = html.escape(str(result.get('tx_id') or '—'))
            autobuy_line = (f"✅ Автовыдача Fragment: <b>оплачено автоматически</b>\n"
                            f"🔗 tx: <code>{tx_id}</code>")
        elif result.get('success'):
            tx_id = html.escape(str(result.get('tx_id') or '—'))
            autobuy_line = (f"✅ Автовыдача Fragment: счёт создан (tx: <code>{tx_id}</code>)\n"
                            "💎 Осталось оплатить по кнопке ниже — 1 клик.")
        else:
            error = html.escape(str(result.get('error') or 'неизвестная ошибка'))
            autobuy_line = (f"⚠️ Автовыдача не выполнена: <i>{error}</i>\n"
                            "Выдайте заказ по кнопке ниже и проверьте настройки Fragment.")

    keyboard = []
    if stars > 0:
        keyboard.append([InlineKeyboardButton(
            "💎 Выдать на Fragment в 1 клик",
            url=str(result.get('link') or fragment_buy_page_link(row['recipient'], stars)))])
    keyboard.append([InlineKeyboardButton("🔙 В админ-меню", callback_data='adm_back')])

    await query.edit_message_text(
        f"✅ <b>Заказ #{order_id} выполнен!</b>\n\n"
        f"🎯 Получатель: <code>{row['recipient']}</code>\n"
        f"📦 {free_text_of(row)}\n"
        f"💰 {row['total']}₽\n\n"
        f"{autobuy_line}",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
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
        [InlineKeyboardButton("📝 Редактор текстов (Все экраны)", callback_data='adm_texts_menu')],
        [InlineKeyboardButton("🔘 Изменить названия кнопок (Блоков)", callback_data='adm_buttons_menu')],
        [InlineKeyboardButton("💳 Настроить ссылки оплаты", callback_data='adm_pay_links')],
        [InlineKeyboardButton("🤖 Автовыдача Fragment", callback_data='adm_fragment')],
        [InlineKeyboardButton("☁️ Облако и База данных", callback_data='adm_cloud')],
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

    conn = connect_db()
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

    conn = connect_db()
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

    conn = connect_db()
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


# === Редактор текстов всех экранов (ШАГ 3.1) ===
async def admin_texts_menu(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    keyboard = []
    for key in TEXT_DEFAULTS:
        customized = "✏️ " if get_setting('txt_' + key) or (
            key == 'welcome' and get_setting('custom_welcome_text')) else ""
        keyboard.append([InlineKeyboardButton(
            f"{customized}{SCREEN_TITLES[key]}", callback_data=f'adm_txt_{key}')])
    keyboard.append([InlineKeyboardButton("🔙 В админ-меню", callback_data='adm_back')])

    await query.edit_message_text(
        "📝 <b>Редактор текстов — все экраны бота</b>\n\n"
        "Выберите экран, текст которого хотите изменить.\n"
        "✏️ — текст уже изменён вами.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def admin_edit_screen(query, context, key):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    context.user_data['state'] = 'WAIT_SCREEN_TEXT'
    context.user_data['edit_key'] = key

    saved = get_setting('txt_' + key)
    if saved is None and key == 'welcome':
        saved = get_setting('custom_welcome_text')
    current = saved if saved is not None else TEXT_DEFAULTS[key]
    status = "✏️ изменён вами" if saved is not None else "стандартный"

    await query.edit_message_text(
        f"📝 <b>{SCREEN_TITLES[key]}</b>\n\n"
        f"<b>Текущий текст</b> ({status}):\n"
        f"───────────────\n{current}\n"
        f"───────────────\n\n"
        f"<b>Доступные переменные:</b> <code>{SCREEN_VARS[key]}</code>\n\n"
        "Отправьте в чат новый текст — можно прямо в Telegram оформить его: "
        "<b>жирный</b>, <i>курсив</i>, цитаты, ссылки и любые <b>Премиум-эмодзи</b>!\n"
        "Переменные подставятся автоматически (если уберёте их из текста — просто не будет подстановки).",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Сбросить этот текст по умолчанию", callback_data=f'adm_txtrst_{key}')
        ], [
            InlineKeyboardButton("🔙 К списку экранов", callback_data='adm_texts_menu')
        ]]),
        parse_mode='HTML'
    )


async def admin_text_reset(query, context, key):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    delete_setting('txt_' + key)
    if key == 'welcome':
        delete_setting('custom_welcome_text')  # сбрасываем и старый ключ
    context.user_data['state'] = ''
    logger.info("Админ %s сбросил текст экрана '%s' на стандартный", query.from_user.id, key)
    await query.edit_message_text(
        f"🔄 Текст экрана «{SCREEN_TITLES[key]}» сброшен на стандартный.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 К списку экранов", callback_data='adm_texts_menu')
        ]]),
        parse_mode='HTML'
    )


# === Редактор названий кнопок (ШАГ 3.2) ===
async def admin_buttons_menu(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    keyboard = []
    for key in BUTTON_DEFAULTS:
        customized = "✏️ " if get_setting('btn_' + key) else ""
        keyboard.append([InlineKeyboardButton(
            f"{customized}{BUTTON_TITLES[key]}", callback_data=f'adm_btn_{key}')])
    keyboard.append([InlineKeyboardButton("🔙 В админ-меню", callback_data='adm_back')])

    await query.edit_message_text(
        "🔘 <b>Изменение названий кнопок (блоков)</b>\n\n"
        "Выберите кнопку, название которой хотите изменить.\n"
        "✏️ — название уже изменено вами.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def admin_edit_button(query, context, key):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    context.user_data['state'] = 'WAIT_BUTTON_TEXT'
    context.user_data['edit_key'] = key

    saved = get_setting('btn_' + key)
    current = btn_label(key)  # уже очищено от HTML-тегов — в кнопках теги недопустимы
    status = "✏️ изменено вами" if saved is not None else "стандартное"
    icon_now = "есть" if btn_icon(key) else "обычный текст/смайлик"
    vars_hint = f"\n<b>Переменные:</b> <code>{BUTTON_VARS[key]}</code>" if key in BUTTON_VARS else ""

    await query.edit_message_text(
        f"🔘 <b>{BUTTON_TITLES[key]}</b>\n\n"
        f"<b>Текущее название</b> ({status}):\n"
        f"{html.escape(current)}\n"
        f"<b>Премиум-иконка:</b> {icon_now}{vars_hint}\n\n"
        "Просто отправьте в чат новое название кнопки с любым Премиум-эмодзи "
        "или обычным смайликом!\n"
        "Премиум-эмодзи станет иконкой кнопки, обычный смайлик — просто частью текста.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Сбросить название по умолчанию", callback_data=f'adm_btnrst_{key}')
        ], [
            InlineKeyboardButton("🔙 К списку кнопок", callback_data='adm_buttons_menu')
        ]]),
        parse_mode='HTML'
    )


async def admin_button_reset(query, context, key):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    delete_setting('btn_' + key)
    delete_setting('btn_icon_' + key)
    context.user_data['state'] = ''
    logger.info("Админ %s сбросил кнопку '%s' (текст + иконка) на стандарт",
                query.from_user.id, key)
    await query.edit_message_text(
        f"🔄 Кнопка «{BUTTON_TITLES[key]}» сброшена на стандарт:\n"
        f"Текст: {html.escape(BUTTON_DEFAULTS[key])}\n"
        f"Иконка: {'премиум' if BUTTON_ICON_DEFAULTS.get(key) else 'без иконки'}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 К списку кнопок", callback_data='adm_buttons_menu')
        ]]),
        parse_mode='HTML'
    )


# === Совместимость: старые кнопки админки ссылаются на редактор welcome ===
async def admin_edit_text(query, context):
    await admin_edit_screen(query, context, 'welcome')


async def admin_reset_text(query, context):
    await admin_text_reset(query, context, 'welcome')


# === Оплата: только кнопки, без ввода цифр ===
PAY_TYPES = {
    'sbp': ('sbp_url', 'СБП', '💸'),
    'card': ('card_url', 'Карта РФ', '💳'),
    'crypto': ('crypto_url', 'Криптовалюта', '💎'),
}


async def admin_pay_links(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    context.user_data['state'] = ''
    context.user_data.pop('pay_type', None)

    sbp = get_payment_url('sbp_url', SBP_PAYMENT_URL)
    card = get_payment_url('card_url', CARD_PAYMENT_URL)
    crypto = get_payment_url('crypto_url', CRYPTO_PAYMENT_URL)

    await query.edit_message_text(
        "💳 <b>Настройка ссылок оплаты</b>\n\n"
        f"💸 СБП: <code>{html.escape(sbp)}</code>\n"
        f"💳 Карта РФ: <code>{html.escape(card)}</code>\n"
        f"💎 Крипта: <code>{html.escape(crypto)}</code>\n\n"
        "Нажмите нужную кнопку — и отправьте новую ссылку сообщением (без цифр и кодов).",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Изменить ссылку СБП", callback_data='adm_setpay_sbp')],
            [InlineKeyboardButton("💳 Изменить ссылку Карты РФ", callback_data='adm_setpay_card')],
            [InlineKeyboardButton("💎 Изменить ссылку Крипты", callback_data='adm_setpay_crypto')],
            [InlineKeyboardButton("🔙 Назад", callback_data='adm_back')],
        ]),
        parse_mode='HTML'
    )


async def admin_setpay(query, context, pay_type):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    key, title, emoji = PAY_TYPES[pay_type]
    context.user_data['state'] = 'WAIT_PAY_URL'
    context.user_data['pay_type'] = pay_type
    current = get_payment_url(key, '')
    logger.info("Админ %s выбрал изменение ссылки %s", query.from_user.id, key)

    if pay_type == 'crypto':
        hint = ("Отправьте ссылку <code>https://…</code> или просто адрес TON-кошелька\n"
                "(<code>UQ…</code> / <code>EQ…</code>, 48 символов) — бот сам сделает кнопку оплаты в Tonkeeper.")
    else:
        hint = ("Подойдёт ссылка ЮMoney (<code>https://yoomoney.ru/to/…</code>) — сумма заказа "
                "подставится сама. Либо касса Platega/Lava/AAIO/CloudTips: отправьте ссылку "
                "<code>https://…</code> (к ней добавятся order_id и amount).")

    await query.edit_message_text(
        f"{emoji} <b>Новая ссылка для «{title}»</b>\n\n"
        f"Сейчас: <code>{html.escape(current or '— не задана —')}</code>\n\n"
        f"{hint}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 К ссылкам оплаты", callback_data='adm_pay_links')
        ]]),
        parse_mode='HTML'
    )


# === Fragment: настройки автовыдачи ===
FRAGMENT_FIELDS = {
    'api': ('fragment_api_key', 'API-ключ сервиса автовыдачи'),
    'hash': ('fragment_hash', 'hash из Fragment (из ссылки fragment.com/api?hash=...)'),
    'cookies': ('fragment_cookies', 'Cookie авторизованной сессии Fragment'),
    'mnemonic': ('fragment_ton_mnemonic', 'TON-мнемоника кошелька'),
}

# Соответствие поля формы -> имя в fragment_conf()/переменных окружения
FRAGMENT_CONF_NAMES = {
    'api': 'api_key',
    'hash': 'hash',
    'cookies': 'cookies',
    'mnemonic': 'ton_mnemonic',
}


def fragment_conf(name: str, default: str = "") -> str:
    """Значение из /admin (settings), затем из переменных окружения, затем default."""
    return (get_setting('fragment_' + name)
            or os.getenv('FRAGMENT_' + name.upper())
            or default or '').strip()


def _mask(value: str) -> str:
    if not value:
        return "— не задано —"
    return f"••••{value[-4:]}" if len(value) > 4 else "••••"


async def admin_fragment(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    context.user_data['state'] = ''
    mode = (get_setting('fragment_mode') or 'auto').strip()
    mode_title = "Автоматический" if mode == 'auto' else "Ручной"
    api_key = fragment_conf('api_key')
    frag_hash = fragment_conf('hash')
    cookies = fragment_conf('cookies')
    mnemonic = fragment_conf('ton_mnemonic')

    ready = bool(api_key or (frag_hash and cookies))
    status = "🟢 Включена" if (mode == 'auto' and ready) else "🟡 Ручной режим"

    await query.edit_message_text(
        "🤖 <b>Автовыдача звёзд через Fragment</b>\n\n"
        f"<b>Статус:</b> {status}\n"
        f"<b>Режим:</b> {mode_title}\n\n"
        f"🔑 API-ключ: <code>{_mask(api_key)}</code>\n"
        f"🔗 hash: <code>{_mask(frag_hash)}</code>\n"
        f"🍪 Cookie: <code>{_mask(cookies)}</code>\n"
        f"🧩 TON-мнемоника: <code>{_mask(mnemonic)}</code>\n\n"
        "Нажмите кнопку и отправьте значение сообщением — код вводить не нужно.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"🔁 Режим: {'Автоматический' if mode == 'auto' else 'Ручной'}",
                callback_data='adm_fragment_mode')],
            [InlineKeyboardButton("🔑 Задать API-ключ", callback_data='adm_fragment_set_api')],
            [InlineKeyboardButton("🔗 Задать hash Fragment", callback_data='adm_fragment_set_hash')],
            [InlineKeyboardButton("🍪 Задать Cookie Fragment", callback_data='adm_fragment_set_cookies')],
            [InlineKeyboardButton("🧩 Задать TON-мнемонику", callback_data='adm_fragment_set_mnemonic')],
            [InlineKeyboardButton("🗑 Очистить ключи Fragment", callback_data='adm_fragment_clear')],
            [InlineKeyboardButton("🔙 Назад", callback_data='adm_back')],
        ]),
        parse_mode='HTML'
    )


async def admin_fragment_mode(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return
    mode = (get_setting('fragment_mode') or 'auto').strip()
    new_mode = 'manual' if mode == 'auto' else 'auto'
    set_setting('fragment_mode', new_mode)
    logger.info("Админ %s переключил режим Fragment на %s", query.from_user.id, new_mode)
    await query.answer("Режим: " + ("Автоматический" if new_mode == 'auto' else "Ручной"))
    await admin_fragment(query, context)


async def admin_fragment_prompt(query, context, field):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    setting_key, title = FRAGMENT_FIELDS[field]
    context.user_data['state'] = 'WAIT_FRAGMENT_VALUE'
    context.user_data['fragment_field'] = field
    current = fragment_conf(FRAGMENT_CONF_NAMES[field])

    await query.edit_message_text(
        f"🤖 <b>{title}</b>\n\n"
        f"Сейчас: <code>{_mask(current)}</code>\n\n"
        "Отправьте значение сообщением.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 К автовыдаче", callback_data='adm_fragment')
        ]]),
        parse_mode='HTML'
    )


async def admin_fragment_clear(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return
    for setting_key, _ in FRAGMENT_FIELDS.values():
        delete_setting(setting_key)
    logger.info("Админ %s очистил ключи Fragment", query.from_user.id)
    await query.answer("Ключи Fragment очищены")
    await admin_fragment(query, context)


# === Облако: БД, деплой, self-ping ===
SELF_PING_INTERVAL = 480  # 8 минут — меньше 15 минут «сна» Render Free
DEFAULT_SELF_PING_URL = "https://stars-seller-bot.onrender.com/"


def self_ping_target() -> str:
    """Внешний адрес сервиса для авто-пинга."""
    return (os.getenv('RENDER_EXTERNAL_URL')
            or get_setting('self_ping_url')
            or DEFAULT_SELF_PING_URL).strip()


def render_deploy_hook() -> str:
    return (get_setting('render_deploy_hook')
            or os.getenv('RENDER_DEPLOY_HOOK')
            or '').strip()


def self_ping_once() -> bool:
    """Один GET-запрос по внешнему адресу сервиса (держит Render контейнер живым)."""
    import urllib.request as _urlreq

    url = self_ping_target()
    try:
        with _urlreq.urlopen(url, timeout=15) as response:
            body = response.read(200).decode('utf-8', errors='replace')
        logger.info("Self-ping OK: %s -> %s (%s)", url, getattr(response, 'status', 200), body[:80])
        return True
    except Exception as exc:
        logger.warning("Self-ping не удался (%s): %s", url, exc)
        return False


def start_self_ping_loop():
    """Демон-поток: раз в 8 минут бьёт по внешнему URL, чтобы Render Free не засыпал 24/7."""
    def loop():
        time.sleep(20)  # даём health-серверу подняться
        while True:
            try:
                self_ping_once()
            except Exception as exc:  # поток не должен падать ни при каких условиях
                logger.warning("Self-ping: непредвиденная ошибка: %s", exc)
            time.sleep(SELF_PING_INTERVAL)

    target = self_ping_target()
    logger.info("Self-ping 24/7 запущен: %s каждые %s сек", target, SELF_PING_INTERVAL)
    threading.Thread(target=loop, daemon=True, name="self-ping").start()


def trigger_render_deploy() -> dict:
    """POST на Render Deploy Hook — перезапуск облака одной кнопкой из Telegram."""
    import urllib.request as _urlreq

    hook = render_deploy_hook()
    if not hook.startswith('http'):
        return {"success": False,
                "error": "Deploy Hook не задан — /admin → ☁️ Облако и База данных"}
    try:
        request = _urlreq.Request(hook, data=b'', method='POST')
        with _urlreq.urlopen(request, timeout=30) as response:
            body = response.read(300).decode('utf-8', errors='replace')
        logger.info("Render deploy hook вызван: %s %s", response.status, body[:150])
        return {"success": True, "status": response.status, "response": body}
    except Exception as exc:
        logger.error("Render deploy hook не сработал: %s", exc)
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


async def admin_cloud(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return

    context.user_data['state'] = ''
    db_kind = "PostgreSQL (постоянная)" if DB_IS_POSTGRES else "SQLite (стирается при передеплое)"
    hook = render_deploy_hook()
    ping = self_ping_target()

    await query.edit_message_text(
        "☁️ <b>Облако и база данных</b>\n\n"
        f"🗄 <b>База:</b> {db_kind}\n"
        f"<code>{html.escape(mask_secret(DATABASE_URL) if DB_IS_POSTGRES else DB_NAME)}</code>\n\n"
        f"🚀 <b>Deploy Hook:</b> <code>{html.escape(mask_secret(hook))}</code>\n"
        f"📡 <b>Self-ping:</b> <code>{html.escape(ping)}</code>\n"
        f"⏱ <b>Интервал:</b> каждые {SELF_PING_INTERVAL // 60} мин (Render не засыпает)\n\n"
        "Нажмите кнопку и отправьте значение сообщением — ничего печатать вручную не нужно.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗄 Задать DATABASE_URL (Neon Postgres)",
                                  callback_data='adm_set_db_url')],
            [InlineKeyboardButton("🚀 Задать Render Deploy Hook",
                                  callback_data='adm_set_deploy_hook')],
            [InlineKeyboardButton("🔄 Перезапустить сервер Render",
                                  callback_data='adm_restart_render')],
            [InlineKeyboardButton("📡 Пропинговать сейчас", callback_data='adm_ping_now')],
            [InlineKeyboardButton("🔙 Назад", callback_data='adm_back')],
        ]),
        parse_mode='HTML'
    )


async def admin_set_db_url(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return
    context.user_data['state'] = 'WAIT_DB_URL'
    await query.edit_message_text(
        "🗄 <b>DATABASE_URL (Neon Postgres)</b>\n\n"
        f"Сейчас: <code>{html.escape(mask_secret(DATABASE_URL))}</code>\n\n"
        "Отправьте строку подключения из Neon (начинается с <code>postgresql://</code>).\n"
        "База подключится <b>сразу, без перезапуска</b>: заказы перестанут стираться.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 К облаку", callback_data='adm_cloud')
        ]]),
        parse_mode='HTML'
    )


async def admin_set_deploy_hook(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return
    context.user_data['state'] = 'WAIT_DEPLOY_HOOK'
    await query.edit_message_text(
        "🚀 <b>Render Deploy Hook</b>\n\n"
        f"Сейчас: <code>{html.escape(mask_secret(render_deploy_hook()))}</code>\n\n"
        "Отправьте ссылку Deploy Hook из Render (вида "
        "<code>https://api.render.com/deploy/srv-...?key=...</code>).\n"
        "После этого кнопка «🔄 Перезапустить сервер Render» будет передеплоивать бот из Telegram.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 К облаку", callback_data='adm_cloud')
        ]]),
        parse_mode='HTML'
    )


async def admin_restart_render(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return
    await query.answer("🚀 Отправляю запрос на передеплой…")
    result = await asyncio.to_thread(trigger_render_deploy)
    if result.get('success'):
        await query.edit_message_text(
            "🚀 <b>Передеплой Render запущен!</b>\n\n"
            f"HTTP {result.get('status')}\n"
            "Через 1–2 минуты сервис поднимется с новым кодом.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 К облаку", callback_data='adm_cloud')
            ]]),
            parse_mode='HTML'
        )
    else:
        await query.edit_message_text(
            f"⚠️ <b>Передеплой не запущен</b>\n\n<i>{html.escape(str(result.get('error')))}</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 Задать Deploy Hook", callback_data='adm_set_deploy_hook')
            ], [
                InlineKeyboardButton("🔙 К облаку", callback_data='adm_cloud')
            ]]),
            parse_mode='HTML'
        )


async def admin_ping_now(query, context):
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет прав", show_alert=True)
        return
    ok = await asyncio.to_thread(self_ping_once)
    await query.answer("✅ Пинг прошёл — контейнер активен" if ok else "⚠️ Пинг не удался, смотрите лог",
                       show_alert=True)


# === Возврат в меню ===
async def back_to_menu(query, context):
    context.user_data.clear()
    await query.edit_message_text(
        main_text(),
        reply_markup=InlineKeyboardMarkup(main_keyboard()),
        parse_mode='HTML'
    )


# === HealthCheck HTTP-сервер (для 24/7 бесплатного облачного хостинга) ===
BOT_VERSION = "2026-10-28.6"


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"Bot is running 24/7 OK (v{BOT_VERSION})".encode())

    def log_message(self, format, *args):
        pass  # не засоряем лог


def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info("HealthCheck HTTP-сервер запущен на порту %s (для 24/7 Free Cloud)", port)
    server.serve_forever()


async def _post_init(app):
    """Узнаём @username бота, чтобы запретить заказ на самого себя."""
    global BOT_USERNAME
    try:
        me = await app.bot.get_me()
        BOT_USERNAME = (me.username or '').strip()
        logger.info("Бот: @%s (id=%s)", BOT_USERNAME, me.id)
    except Exception as exc:
        logger.warning("Не удалось получить username бота: %s", exc)


# === Main ===
def main():
    init_db()
    ensure_database_from_settings()
    logger.info("Администратор (ADMIN_ID): %s", ADMIN_ID)
    logger.info(
        "Дефолты зашиты в код: %s экранов текста и %s кнопок — БД необязательна, "
        "settings используется как кэш правок", len(TEXT_DEFAULTS), len(BUTTON_DEFAULTS))

    threading.Thread(target=start_health_server, daemon=True).start()
    start_self_ping_loop()

    app = Application.builder().token(TOKEN).post_init(_post_init).build()

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