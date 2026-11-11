# Telegram management: deployment and operation

## Architecture and cost

`python control.py` runs one long-polling process, the daily scheduler, and the existing RSS digest engine. No public port, webhook, paid API, or new Python dependency is required. SQLite on a **persistent local disk** stores configuration, wizard progress, polling offset, daily completion, and confirmed delivery history. A process lock prevents two interactive instances sharing that disk. Run exactly one instance; do not mount SQLite over NFS.

The software can run for free on an already-owned always-on PC, Raspberry Pi, or home server. This repository does **not** provide free hosted compute. Independent 24/7 operation requires an external machine with persistent disk (your server or a provider account; free-tier availability is not guaranteed). A sleeping/ephemeral free web host loses availability or data and is unsuitable without a separate durable database. GitHub Actions is not a permanent Telegram command listener.

The target time is 08:00 Moscow (05:00 UTC). RSS collection/delivery runs in a single background worker so Telegram controls stay responsive. Each delivery uses a configuration snapshot: edits and removals apply to the next run, and cannot recall messages already in progress. The loop checks between polling calls (up to about 20 seconds plus network/feed time). On restart after 08:00 it catches up once for the current day; earlier missed days are not replayed. Failed targets retry after five minutes; already confirmed news is skipped. Network uncertainty between Telegram accepting a message and recording it locally can still cause a duplicate: Telegram sendMessage has no idempotency key. Back up the database, including after configuration changes.

## Initial setup (once, no JSON editing needed)

1. Merge the feature branch and install Python 3.13+, then `python -m pip install -r requirements.txt`.
2. Configure `TELEGRAM_BOT_TOKEN` for **@News_crp_bot**, `TELEGRAM_ADMIN_ID` (your numeric user ID), and `NEWS_STATE_PATH=/persistent/news/sent.sqlite3` using the host's secret manager or protected service environment. Never put tokens in code, Git, command-line arguments, screenshots, or logs. Existing GitHub secrets cannot be read back or automatically transferred to another host. Obtain your ID through a trusted Telegram client or a one-time getUpdates inspection locally; do not publish its response.
3. Start `python control.py` with those secrets supplied. Existing webhook must be removed explicitly before polling. Do not run any other getUpdates consumer for this bot.
4. Open the bot and send `/start`. The first operator is **not** automatically trusted: only `TELEGRAM_ADMIN_ID` can use the panel.
5. Choose **Добавить тему**, enter its name, search queries, and keywords. Separate queries/words with semicolons. A headline must contain at least one keyword. Topics begin without recipients.
6. Make the bot an administrator in the destination group/channel (with posting permission in a channel). You must also be an administrator there. Choose **Добавить получателя → Выбрать канал / Выбрать группу**. Telegram shows its native picker. Alternatively supply a numeric chat_id, never a link or @username. The bot checks the actual chat and both administrators' permissions. For a private recipient, that person must first open the bot and send /start, then give the administrator their numeric chat_id privately. Add that ID through the same recipient wizard. The bot verifies getChat and sendChatAction; blocked/unreachable private chats are refused. This does not grant the recipient management access.
7. Open **Рубрики → Открыть → Получатели** and toggle one or several destinations. An empty selection pauses that topic. Removing a destination removes its assignments. Add it again and explicitly assign topics to resume.

Commands: `/start`, `/help`, `/add`, `/topics`, `/recipient`, `/recipients`, `/schedule`, `/cancel`. Edit name, queries, and keywords from the topic card. Topic deletion asks for confirmation. `/cancel` abandons the draft. A new topic is saved only after all three inputs; edits are saved after the replacement field. Selection buttons require a Telegram client supporting request_chat; numeric ID entry is the fallback. Telegram cannot provide bots a complete list of all channels you own.

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
