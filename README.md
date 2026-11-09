# Криптообзор в Telegram

Бесплатная ежедневная подборка до пяти свежих заголовков по крипторегулированию, криптобанкам, ЦФА и отраслевым форумам в России. Используются RSS-результаты Google News, без платного API. Это не полноценный AI-анализ: даты мероприятий и заявленные спикеры требуют проверки на сайтах организаторов.

## Установка через GitHub (в том числе с телефона)

1. Распакуйте ZIP. В репозитории GitHub `Skr1tek/crypto-news-bot` добавьте `bot.py`, `requirements.txt`, `tests/test_bot.py` и `.github/workflows/daily.yml`, сохраняя пути. Удобнее использовать веб-редактор `https://github.dev/Skr1tek/crypto-news-bot` (открывается в браузере; на телефоне включите «Версия для ПК»). В нём можно создавать папки и файлы и затем выполнить Commit & Push. **ZIP в GitHub нельзя просто загрузить как один файл: он не распакуется автоматически.**
2. Откройте репозиторий → Settings → Secrets and variables → Actions → New repository secret. Добавьте `TELEGRAM_BOT_TOKEN` (токен от @BotFather) и `TELEGRAM_CHAT_ID` (числовой ID вашего личного Telegram-чата). Никому не отправляйте токен и не записывайте его в публичный репозиторий.
3. Для получения личного chat ID можно обратиться к @userinfobot в Telegram (это сторонний бот), либо получить ID через Telegram Bot API самостоятельно. Боту @News_crp_bot необходимо отправить `/start` до первой рассылки.
4. В GitHub → Actions → Crypto news to Telegram → Run workflow запустите проверку. Если в логах появилась `Telegram digest delivered`, отправка прошла.
5. Автоматический запуск назначен на 05:00 UTC = 08:00 МСК ежедневно. GitHub не гарантирует запуск строго по минутам. В публичных репозиториях GitHub Actions доступен без оплаты в рамках текущих условий GitHub.

## Локальная проверка

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Для ручной отправки необходимо задать переменные окружения `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID`, затем запустить `python bot.py`.
