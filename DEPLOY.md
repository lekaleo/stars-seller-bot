# Бесплатный хостинг 24/7 (Render / Koyeb / Hugging Face)

Бот уже готов к облаку:

- слушает HTTP-порт из переменной `PORT` (по умолчанию **8080**) и отвечает `200 OK` на `/` — health check;
- все секреты берутся из переменных окружения (`BOT_TOKEN`, `ADMIN_ID`, ссылки оплаты);
- путь к БД можно задать через `DB_PATH` (по умолчанию `bot.db` рядом с `bot.py`).

## Переменные окружения (обязательные)

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | токен от @BotFather |
| `ADMIN_ID` | `481441820` |
| `SBP_PAYMENT_URL` | ссылка СБП (необязательно) |
| `CARD_PAYMENT_URL` | ссылка карты РФ (необязательно) |
| `CRYPTO_PAYMENT_URL` | ссылка крипты (необязательно) |
| `DB_PATH` | путь к SQLite-файлу (необязательно) |
| `DATABASE_URL` | строка подключения Neon Postgres (необязательно; можно задать прямо в `/admin` → ☁️ Облако и База данных) |
| `FRAGMENT_API_KEY` / `FRAGMENT_HASH` / `FRAGMENT_COOKIES` / `FRAGMENT_TON_MNEMONIC` | доступ к Fragment для автовыдачи звёзд (необязательно, проще задать в `/admin`) |
| `RENDER_DEPLOY_HOOK` | Deploy Hook из Render (можно задать в `/admin` → ☁️ Облако и База данных) |
| `RENDER_EXTERNAL_URL` | внешний адрес сервиса для self-ping (Render подставляет сам) |
| `TONCENTER_API_KEY` | ключ toncenter.com для автооплаты TON (необязательно, без него лимит ниже) |

## Авто-пинг 24/7 — встроен в бота

Внешний cron-job.org больше не нужен: при старте бот запускает демон-поток
`start_self_ping_loop()`, который **каждые 8 минут** делает HTTP GET на
`RENDER_EXTERNAL_URL` (или на сохранённый в `/admin` адрес), поэтому Render Free
видит входящий трафик и **не засыпает**. Проверить вручную можно кнопкой
`📡 Пропинговать сейчас` в `/admin` → ☁️ Облако и База данных.

## Управление облаком и БД из Telegram

`/admin` → **☁️ Облако и База данных**:

| Кнопка | Что делает |
|---|---|
| 🗄 Задать DATABASE_URL (Neon Postgres) | сохраняет строку подключения и **сразу переключает** `connect_db()` на PostgreSQL без перезапуска сервера (таблицы создаются, настройки переносятся) |
| 🚀 Задать Render Deploy Hook | сохраняет ссылку Deploy Hook |
| 🔄 Перезапустить сервер Render | POST на Deploy Hook — передеплой из Telegram в один клик |
| 📡 Пропинговать сейчас | ручной self-ping для проверки, что контейнер жив |

## Постоянное хранение заказов — Neon (бесплатно, навсегда)

Без `DATABASE_URL` заказы пишутся в SQLite и стираются при передеплое.
С `DATABASE_URL` заказы живут в Postgres и не теряются.

1. https://neon.tech → **Sign up with GitHub** (карта не нужна).
2. **New Project** → имя `stars-bot`, регион ближайший (Frankfurt).
3. Скопируйте **Connection string** вида
   `postgresql://user:pass@ep-xxx.eu-central-1.aws.neon.tech/neondb?sslmode=require`
4. Вставьте её в Telegram: `/admin` → **☁️ Облако и База данных** → `🗄 Задать DATABASE_URL`
   — база подключится сразу, без перезапуска и без захода в панель Render.
   (Альтернатива: Render → **Environment** → `DATABASE_URL` → **Save Changes**.)
5. В логах появится `База данных переключена на PostgreSQL`.

Тексты и названия кнопок в БД не нужны: они зашиты в коде (`TEXT_DEFAULTS`/`BUTTON_DEFAULTS`),
а `settings` — лишь необязательный кэш правок админа (при недоступной БД работает память процесса).

## Fragment (автовыдача звёзд)

Кнопка «✅ Выполнено (Выдано)» в `/admin` вызывает `fragment_auto_buy()`.
Настраивается целиком в Telegram: `/admin` → **🤖 Автовыдача Fragment** (ввод кодов не нужен —
нажимаете кнопку и отправляете значение сообщением):

| Параметр | Что это | Способ |
|---|---|---|
| API-ключ | ключ сервиса автовыдачи | А: POST к API-сервису с `X-API-Key` |
| hash + Cookie | `hash` из `fragment.com/api?hash=…` и Cookie авторизованной сессии | Б: прямой 3-шаговый поток `searchStarsRecipient` → `initBuyStarsRequest` → `getBuyStarsLink` |
| TON-мнемоника | 24 слова кошелька Tonkeeper v4r2 | автооплата: транзакция подписывается (`tonsdk`) и отправляется через toncenter |
| Режим | Автоматический / Ручной | переключается кнопкой |

Значения читаются сначала из `/admin` (таблица `settings`), затем из переменных окружения
`FRAGMENT_API_KEY` / `FRAGMENT_HASH` / `FRAGMENT_COOKIES` / `FRAGMENT_TON_MNEMONIC`.
Если доступ не задан или включён ручной режим — функция честно отвечает
«автовыдача не настроена», заказ помечается выполненным и выдаётся вручную.

**Оплата в 1 клик.** Если мнемоника не задана, бот формирует персональную ссылку
`https://app.tonkeeper.com/transfer/<адрес>?amount=<нанотоны>&bin=<payload>` по данным
`transaction.messages[0]` от Fragment, и под карточкой заказа появляется кнопка
**💎 Выдать на Fragment в 1 клик**. Без сессии Fragment кнопка ведёт на
`https://fragment.com/stars/buy?recipient=<username>&quantity=<звёзды>`.

**Автооплата.** Если задана TON-мнемоника, бот сам подписывает перевод (`tonsdk`, кошелёк v4r2),
берёт `seqno` через `https://toncenter.com/api/v2/jsonRPC` и отправляет `sendBoc`;
в отчёте админу появляется `оплачено автоматически` и хеш транзакции.

**Важно:** у Fragment нет публичного API «купить звёзды». Способ Б использует вашу
авторизованную сессию (Cookie) и требует `hash` со страницы покупки звёзд; способ А —
внешний сервис автовыдачи, если он у вас есть. Без этих данных бот работает в ручном режиме.

## Render.com (Blueprint — самый простой путь)

1. https://dashboard.render.com → **New → Blueprint** → подключить GitHub-репозиторий `lekaleo/stars-seller-bot`.
2. Render прочитает `render.yaml` и создаст web-сервис (Docker, free).
3. Вписать `BOT_TOKEN` (и при желании ссылки оплаты) → **Apply/Deploy**.
4. Проверка: открыть `https://<имя>.onrender.com/` — должно быть `Bot is running 24/7 OK`.

Важно: бесплатный web-сервис Render засыпает после **15 минут** без входящих HTTP-запросов.
Бот теперь **сам себя пингует каждые 8 минут** (`start_self_ping_loop()`), поэтому
cron-job.org/UptimeRobot не нужны. Если Render сам не передал `RENDER_EXTERNAL_URL`,
добавьте переменную `RENDER_EXTERNAL_URL=https://<имя>.onrender.com/` (или задайте адрес
в `/admin` → ☁️ Облако и База данных).

## Koyeb (Free instance)

1. https://app.koyeb.com → **Create Service → GitHub** → выбрать репозиторий.
2. Builder: **Dockerfile**. Port: **8080** (HTTP), Health check path: `/`.
3. Instance: **Free**. Регион: Frankfurt / Washington.
4. Environment variables: `BOT_TOKEN`, `ADMIN_ID`, ссылки оплаты.
5. Deploy. Проверка: `https://<app>.koyeb.app/` → `Bot is running 24/7 OK`.

## Hugging Face Spaces (Docker Space)

1. https://huggingface.co/new-space → SDK: **Docker** → Blank.
2. Загрузить файлы репозитория (или подключить GitHub).
3. В Settings → Variables and secrets добавить `BOT_TOKEN`, `ADMIN_ID`.
4. Порт: в Space приложение должно слушать `PORT=7860` — задайте переменную `PORT=7860`.

## Важные предупреждения

- **Только один экземпляр бота.** Если локальный бот ещё запущен, а вы подняли облачный,
  Telegram вернёт `Conflict: terminated by other getUpdates request` и оба будут работать нестабильно.
  Перед деплоем остановите локальный процесс (или `start_bot_forever.bat`).
- **Файловая система бесплатных тарифов эфемерная.** `bot.db` (заказы, отзывы, настройки)
  обнулится при передеплое/рестарте контейнера. Для сохранности нужен persistent disk
  (Render Disk — платно) либо подключение внешней БД (например, бесплатный Postgres на Neon/Supabase).
- Локальный запуск не изменился: `python bot.py` (или `start_bot_forever.bat`).
