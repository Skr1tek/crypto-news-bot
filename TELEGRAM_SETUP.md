# Telegram management: deployment and operation

## Architecture and cost

`python control.py` runs one long-polling process, the daily scheduler, and the existing RSS digest engine. No public port, webhook, paid API, or new Python dependency is required. SQLite on a **persistent local disk** stores configuration, wizard progress, polling offset, schedule slot completion, and confirmed delivery history. A process lock prevents two interactive instances sharing that disk. Run exactly one instance; do not mount SQLite over NFS.

The software can run for free on an already-owned always-on PC, Raspberry Pi, or home server. This repository does **not** provide free hosted compute. Independent 24/7 operation requires an external machine with persistent disk (your server or a provider account; free-tier availability is not guaranteed). A sleeping/ephemeral free web host loses availability or data and is unsuitable without a separate durable database. GitHub Actions is not a permanent Telegram command listener.

The default time is 08:00 Moscow (05:00 UTC). Change it in Telegram via Расписание → Изменить время; enter one or more daily times such as 08:00, 12:00. All times are Moscow time regardless of the server timezone. You may disable automatic sending and still use manual snapshots. RSS collection/delivery runs in a single background worker so Telegram controls stay responsive. Each delivery uses a configuration snapshot: edits and removals apply to the next run, and cannot recall messages already in progress. The loop checks between polling calls (up to about 20 seconds plus network/feed time). On restart it catches up once for the latest due time of the current day; earlier missed days are not replayed. Failed targets retry after five minutes; already confirmed news is skipped. Network uncertainty between Telegram accepting a message and recording it locally can still cause a duplicate: Telegram sendMessage has no idempotency key. Back up the database, including after configuration changes.

## Initial setup (once, no JSON editing needed)

1. Merge the feature branch and install Python 3.13+, then `python -m pip install -r requirements.txt`.
2. Configure `TELEGRAM_BOT_TOKEN` for **@News_crp_bot**, `TELEGRAM_ADMIN_ID` (your numeric user ID), and `NEWS_STATE_PATH=/persistent/news/sent.sqlite3` using the host's secret manager or protected service environment. Never put tokens in code, Git, command-line arguments, screenshots, or logs. Existing GitHub secrets cannot be read back or automatically transferred to another host. Obtain your ID through a trusted Telegram client or a one-time getUpdates inspection locally; do not publish its response.
3. Start `python control.py` with those secrets supplied. Existing webhook must be removed explicitly before polling. Do not run any other getUpdates consumer for this bot.
4. Open the bot and send `/start`. The first operator is **not** automatically trusted: only `TELEGRAM_ADMIN_ID` can use the panel.
5. Choose **Добавить тему**, enter its name, search queries, and keywords. Separate queries/words with commas (legacy semicolons and line breaks also work). Queries perform Google News searches. Keywords filter the resulting headlines: any keyword or word fragment is enough; query text and keywords therefore serve different purposes. A headline must contain at least one keyword. Topics begin without recipients.
6. Make the bot an administrator in the destination group/channel (with posting permission in a channel). You must also be an administrator there. Choose **Добавить получателя → Выбрать канал / Выбрать группу**. Telegram shows its native picker. Alternatively supply a numeric chat_id, never a link or @username. The bot checks the actual chat and both administrators' permissions. For a private recipient, that person must first open the bot and send /start, then give the administrator their numeric chat_id privately. Add that ID through the same recipient wizard. The bot verifies getChat and sendChatAction; blocked/unreachable private chats are refused. This does not grant the recipient management access.
7. Open **Рубрики → Открыть → Получатели** and toggle one or several destinations. An empty selection pauses that topic. Removing a destination removes its assignments. Add it again and explicitly assign topics to resume.

Commands: `/start`, `/help`, `/add`, `/topics`, `/recipient`, `/recipients`, `/schedule`, `/now`, `/cancel`. Edit name, queries, and keywords from the topic card. Topic deletion asks for confirmation. `/cancel` abandons the draft. A new topic is saved only after all three inputs; edits are saved after the replacement field. Selection buttons require a Telegram client supporting request_chat; numeric ID entry is the fallback. Telegram cannot provide bots a complete list of all channels you own.

## Keep the process alive

On Linux, install a service such as the following (adjust paths and create a dedicated unprivileged account):

```ini
[Unit]
Description=Telegram news control
After=network-online.target
Wants=network-online.target

[Service]
User=newsbot
WorkingDirectory=/opt/crypto-news-bot
EnvironmentFile=/etc/newsbot/secrets
ExecStart=/opt/crypto-news-bot/.venv/bin/python control.py
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Store the three settings as NAME=value lines in `/etc/newsbot/secrets`, owned by root with mode 600. Allow the service user write access to the persistent state directory only. Enable/start the service with systemctl. On Windows use Task Scheduler to start the same process at boot under a dedicated account, restart on failure, and protect injected secrets and the data folder with that account's ACL. The machine must stay awake and online.

## Migration and daily Actions compatibility

`python bot.py`, `NEWS_CONFIG_PATH`, `TELEGRAM_CHAT_ID`, `TELEGRAM_TARGETS`, and the original 05:00 UTC daily workflow remain supported. Interactive mode imports the legacy configuration **once** when its database has no control/config record. Later environment/JSON changes do not overwrite Telegram-managed settings. Without legacy settings it starts with the four default rubrics and no recipients. Existing legacy chat IDs are checked before interactive delivery; unsupported aliases should be removed and re-added using the Telegram picker.

Before enabling the interactive service, set repository Actions variable **INTERACTIVE_BOT_ENABLED=true**. The daily workflow then skips delivery, avoiding two independent schedulers and divergent history. Keep this variable unset/false to retain the original daily Actions deployment. GitHub scheduled workflows may be delayed; they never guarantee exact 08:00 execution.

To preserve history, download `sent.sqlite3` from the current bot-state branch privately and copy it to NEWS_STATE_PATH **before the first interactive launch**. Do not commit interactive configuration, chat IDs, or the database into a public repository. Stop the service before migration. For backups use SQLite's backup API (or stop the service and copy the file). Protect backups like the live database. To restore, stop the service, replace the database with a verified backup, restart. Rollback to Actions requires exporting the desired legacy configuration and preserving/importing the sent table; simply reenabling the old workflow uses its old state and settings.

## Verification

`python -m unittest discover -s . -p 'test_*.py' -v`

Automated tests use mocked Telegram/RSS boundaries and real temporary SQLite databases. A real end-to-end check needs the deployment host, bot secret, and administrator ID: verify `/start` is ignored for a second account; create/edit/delete a test topic; pick a test channel; assign/unassign it; restart and check persistence; revoke posting rights and check delivery is refused. No live Telegram publication is performed by the test suite.

Protocol references: https://core.telegram.org/bots/api#keyboardbuttonrequestchat and https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule.


## Если Telegram API заблокирован

Боту нужен доступ к `https://api.telegram.org` непосредственно с компьютера, где он запущен. VPN должен распространяться на весь компьютер или включать Python в маршрутизацию. Расширение браузера, прокси внутри приложения Telegram и MTProto-прокси сами по себе этого не обеспечивают.

Если доступен HTTP/HTTPS-прокси, Python requests поддерживает его через переменную окружения `HTTPS_PROXY`, заданную в том же окне PowerShell перед запуском бота. Адрес и порт берите у своего провайдера прокси. Учетные данные прокси храните как секреты, не публикуйте их в логах. Для SOCKS требуется дополнительный компонент requests[socks]. Не отключайте проверку TLS-сертификатов.

Сообщения запуска теперь различают connection (соединение/DNS/возможная блокировка), timeout, proxy, TLS и ответы Telegram 401/403/404/409/429. Полный адрес запроса и текст ответа сервера скрыты, чтобы исключить утечку токена. Тип сетевой ошибки не доказывает блокировку: для проверки включите рабочий VPN для всего ПК и повторите запуск.


## Расписание, списки и ручной срез

В меню Расписание → Изменить время задайте `08:00, 12:00` или любое другое время по МСК (формат HH:MM, до 12 запусков за сутки). Можно отключить расписание. Настройки сохраняются в той же SQLite. Старый маркер last_daily переносится в слот 08:00: обновление в тот же день не повторяет уже выполненный утренний запуск. После простоя выполняется только последний пропущенный слот текущего дня. Если добавленное время раньше уже выполненного слота этого дня, оно начнет действовать на следующий день. Изменения расписания не отменяют уже начатую отправку.

Запросы и ключевые слова вводятся через запятую; точка с запятой и переносы строк сохранены для совместимости. Запросы — отдельные фразы для поиска Google News, например `банкротство банков, отзыв лицензии банка`. Ключевые слова — фильтр полученных заголовков, например `банкрот, лиценз, АСВ`; достаточно одного совпадения. Сохранённые списки при обновлении не меняются. Запятая внутри фразы трактуется как разделитель.

Кнопка «Срез новостей сейчас» и команда /now запрашивают подтверждение отправки всем настроенным получателям по их рубрикам. Это реальная рассылка, не предварительный просмотр. Используется та же история повторов; если свежих новостей нет, администратор получает пояснение. Повторное нажатие во время работы не создаёт второй срез. Ручной и автоматический запуски выполняются одним рабочим потоком по очереди; ручной срез не отмечает слот расписания выполненным. По завершении администратор получает статус. Неудавшуюся ручную отправку можно повторить; подтверждённые новости не повторяются. Запрос ручного среза сохраняется в SQLite и может возобновиться после перезапуска.

Обновление на установленном сервере: остановите newsbot, выполните `git pull --ff-only origin feature/telegram-admin` в /opt/newsbot, затем запустите службу. Файл /etc/newsbot.env и база /var/lib/newsbot/sent.sqlite3 остаются на месте; заранее сохраните резервную копию базы при остановленной службе. В Telegram отправьте /start, чтобы получить новые кнопки.


## Количество новостей

Меню «Количество новостей» или команда /limit позволяет установить от 1 до 50 новостей. По умолчанию — 5, как в прежних версиях. Лимит общий для бота и применяется отдельно к каждому получателю, суммарно по назначенным ему рубрикам, в каждом ручном или автоматическом запуске. Это верхняя граница: если новых подходящих сюжетов меньше, бот отправит меньше. Большие подборки автоматически делятся на сообщения по ограничению Telegram. Настройка сохраняется в SQLite и применяется со следующего запуска; уже начатая рассылка использует прежний снимок конфигурации. Для старого JSON-режима предусмотрено необязательное поле news_limit с теми же границами.


## Прямые ссылки на публикации

Перед отправкой бот раскрывает ссылку Google News до адреса конкретной статьи издания. Успешные результаты кэшируются в SQLite; для дедупликации сохраняются и исходные RSS-ссылки, и прямые адреса. Бот не скачивает страницы изданий при раскрытии: запросы идут только к news.google.com. Главные страницы, страницы согласия Google, локальные адреса и ссылки с логином/паролем отбрасываются. Если адрес статьи получить не удалось, новость не отправляется и не отмечается доставленной; остальные новости могут отправиться, а автоматический запуск будет повторён через пять минут. Итоговое число новостей может быть меньше лимита. В Google используется внутренний протокол, поэтому его изменения/блокировки могут временно нарушить раскрытие. Прямой адрес не гарантирует доступность сайта из вашей сети или отсутствие платного доступа. Протокол: https://github.com/dariandanciu/google-news-url-decoder .

## ИИ-помощник через OpenRouter

Добавьте в существующий защищённый `/etc/newsbot.env` две строки (прежние настройки сохраняйте):

```
OPENROUTER_API_KEY=ваш_ключ_OpenRouter
OPENROUTER_MODEL=openai/gpt-4.1-mini
```

Пример модели проверен по публичному каталогу OpenRouter на поддержку структурированного ответа. Можно выбрать другой точный ID модели с поддержкой JSON Schema. Нужен действующий ключ OpenRouter и доступный баланс/лимит этой модели; бесплатность не обещается. Установите бюджет ключа в аккаунте OpenRouter. Новых Python-зависимостей нет. После сохранения с правами 600 и перезапуска службы отправьте /start → ИИ-помощник или /ai.

Примеры: «Добавь рубрику Банкротства банков и подбери запросы и ключевые слова», «Измени запросы рубрики ЦФА, добавь цифровые облигации», «Что улучшить в коде бота?». Для неоднозначного запроса ИИ может попросить уточнение. Новая рубрика и изменение существующей сначала показываются как предложение; сохранение — кнопкой. Предложение действует 10 минут, повторная кнопка не применяет его дважды; изменение вручную после запроса ИИ защищено от перезаписи старым предложением. Получатели новой рубрике назначаются вручную.

Доступен только администратору в личном чате. ИИ может создавать/редактировать рубрики после подтверждения и давать советы; он не выполняет команды, не редактирует код на диске, не меняет права/секреты и не имеет доступа к серверным журналам. Вопросы по коду сопровождаются ограниченными выдержками только из bot.py, control.py и links.py. Для внедрения советов по коду нужны отдельная проверка, тесты и обновление проекта.

В OpenRouter и выбранному провайдеру модели передаются ваш запрос, последние 6 сообщений ИИ-диалога, настройки рубрик, число получателей, расписание и лимит новостей; для запросов по коду — выдержки исходников. Chat ID, файлы секретов и окружение в контекст не добавляются. Тексты с распознанным ключом/токеном отклоняются; автоматическое распознавание не заменяет осторожность: никогда не вводите секреты в Telegram. Действуют условия обработки данных OpenRouter и выбранного провайдера. История диалога хранится в вашей SQLite; кнопка «Очистить историю ИИ» удаляет её из бота, но не является запросом удаления данных у внешнего провайдера.

Лимит — 30 запросов в сутки по МСК, один запрос одновременно, до 3000 символов ввода, 2200 выходных токенов. Контекст ограничен; слишком большие настройки нужно сократить. Ошибки также учитываются в дневном лимите. Запрос выполняется отдельным потоком и не блокирует приём команд и рассылку. /cancel отменяет применение ожидаемого ответа и выходит из режима ИИ; уже отправленный запрос может тарифицироваться. Перезапуск службы прерывает ожидание ответа: запросите его заново. Проверки ИИ выполнены на подменённых ответах API; живой платный вызов без ключа не выполнялся.

Документация: https://openrouter.ai/docs/api/reference/overview и https://openrouter.ai/docs/guides/features/structured-outputs .


## Раздельная выдача по рубрикам

Каждая рубрика отправляется отдельным сообщением с её названием в заголовке. Большая рубрика разбивается на несколько сообщений, каждое повторяет название. Лимит новостей по-прежнему суммарный на получателя за запуск, не на каждую рубрику. Один сюжет, подходящий нескольким рубрикам, отправляется один раз: выбирается первая подходящая рубрика в назначениях получателя. Пустые рубрики не отправляются. История прежних отправок сохранена; обновление не пересылает уже доставленные новости ради новых заголовков. Проверьте на следующих новых сюжетах.
