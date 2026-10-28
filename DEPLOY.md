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
| `DB_PATH` | путь к файлу БД (необязательно) |

## Render.com (Blueprint — самый простой путь)

1. https://dashboard.render.com → **New → Blueprint** → подключить GitHub-репозиторий `lekaleo/stars-seller-bot`.
2. Render прочитает `render.yaml` и создаст web-сервис (Docker, free).
3. Вписать `BOT_TOKEN` (и при желании ссылки оплаты) → **Apply/Deploy**.
4. Проверка: открыть `https://<имя>.onrender.com/` — должно быть `Bot is running 24/7 OK`.

Важно: бесплатный web-сервис Render засыпает после **15 минут** без входящих HTTP-запросов.
Чтобы бот не спал, поставьте бесплатный пинг раз в 10 минут (cron-job.org или UptimeRobot)
на адрес `https://<имя>.onrender.com/`.

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
