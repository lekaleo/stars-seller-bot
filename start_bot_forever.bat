@echo off
chcp 65001 >nul
title Stars Seller Bot (auto-restart)
cd /d E:\AI\test_bot
echo [%date% %time%] Запуск бота с автоперезапуском...

:loop
python bot.py >> bot.log 2>&1
echo [%date% %time%] Бот упал с кодом %errorlevel%. Перезапуск через 5 секунд...
timeout /t 5 /nobreak >nul
goto loop
