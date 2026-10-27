import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
import sqlite3
import math

TOKEN = "8646243333:AAEDdzL5wG7pWTi4H8O8fXjXfyYk-GDS-JA"
DB_NAME = "bot.db"

# === Настройки ===
REVIEWS_LINK = "https://t.me/starsdefolt"
SBP_PAYMENT_LINK = "https://pay.sinaim.ru/checkout"
CARD_PAYMENT_LINK = "https://pay.sinaim.ru/checkout"
CRYPTO_PAYMENT_LINK = "https://t.me/CryptoBot?start=payment"

# === Инициализация БД ===
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS catalog (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, text TEXT)''')
    c.execute("SELECT COUNT(*) FROM catalog")
    if c.fetchone()[0] == 0:
        stars = [("Звезда уровня 1", 50), ("Звезда уровня 2", 100),
                 ("Звезда уровня 3", 250), ("Звезда уровня 5", 1000)]
        c.executemany("INSERT INTO catalog (name, price) VALUES (?, ?)", stars)
    conn.commit()
    conn.close()

# === Меню ===
async def start(update: Update, context):
    keyboard = [
        [InlineKeyboardButton("🌟 Каталог звёзд", callback_data='catalog')],
        [InlineKeyboardButton("⭐ Отзывы покупателей", callback_data='reviews')],
        [InlineKeyboardButton("💳 Оплата", callback_data='payment')]
    ]
    await update.message.reply_text(
        '🔮 Добро пожаловать!\n\nЭто магазин звёздных покровителей.\nВыберите нужный раздел:',
        reply_markup=InlineKeyboardMarkup(keyboard))

# === Обработчики кнопок ===
async def button(update: Update, context):
    query = update.callback_query
    await query.answer()
    handlers = {
        'catalog': show_catalog_with_calculator,
        'reviews': show_reviews,
        'payment': show_payment,
        'back': back_to_menu,
        'set_stars': show_star_input,
        'set_rates': show_rate_edit,
        'calculate': calculate_cost,
        'payment_sbp': payment_sbp,
        'payment_card': payment_card,
        'payment_crypto': payment_crypto
    }
    handler = handlers.get(query.data)
    if handler:
        await handler(query, context)

# === Блок 1: Каталог + калькулятор ===
async def show_catalog_with_calculator(query, context):
    default_stars = 50
    default_rate = 1.4
    context.user_data.setdefault('stars', default_stars)
    context.user_data.setdefault('rate', default_rate)
    await update_calculator(query, context)

async def show_star_input(query, context):
    await query.message.reply_text("Введите количество звёзд (чётное число, минимум 50):")

async def show_rate_edit(query, context):
    current_rate = context.user_data.get('rate', 1.2)
    await query.message.reply_text(f"Текущий курс: {current_rate}₽ за звезду\nВведите новый курс:")

async def update_calculator(query, context):
    stars = context.user_data.get('stars', 50)
    rate = context.user_data.get('rate', 1.2)
    total = round(stars * rate, 2)
    
    text = f"""🌟 **Каталог звёзд с калькулятором**

🔢 **Количество звёзд:** {stars}
💱 **Курс (1 звезда =):** {rate}₽
💰 **Стоимость:** {total}₽"""
    
    keyboard = [
        [InlineKeyboardButton("📝 Изменить количество звёзд", callback_data='set_stars')],
        [InlineKeyboardButton("💱 Изменить курс", callback_data='set_rates')],
        [InlineKeyboardButton("🧮 Пересчитать", callback_data='calculate')],
        [InlineKeyboardButton("💳 Оплатить", callback_data='payment')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def calculate_cost(query, context):
    stars = context.user_data.get('stars', 50)
    rate = context.user_data.get('rate', 1.2)
    total = round(stars * rate, 2)
    context.user_data['total'] = total
    await update_calculator(query, context)

# === Блок 2: Отзывы ===
async def show_reviews(query, context):
    text = """⭐ **Отзывы покупателей**

Нажмите кнопку ниже, чтобы перейти на наш канал с отзывами:"""
    
    keyboard = [
        [InlineKeyboardButton("📢 Перейти к отзывам", url=REVIEWS_LINK)],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# === Блок 3: Оплата ===
async def show_payment(query, context):
    total = context.user_data.get('total', 60)
    text = f"""💳 **Оплата**

💰 **Сумма к оплате:** {total}₽

Выберите способ оплаты:"""
    
    keyboard = [
        [InlineKeyboardButton("💸 СБП (Система Быстрых Платежей)", callback_data='payment_sbp')],
        [InlineKeyboardButton("💳 Банковская карта (RU)", callback_data='payment_card')],
        [InlineKeyboardButton("🔣 Криптовалюта", callback_data='payment_crypto')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def payment_sbp(query, context):
    keyboard = [
        [InlineKeyboardButton("💳 Перейти к оплате СБП", url=SBP_PAYMENT_LINK)],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.edit_message_text("💸 Оплата через СБП:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def payment_card(query, context):
    keyboard = [
        [InlineKeyboardButton("💳 Перейти к оплате картой", url=CARD_PAYMENT_LINK)],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.edit_message_text("💳 Оплата банковской картой:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def payment_crypto(query, context):
    keyboard = [
        [InlineKeyboardButton("🔣 Перейти к оплате криптой", url=CRYPTO_PAYMENT_LINK)],
        [InlineKeyboardButton("🔙 Назад", callback_data='back')]
    ]
    await query.edit_message_text("🔣 Оплата криптовалютой:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def back_to_menu(query, context):
    keyboard = [
        [InlineKeyboardButton("🌟 Каталог звёзд", callback_data='catalog')],
        [InlineKeyboardButton("⭐ Отзывы покупателей", callback_data='reviews')],
        [InlineKeyboardButton("💳 Оплата", callback_data='payment')]
    ]
    await query.edit_message_text('🔮 Главное меню\n\nВыберите нужный раздел:', reply_markup=InlineKeyboardMarkup(keyboard))

# === Main ===
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    print("Бот запущен...")
    app.run_polling()

if __name__ == '__main__':
    main()