"""Single-owner Telegram control plane and durable local configuration."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import requests
import bot


class Store:
    def __init__(self, path, initial):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.execute('CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        if self.get('config') is None:
            self.put('config', initial)

    def get(self, key):
        row = self.db.execute('SELECT value FROM control WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO control VALUES (?, ?)', (key, json.dumps(value)))

    def config(self):
        return copy.deepcopy(self.get('config'))


class TelegramError(ValueError):
    """Safe diagnostic: never includes request URLs, server text or credentials."""


class Telegram:
    def __init__(self, token):
        self.token = token

    def __call__(self, method, **data):
        try:
            response = requests.post(f'https://api.telegram.org/bot{self.token}/{method}', json=data, timeout=40)
        except requests.exceptions.SSLError:
            raise TelegramError('Telegram TLS: ошибка сертификата. Проверьте дату ПК и настройки защищённого соединения.') from None
        except requests.exceptions.ProxyError:
            raise TelegramError('Telegram proxy: не удалось подключиться через прокси. Проверьте его адрес и доступность.') from None
        except requests.Timeout:
            raise TelegramError('Telegram timeout: сервер не ответил вовремя. Проверьте интернет и доступ к api.telegram.org.') from None
        except requests.ConnectionError:
            raise TelegramError('Telegram connection: соединение не установлено. Возможны блокировка, ошибка DNS или отсутствие интернета. Проверьте доступ к api.telegram.org; при блокировке нужен VPN для всего ПК или HTTPS-прокси.') from None
        except requests.RequestException:
            raise TelegramError('Telegram request: ошибка запроса. Проверьте настройки сети.') from None
        status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(status, int):
            raise TelegramError('Telegram response: неизвестный ответ сервера.')
        if status >= 400 or (isinstance(payload, dict) and payload.get('ok') is False):
            api_code = payload.get('error_code') if isinstance(payload, dict) else None
            code = api_code if type(api_code) is int and 400 <= api_code <= 599 else status
            reasons = {
                400: 'Некорректные параметры запроса.',
                401: 'Токен бота не принят. Заново введите токен от BotFather.',
                403: 'Доступ запрещён: проверьте блокировку бота и его права в чате.',
                404: 'Адрес API не найден. Проверьте токен: без кавычек, пробелов и префикса bot.',
                409: 'Конфликт: остановите другой процесс бота и проверьте webhook.',
                429: 'Слишком много запросов. Подождите перед повторным запуском.',
            }
            raise TelegramError('Telegram ' + str(code) + ': ' + reasons.get(code, 'Сервис недоступен или запрос отклонён. Повторите позже.'))
        if not isinstance(payload, dict) or payload.get('ok') is not True or 'result' not in payload:
            raise TelegramError('Telegram response: неожиданный формат ответа. Проверьте доступ к API и сетевые фильтры.')
        return payload['result']


def validate_target(api, raw, admin):
    if isinstance(raw, bool) or not re.fullmatch(r'-?[1-9][0-9]{0,15}', str(raw)):
        raise ValueError('Нужен числовой chat_id, без ссылок и @username.')
    chat_id = int(raw)
    if abs(chat_id) >= 2**52:
        raise ValueError('chat_id вне допустимого диапазона.')
    chat = api('getChat', chat_id=chat_id)
    if chat['id'] != chat_id:
        raise ValueError('chat_id не совпадает.')
    if chat['type'] == 'private':
        if chat_id <= 0:
            raise ValueError('Invalid private chat_id')
        # Private recipients must open/start the bot first; a typing action
        # verifies reachability without publishing a probe message.
        api('sendChatAction', chat_id=chat_id, action='typing')
    elif chat['type'] in ('group', 'supergroup', 'channel'):
        me = api('getMe')
        member = api('getChatMember', chat_id=chat_id, user_id=me['id'])
        # Require admin for groups too: avoids default/restricted permission ambiguity.
        if member['status'] not in ('administrator', 'creator'):
            raise ValueError('Сначала назначьте бота администратором группы или канала.')
        if chat['type'] == 'channel' and member['status'] != 'creator' and not member.get('can_post_messages'):
            raise ValueError('Боту нужно право публикации в канале.')
        owner = api('getChatMember', chat_id=chat_id, user_id=admin)
        if owner['status'] not in ('administrator', 'creator'):
            raise ValueError('Вы должны быть администратором получателя.')
    else:
        raise ValueError('Этот тип чата не поддерживается.')
    return {'chat_id': str(chat_id), 'title': chat.get('title', chat.get('first_name', str(chat_id))), 'categories': []}


def due(now, last):
    local = now.astimezone(bot.MSK)
    return local.hour >= 8 and local.date().isoformat() != last


def buttons(rows):
    return {'inline_keyboard': [[{'text': label[:60], 'callback_data': data} for label, data in row] for row in rows]}


class Controller:
    def __init__(self, store, api, admin):
        self.store, self.api, self.admin = store, api, admin

    def say(self, text, rows=None, markup=None):
        self.api('sendMessage', chat_id=self.admin, text=text,
                 reply_markup=markup or buttons(rows or [[('Меню', 'menu')]]))

    def menu(self):
        self.say('Панель управления новостями', [[('➕ Добавить тему', 'add'), ('📋 Рубрики', 'topics')],
                 [('👥 Получатели', 'targets'), ('➕ Получатель', 'target')], [('🕗 Расписание', 'schedule')]])

    def handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message', {}) if callback else update.get('message', {})
        actor = callback.get('from', {}) if callback else message.get('from', {})
        if actor.get('id') != self.admin or message.get('chat', {}).get('id') != self.admin or message.get('chat', {}).get('type') != 'private':
            return
        if callback:
            self.api('answerCallbackQuery', callback_query_id=callback['id'])
        try:
            self.process(message, callback.get('data') if callback else None)
        except (ValueError, KeyError, StopIteration):
            self.say('Действие не выполнено. Проверьте данные, наличие рубрики и права публикации. /cancel — отмена.')

    def process(self, message, action):
        text = message.get('text', '').strip()
        commands = {'/start': 'menu', '/help': 'menu', '/topics': 'topics', '/add': 'add',
                    '/recipients': 'targets', '/recipient': 'target', '/schedule': 'schedule', '/cancel': 'menu'}
        if text.startswith('/'):
            action = commands.get(text.split('@')[0], 'menu')
        config = self.store.config()
        categories, targets = config['categories'], config['targets']
        session = self.store.get('session')
        if action:
            if action in ('menu', 'topics', 'targets', 'add', 'target'):
                self.store.put('session', None)
            if action == 'menu':
                return self.menu()
            if action == 'schedule':
                return self.say('Ежедневно в 08:00 МСК. После простоя — одна рассылка при запуске после 08:00. Последний успешный день: ' + str(self.store.get('last_daily') or 'ещё нет'))
            if action == 'add':
                self.store.put('session', {'step': 'title', 'id': uuid.uuid4().hex[:12], 'draft': {}})
                return self.say('Введите название новой рубрики (до 100 символов). /cancel — отмена.')
            if action == 'topics':
                self.say('Рубрики: ' + str(len(categories)))
                for key, settings in categories.items():
                    self.say(settings.get('title', key), [[('Открыть', 'topic:' + key)]])
                return
            if action == 'targets':
                self.say('Получатели: ' + str(len(targets)))
                for target in targets:
                    self.say(target.get('title', target['chat_id']) + ' (' + target['chat_id'] + ')',
                             [[('Удалить', 'deltarget:' + target['chat_id'])]])
                return
            if action == 'target':
                self.store.put('session', {'step': 'target'})
                rows = [[{'text': 'Мой личный чат'}], [{'text': 'Выбрать канал', 'request_chat': {
                    'request_id': 1, 'chat_is_channel': True, 'request_title': True, 'bot_is_member': True,
                    'user_administrator_rights': admin_rights(True), 'bot_administrator_rights': admin_rights(True)}}],
                    [{'text': 'Выбрать группу', 'request_chat': {'request_id': 2, 'chat_is_channel': False,
                    'request_title': True, 'bot_is_member': True, 'user_administrator_rights': admin_rights(False),
                    'bot_administrator_rights': admin_rights(False)}}]]
                return self.say('Добавьте бота администратором, затем выберите чат кнопкой или отправьте числовой chat_id. /cancel — отмена.',
                                markup={'keyboard': rows, 'resize_keyboard': True, 'one_time_keyboard': True})
            parts = action.split(':')
            verb = parts[0]
            if verb == 'deltarget':
                found = next(t for t in targets if t['chat_id'] == parts[1])
                targets.remove(found)
            else:
                key = parts[1]
                settings = categories[key]
                if verb == 'topic':
                    rows = [[('Название', 'edit:' + key + ':title'), ('Запросы', 'edit:' + key + ':queries')],
                            [('Ключевые слова', 'edit:' + key + ':keywords'), ('Получатели', 'assign:' + key)],
                            [('Удалить рубрику', 'delete:' + key)]]
                    return self.say(settings.get('title', key) + '\nЗапросы: ' + '; '.join(settings['queries']) + '\nСлова: ' + '; '.join(settings['keywords']), rows)
                if verb == 'edit':
                    if parts[2] not in ('title', 'queries', 'keywords'):
                        raise ValueError()
                    self.store.put('session', {'step': parts[2], 'id': key, 'draft': dict(settings), 'edit': True})
                    return self.say('Введите новое значение. Списки разделяйте точкой с запятой. /cancel — отмена.')
                if verb == 'delete':
                    return self.say('Удалить рубрику ' + settings.get('title', key) + '?', [[('Да, удалить', 'confirm:' + key)]])
                if verb == 'confirm':
                    del categories[key]
                    for target in targets:
                        target['categories'] = [c for c in target['categories'] if c != key]
                elif verb in ('assign', 'toggle'):
                    if verb == 'toggle':
                        target = next(t for t in targets if t['chat_id'] == parts[2])
                        if key in target['categories']:
                            target['categories'].remove(key)
                        else:
                            validate_target(self.api, target['chat_id'], self.admin)
                            target['categories'].append(key)
                        self.store.put('config', config)
                    self.say('Получатели рубрики ' + settings.get('title', key))
                    for t in targets:
                        self.say(('✅ ' if key in t['categories'] else '⬜ ') + t.get('title', t['chat_id']),
                                 [[('Переключить', 'toggle:' + key + ':' + t['chat_id'])]])
                    return
                else:
                    raise ValueError()
            self.store.put('config', config)
            return self.menu()
        if not session:
            return self.menu()
        step = session['step']
        if step == 'target':
            shared = message.get('chat_shared', {})
            if shared and shared.get('request_id') not in (1, 2):
                raise ValueError()
            raw = shared.get('chat_id', self.admin if text == 'Мой личный чат' else text)
            target = validate_target(self.api, raw, self.admin)
            if any(t['chat_id'] == target['chat_id'] for t in targets):
                raise ValueError()
            targets.append(target)
            self.store.put('config', config)
            self.store.put('session', None)
            self.say('Получатель добавлен. Назначьте ему рубрики через «Рубрики → Получатели».', markup={'remove_keyboard': True})
            return self.menu()
        if step == 'title':
            if not text or len(text) > 100:
                raise ValueError()
            value = text
        else:
            value = [v.strip() for v in text.split(';') if v.strip()]
            if not value or len(value) > 20 or any(len(v) > 200 for v in value):
                raise ValueError()
        session['draft'][step] = value
        if session.get('edit') or step == 'keywords':
            if session.get('edit') and session['id'] not in categories:
                raise ValueError()
            if not session.get('edit') and len(categories) >= 50:
                raise ValueError()
            categories[session['id']] = session['draft']
            self.store.put('config', config)
            self.store.put('session', None)
            self.say('Рубрика сохранена. Выберите для неё получателей.')
            return self.menu()
        session['step'] = 'queries' if step == 'title' else 'keywords'
        self.store.put('session', session)
        self.say('Введите ' + ('поисковые запросы' if step == 'title' else 'ключевые слова') + ' через точку с запятой.')


def admin_rights(channel):
    rights = {key: False for key in ('is_anonymous', 'can_manage_chat', 'can_delete_messages',
              'can_manage_video_chats', 'can_restrict_members', 'can_promote_members', 'can_change_info',
              'can_invite_users', 'can_post_stories', 'can_edit_stories', 'can_delete_stories')}
    rights['can_manage_chat'] = True
    if channel:
        rights['can_post_messages'] = True
    return rights


def main():
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    raw = os.environ.get('TELEGRAM_ADMIN_ID', '')
    if not token or not re.fullmatch(r'[1-9][0-9]{0,15}', raw) or int(raw) >= 2**52:
        raise SystemExit('Set TELEGRAM_BOT_TOKEN and numeric TELEGRAM_ADMIN_ID in secrets')
    path = Path(os.environ.get('NEWS_STATE_PATH', 'data/sent.sqlite3'))
    # A local OS lock prevents two pollers/schedulers using the same volume.
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(path) + '.lock', 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            lock.write(b'0')
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit('Another interactive instance holds the state lock') from None
    # Missing TELEGRAM_CHAT_ID is valid in interactive mode: start without targets.
    if not any(os.environ.get(k) for k in ('TELEGRAM_CHAT_ID', 'TELEGRAM_TARGETS', 'NEWS_CONFIG_PATH')):
        initial = {'categories': copy.deepcopy(bot.DEFAULT_CATEGORIES), 'targets': []}
    else:
        initial = bot.load_config()
    store = Store(path, initial)
    api = Telegram(token)
    if api('getWebhookInfo').get('url'):
        raise SystemExit('Remove the existing Telegram webhook before enabling polling')
    controller = Controller(store, api, int(raw))
    api('setMyCommands', commands=[{'command': c, 'description': d} for c, d in (
        ('start', 'Панель управления'), ('add', 'Добавить рубрику'), ('topics', 'Рубрики и назначения'),
        ('recipients', 'Получатели'), ('recipient', 'Добавить получателя'), ('schedule', 'Расписание'), ('cancel', 'Отмена'))])
    retry_at = 0
    worker = ThreadPoolExecutor(max_workers=1)
    delivery = None
    delivery_day = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            if delivery is not None and delivery.done():
                try:
                    delivery.result()
                    store.put('last_daily', delivery_day)
                except (SystemExit, ValueError, requests.RequestException):
                    print('Daily delivery incomplete; retry in five minutes')
                    retry_at = time.monotonic() + 300
                delivery = None
            if delivery is None and due(now, store.get('last_daily')) and time.monotonic() >= retry_at:
                delivery_day = now.astimezone(bot.MSK).date().isoformat()
                delivery = worker.submit(bot.main, config=store.config(),
                    target_validator=lambda chat: validate_target(api, chat, int(raw)))
            updates = api('getUpdates', offset=store.get('offset') or 0, timeout=20,
                          allowed_updates=['message', 'callback_query'])
            for update in updates:
                controller.handle(update)
                store.put('offset', update['update_id'] + 1)
        except TelegramError as exc:
            print(str(exc))
            time.sleep(5)


if __name__ == '__main__':
    try:
        main()
    except TelegramError as exc:
        raise SystemExit(str(exc)) from None
