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
import assistant_api


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


def parse_schedule(text):
    values = sorted(set(v.strip() for v in re.split(r'[,;\n]', text) if v.strip()))
    if not values or len(values) > 12 or any(not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', v) for v in values):
        raise ValueError('Введите время в формате 08:00, 12:00 (до 12 запусков в сутки).')
    return values


def due_slot(now, last, schedule):
    local = now.astimezone(bot.MSK)
    passed = [local.date().isoformat() + ' ' + t for t in schedule if t <= local.strftime('%H:%M')]
    latest = max(passed) if passed else None
    return latest if latest and (last is None or latest > last) else None


def due(now, last):
    # Backward-compatible helper for the original single daily schedule.
    return due_slot(now, last + ' 08:00' if last else None, ['08:00']) is not None


def field_prompt(field):
    if field == 'title':
        return 'Введите название рубрики (до 100 символов). /cancel — отмена.'
    if field == 'queries':
        return ('Введите поисковые запросы через запятую. Каждый запрос — отдельный поиск в Google News. '
                'Пример: банкротство банков, отзыв лицензии банка. /cancel — отмена.')
    return ('Введите ключевые слова через запятую. Они фильтруют найденные заголовки: достаточно совпадения одного слова или части слова. '
            'Пример: банкрот, лиценз, АСВ. /cancel — отмена.')


class DeliveryRunner:
    """One worker serializes manual and scheduled sends using existing history."""
    def __init__(self, store, api, admin, worker):
        self.store, self.api, self.admin, self.worker = store, api, admin, worker
        self.future = None
        self.job = None
        self.retry_at = 0
        if self.store.get('last_slot') is None and self.store.get('last_daily'):
            self.store.put('last_slot', self.store.get('last_daily') + ' 08:00')

    def notify(self, text):
        try:
            self.api('sendMessage', chat_id=self.admin, text=text)
        except TelegramError:
            print('Could not send delivery status to administrator')

    def tick(self, now):
        if self.future is not None and self.future.done():
            kind, identity = self.job
            try:
                count = self.future.result()
            except (SystemExit, Exception):
                print('Delivery incomplete; confirmed messages remain in history')
                if kind == 'manual':
                    self.notify('Срез выполнен не полностью. Проверьте права получателей и повторите: уже отправленные новости не повторятся.')
                else:
                    self.retry_at = time.monotonic() + 300
            else:
                if kind == 'scheduled':
                    self.store.put('last_slot', identity)
                else:
                    self.notify('Срез завершён. Отправлено новостей: ' + str(count) if count else
                                'Новых неотправленных новостей для назначенных получателей не найдено.')
            if kind == 'manual' and self.store.get('manual_request') == identity:
                self.store.put('manual_request', None)
            self.future = None
        if self.future is not None:
            return
        request = self.store.get('manual_request')
        schedule = self.store.get('schedule')
        slot = due_slot(now, self.store.get('last_slot'), ['08:00'] if schedule is None else schedule)
        if request:
            self.job = ('manual', request)
        elif slot and time.monotonic() >= self.retry_at:
            self.job = ('scheduled', slot)
        else:
            return
        self.future = self.worker.submit(bot.main, config=self.store.config(),
            target_validator=lambda chat: validate_target(self.api, chat, self.admin))


def buttons(rows):
    return {'inline_keyboard': [[{'text': label[:60], 'callback_data': data} for label, data in row] for row in rows]}


class Controller:
    def __init__(self, store, api, admin, ai_worker=None):
        self.store, self.api, self.admin = store, api, admin
        self.ai_worker = ai_worker
        self.ai_future = None
        self.ai_cancelled = False

    def say(self, text, rows=None, markup=None):
        self.api('sendMessage', chat_id=self.admin, text=text,
                 reply_markup=markup or buttons(rows or [[('Меню', 'menu')]]))

    def menu(self):
        self.say('Панель управления новостями', [[('➕ Добавить тему', 'add'), ('📋 Рубрики', 'topics')],
                 [('👥 Получатели', 'targets'), ('➕ Получатель', 'target')], [('🕗 Расписание', 'schedule'), ('📰 Срез новостей сейчас', 'now')],
                 [('🔢 Количество новостей', 'limit'), ('🤖 ИИ-помощник', 'ai')]])

    def start_ai(self, text):
        if not os.environ.get('OPENROUTER_API_KEY') or not os.environ.get('OPENROUTER_MODEL') or self.ai_worker is None:
            return self.say('Для ИИ нужны OPENROUTER_API_KEY и OPENROUTER_MODEL в защищённых настройках сервера. Ключ в Telegram не отправляйте.')
        if self.ai_future is not None:
            return self.say('ИИ ещё отвечает на предыдущий запрос. Подождите или /cancel — отмена результата.')
        if not 1 <= len(text) <= 3000:
            return self.say('Отправьте запрос длиной до 3000 символов.')
        day = datetime.now(bot.MSK).date().isoformat()
        usage = self.store.get('ai_usage') or {}
        count = usage.get('count', 0) if usage.get('day') == day else 0
        if count >= 30:
            return self.say('Достигнут лимит 30 запросов ИИ за день по МСК.')
        clean = assistant_api.redact(text)
        if clean != text:
            return self.say('В сообщении обнаружен секрет. Запрос не отправлен. Удалите токены и повторите.')
        self.ai_context = assistant_api.context(self.store.config(), self.store.get('schedule'), clean)
        self.ai_text = clean
        self.ai_cancelled = False
        self.store.put('ai_draft', None)
        self.store.put('ai_usage', {'day': day, 'count': count + 1})
        self.ai_future = self.ai_worker.submit(assistant_api.ask, clean, self.ai_context, self.store.get('ai_history') or [])
        self.say('ИИ готовит ответ. Запрос, краткая история и настройки рубрик передаются OpenRouter; для вопросов по коду — также исходники бота без файлов секретов. /cancel — отмена результата.')

    def tick_ai(self):
        if self.ai_future is None or not self.ai_future.done():
            return
        future, self.ai_future = self.ai_future, None
        if self.ai_cancelled:
            return
        try:
            result = assistant_api.validate_proposal(future.result(), self.ai_context)
        except (ValueError, Exception):
            return self.say('ИИ не смог ответить. Проверьте ключ, баланс и поддержку JSON Schema выбранной моделью OpenRouter. Настройки не изменены.')
        history = (self.store.get('ai_history') or []) + [
            {'role': 'user', 'content': self.ai_text},
            {'role': 'assistant', 'content': assistant_api.redact(result['reply'])}]
        self.store.put('ai_history', history[-6:])
        for start in range(0, len(result['reply']), 1800):
            self.say(assistant_api.redact(result['reply'][start:start + 1800]))
        if result['action'] != 'advice':
            nonce = uuid.uuid4().hex[:12]
            original = self.ai_context['categories'].get(result['topic_id'])
            self.store.put('ai_draft', {'nonce': nonce, 'created': time.time(), 'result': result, 'original': original})
            preview = ('Предложение: ' + ('создать' if result['action'] == 'create_topic' else 'изменить') +
                ' рубрику «' + result['title'] + '»\nЗапросы: ' + ', '.join(result['queries']) +
                '\nКлючевые слова: ' + ', '.join(result['keywords']))
            for start in range(0, len(preview), 1800):
                self.say(preview[start:start + 1800])
            self.say('Сохранить предложение?',
                [[('Сохранить рубрику', 'ai_apply:' + nonce), ('Отклонить', 'ai_reject')]])


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
        if text == '/cancel':
            self.ai_cancelled = True
            self.store.put('ai_mode', False)
            self.store.put('ai_draft', None)
        commands = {'/start': 'menu', '/help': 'menu', '/topics': 'topics', '/add': 'add',
                    '/recipients': 'targets', '/recipient': 'target', '/schedule': 'schedule', '/now': 'now', '/limit': 'limit', '/ai': 'ai', '/cancel': 'menu'}
        if text.startswith('/'):
            action = commands.get(text.split('@')[0], 'menu')
        config = self.store.config()
        categories, targets = config['categories'], config['targets']
        session = self.store.get('session')
        if action:
            if action in ('menu', 'topics', 'targets', 'add', 'target', 'schedule', 'schedule_edit', 'now', 'limit', 'ai'):
                self.store.put('session', None)
            if action == 'menu':
                self.store.put('ai_mode', False)
                return self.menu()
            if action == 'ai':
                self.store.put('ai_mode', True)
                return self.say('Напишите обычным сообщением, например: «Добавь рубрику Банкротства банков с запросами и ключевыми словами» или «Что улучшить в коде?». '
                    'ИИ предлагает изменения рубрик с подтверждением. Код он только обсуждает, не выполняет и не редактирует. '
                    'Запросы и контекст передаются OpenRouter. /cancel — выйти.', [[('Очистить историю ИИ', 'ai_clear'), ('Меню', 'menu')]])
            if action == 'ai_clear':
                self.ai_cancelled = True
                self.store.put('ai_history', [])
                self.store.put('ai_draft', None)
                return self.say('История ИИ и несохранённое предложение удалены из бота.')
            if action == 'ai_reject':
                self.store.put('ai_draft', None)
                return self.say('Предложение отклонено.')
            if action.startswith('ai_apply:'):
                draft = self.store.get('ai_draft')
                if not draft or draft['nonce'] != action.split(':')[1] or time.time() - draft['created'] > 600:
                    return self.say('Предложение устарело. Запросите новое у ИИ.')
                result = assistant_api.validate_proposal(draft['result'], config)
                if result['action'] == 'advice':
                    raise ValueError()
                if result['action'] == 'create_topic':
                    if len(categories) >= 50 or any(c.get('title', key).casefold() == result['title'].casefold() for key, c in categories.items()):
                        return self.say('Рубрика с таким названием уже есть или достигнут лимит рубрик.')
                    key = uuid.uuid4().hex[:12]
                else:
                    key = result['topic_id']
                    if categories.get(key) != draft['original']:
                        return self.say('Рубрика изменилась после запроса ИИ. Запросите новое предложение.')
                categories[key] = dict(categories.get(key, {}), **{field: result[field] for field in ('title', 'queries', 'keywords')})
                self.store.put('config', config)
                self.store.put('ai_draft', None)
                return self.say('Рубрика сохранена. Получателей выберите в «Рубрики → Получатели».')
            if action == 'limit':
                self.store.put('session', {'step': 'limit'})
                return self.say('Сейчас до ' + str(bot.get_news_limit(config)) +
                    ' новостей за один срез для каждого получателя, суммарно по его рубрикам. '
                    'Введите число от 1 до 50. Это максимум: повторов и неподходящих новостей не добавляем. /cancel — отмена.')
            if action == 'schedule':
                schedule = self.store.get('schedule')
                times = ['08:00'] if schedule is None else schedule
                return self.say('Рассылка по МСК: ' + (', '.join(times) if times else 'выключена') +
                    '. После простоя выполняется последний пропущенный запуск за текущий день.',
                    [[('Изменить время', 'schedule_edit'), ('Выключить расписание', 'schedule_off')]])
            if action == 'schedule_edit':
                self.store.put('session', {'step': 'schedule'})
                return self.say('Введите время по МСК через запятую: например, 08:00, 12:00. /cancel — отмена.')
            if action == 'schedule_off':
                self.store.put('schedule', [])
                return self.say('Автоматическая рассылка выключена. Срез вручную доступен.')
            if action == 'now':
                return self.say('Собрать свежие новости и отправить назначенным получателям? История повторов учитывается.',
                                [[('Сделать и отправить сейчас', 'sendnow'), ('Отмена', 'menu')]])
            if action == 'sendnow':
                if self.store.get('manual_request'):
                    return self.say('Срез уже выполняется или ожидает завершения текущей рассылки.')
                if not any(t['categories'] for t in targets):
                    return self.say('Сначала добавьте получателя и назначьте ему хотя бы одну рубрику.')
                self.store.put('manual_request', uuid.uuid4().hex)
                return self.say('Срез поставлен в очередь. Сообщу о завершении; это может занять несколько минут.')
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
                    return self.say(settings.get('title', key) + '\nЗапросы: ' + ', '.join(settings['queries']) + '\nСлова: ' + ', '.join(settings['keywords']), rows)
                if verb == 'edit':
                    if parts[2] not in ('title', 'queries', 'keywords'):
                        raise ValueError()
                    self.store.put('session', {'step': parts[2], 'id': key, 'draft': dict(settings), 'edit': True})
                    return self.say(field_prompt(parts[2]))
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
            if text and self.store.get('ai_mode'):
                return self.start_ai(text)
            return self.menu()
        step = session['step']
        if step == 'limit':
            if not re.fullmatch(r'[0-9]{1,2}', text) or not 1 <= int(text) <= 50:
                return self.say('Введите целое число от 1 до 50. /cancel — отмена.')
            config['news_limit'] = int(text)
            self.store.put('config', config)
            self.store.put('session', None)
            return self.say('Сохранено: до ' + text + ' новостей каждому получателю за один запуск. Применяется к расписанию и срезу сейчас.')
        if step == 'schedule':
            try:
                times = parse_schedule(text)
            except ValueError as exc:
                return self.say(str(exc))
            self.store.put('schedule', times)
            self.store.put('session', None)
            return self.say('Расписание сохранено: ' + ', '.join(times) + ' МСК.')
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
            value = [v.strip() for v in re.split(r'[,;\n]', text) if v.strip()]
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
        self.say(field_prompt(session['step']))


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
    ai_worker = ThreadPoolExecutor(max_workers=1)
    controller = Controller(store, api, int(raw), ai_worker)
    api('setMyCommands', commands=[{'command': c, 'description': d} for c, d in (
        ('start', 'Панель управления'), ('add', 'Добавить рубрику'), ('topics', 'Рубрики и назначения'),
        ('recipients', 'Получатели'), ('recipient', 'Добавить получателя'), ('schedule', 'Расписание'), ('now', 'Срез новостей сейчас'), ('limit', 'Количество новостей'), ('ai', 'ИИ-помощник'), ('cancel', 'Отмена'))])
    worker = ThreadPoolExecutor(max_workers=1)
    runner = DeliveryRunner(store, api, int(raw), worker)
    while True:
        try:
            runner.tick(datetime.now(timezone.utc))
            controller.tick_ai()
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
