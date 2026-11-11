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


class Telegram:
    def __init__(self, token):
        self.token = token

    def __call__(self, method, **data):
        try:
            response = requests.post(f'https://api.telegram.org/bot{self.token}/{method}', json=data, timeout=40)
            response.raise_for_status()
            payload = response.json()
            if not payload.get('ok'):
                raise ValueError()
            return payload['result']
        except (requests.RequestException, ValueError, KeyError):
            raise ValueError('Telegram: Р·Р°РїСЂРѕСЃ РЅРµ РІС‹РїРѕР»РЅРµРЅ. РџСЂРѕРІРµСЂСЊС‚Рµ РґРѕСЃС‚СѓРї Рё РїСЂР°РІР° Р±РѕС‚Р°.') from None


def validate_target(api, raw, admin):
    if isinstance(raw, bool) or not re.fullmatch(r'-?[1-9][0-9]{0,15}', str(raw)):
        raise ValueError('РќСѓР¶РµРЅ С‡РёСЃР»РѕРІРѕР№ chat_id, Р±РµР· СЃСЃС‹Р»РѕРє Рё @username.')
    chat_id = int(raw)
    if abs(chat_id) >= 2**52:
        raise ValueError('chat_id РІРЅРµ РґРѕРїСѓСЃС‚РёРјРѕРіРѕ РґРёР°РїР°Р·РѕРЅР°.')
    chat = api('getChat', chat_id=chat_id)
    if chat['id'] != chat_id:
        raise ValueError('chat_id РЅРµ СЃРѕРІРїР°РґР°РµС‚.')
    if chat['type'] == 'private':
        if chat_id != admin:
            raise ValueError('Р›РёС‡РЅС‹Р№ РїРѕР»СѓС‡Р°С‚РµР»СЊ вЂ” С‚РѕР»СЊРєРѕ С‡Р°С‚ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°.')
    elif chat['type'] in ('group', 'supergroup', 'channel'):
        me = api('getMe')
        member = api('getChatMember', chat_id=chat_id, user_id=me['id'])
        # Require admin for groups too: avoids default/restricted permission ambiguity.
        if member['status'] not in ('administrator', 'creator'):
            raise ValueError('РЎРЅР°С‡Р°Р»Р° РЅР°Р·РЅР°С‡СЊС‚Рµ Р±РѕС‚Р° Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂРѕРј РіСЂСѓРїРїС‹ РёР»Рё РєР°РЅР°Р»Р°.')
        if chat['type'] == 'channel' and member['status'] != 'creator' and not member.get('can_post_messages'):
            raise ValueError('Р‘РѕС‚Сѓ РЅСѓР¶РЅРѕ РїСЂР°РІРѕ РїСѓР±Р»РёРєР°С†РёРё РІ РєР°РЅР°Р»Рµ.')
        owner = api('getChatMember', chat_id=chat_id, user_id=admin)
        if owner['status'] not in ('administrator', 'creator'):
            raise ValueError('Р’С‹ РґРѕР»Р¶РЅС‹ Р±С‹С‚СЊ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂРѕРј РїРѕР»СѓС‡Р°С‚РµР»СЏ.')
    else:
        raise ValueError('Р­С‚РѕС‚ С‚РёРї С‡Р°С‚Р° РЅРµ РїРѕРґРґРµСЂР¶РёРІР°РµС‚СЃСЏ.')
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
                 reply_markup=markup or buttons(rows or [[('РњРµРЅСЋ', 'menu')]]))

    def menu(self):
        self.say('РџР°РЅРµР»СЊ СѓРїСЂР°РІР»РµРЅРёСЏ РЅРѕРІРѕСЃС‚СЏРјРё', [[('вћ• Р”РѕР±Р°РІРёС‚СЊ С‚РµРјСѓ', 'add'), ('рџ“‹ Р СѓР±СЂРёРєРё', 'topics')],
                 [('рџ‘Ґ РџРѕР»СѓС‡Р°С‚РµР»Рё', 'targets'), ('вћ• РџРѕР»СѓС‡Р°С‚РµР»СЊ', 'target')], [('рџ•— Р Р°СЃРїРёСЃР°РЅРёРµ', 'schedule')]])

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
            self.say('Р”РµР№СЃС‚РІРёРµ РЅРµ РІС‹РїРѕР»РЅРµРЅРѕ. РџСЂРѕРІРµСЂСЊС‚Рµ РґР°РЅРЅС‹Рµ, РЅР°Р»РёС‡РёРµ СЂСѓР±СЂРёРєРё Рё РїСЂР°РІР° РїСѓР±Р»РёРєР°С†РёРё. /cancel вЂ” РѕС‚РјРµРЅР°.')

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
                return self.say('Р•Р¶РµРґРЅРµРІРЅРѕ РІ 08:00 РњРЎРљ. РџРѕСЃР»Рµ РїСЂРѕСЃС‚РѕСЏ вЂ” РѕРґРЅР° СЂР°СЃСЃС‹Р»РєР° РїСЂРё Р·Р°РїСѓСЃРєРµ РїРѕСЃР»Рµ 08:00. РџРѕСЃР»РµРґРЅРёР№ СѓСЃРїРµС€РЅС‹Р№ РґРµРЅСЊ: ' + str(self.store.get('last_daily') or 'РµС‰С‘ РЅРµС‚'))
            if action == 'add':
                self.store.put('session', {'step': 'title', 'id': uuid.uuid4().hex[:12], 'draft': {}})
                return self.say('Р’РІРµРґРёС‚Рµ РЅР°Р·РІР°РЅРёРµ РЅРѕРІРѕР№ СЂСѓР±СЂРёРєРё (РґРѕ 100 СЃРёРјРІРѕР»РѕРІ). /cancel вЂ” РѕС‚РјРµРЅР°.')
            if action == 'topics':
                self.say('Р СѓР±СЂРёРєРё: ' + str(len(categories)))
                for key, settings in categories.items():
                    self.say(settings.get('title', key), [[('РћС‚РєСЂС‹С‚СЊ', 'topic:' + key)]])
                return
            if action == 'targets':
                self.say('РџРѕР»СѓС‡Р°С‚РµР»Рё: ' + str(len(targets)))
                for target in targets:
                    self.say(target.get('title', target['chat_id']) + ' (' + target['chat_id'] + ')',
                             [[('РЈРґР°Р»РёС‚СЊ', 'deltarget:' + target['chat_id'])]])
                return
            if action == 'target':
                self.store.put('session', {'step': 'target'})
                rows = [[{'text': 'РњРѕР№ Р»РёС‡РЅС‹Р№ С‡Р°С‚'}], [{'text': 'Р’С‹Р±СЂР°С‚СЊ РєР°РЅР°Р»', 'request_chat': {
                    'request_id': 1, 'chat_is_channel': True, 'request_title': True, 'bot_is_member': True,
                    'user_administrator_rights': admin_rights(True), 'bot_administrator_rights': admin_rights(True)}}],
                    [{'text': 'Р’С‹Р±СЂР°С‚СЊ РіСЂСѓРїРїСѓ', 'request_chat': {'request_id': 2, 'chat_is_channel': False,
                    'request_title': True, 'bot_is_member': True, 'user_administrator_rights': admin_rights(False),
                    'bot_administrator_rights': admin_rights(False)}}]]
                return self.say('Р”РѕР±Р°РІСЊС‚Рµ Р±РѕС‚Р° Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂРѕРј, Р·Р°С‚РµРј РІС‹Р±РµСЂРёС‚Рµ С‡Р°С‚ РєРЅРѕРїРєРѕР№ РёР»Рё РѕС‚РїСЂР°РІСЊС‚Рµ С‡РёСЃР»РѕРІРѕР№ chat_id. /cancel вЂ” РѕС‚РјРµРЅР°.',
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
                    rows = [[('РќР°Р·РІР°РЅРёРµ', 'edit:' + key + ':title'), ('Р—Р°РїСЂРѕСЃС‹', 'edit:' + key + ':queries')],
                            [('РљР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР°', 'edit:' + key + ':keywords'), ('РџРѕР»СѓС‡Р°С‚РµР»Рё', 'assign:' + key)],
                            [('РЈРґР°Р»РёС‚СЊ СЂСѓР±СЂРёРєСѓ', 'delete:' + key)]]
                    return self.say(settings.get('title', key) + '\nР—Р°РїСЂРѕСЃС‹: ' + '; '.join(settings['queries']) + '\nРЎР»РѕРІР°: ' + '; '.join(settings['keywords']), rows)
                if verb == 'edit':
                    if parts[2] not in ('title', 'queries', 'keywords'):
                        raise ValueError()
                    self.store.put('session', {'step': parts[2], 'id': key, 'draft': dict(settings), 'edit': True})
                    return self.say('Р’РІРµРґРёС‚Рµ РЅРѕРІРѕРµ Р·РЅР°С‡РµРЅРёРµ. РЎРїРёСЃРєРё СЂР°Р·РґРµР»СЏР№С‚Рµ С‚РѕС‡РєРѕР№ СЃ Р·Р°РїСЏС‚РѕР№. /cancel вЂ” РѕС‚РјРµРЅР°.')
                if verb == 'delete':
                    return self.say('РЈРґР°Р»РёС‚СЊ СЂСѓР±СЂРёРєСѓ ' + settings.get('title', key) + '?', [[('Р”Р°, СѓРґР°Р»РёС‚СЊ', 'confirm:' + key)]])
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
                    self.say('РџРѕР»СѓС‡Р°С‚РµР»Рё СЂСѓР±СЂРёРєРё ' + settings.get('title', key))
                    for t in targets:
                        self.say(('вњ… ' if key in t['categories'] else 'в¬њ ') + t.get('title', t['chat_id']),
                                 [[('РџРµСЂРµРєР»СЋС‡РёС‚СЊ', 'toggle:' + key + ':' + t['chat_id'])]])
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
            raw = shared.get('chat_id', self.admin if text == 'РњРѕР№ Р»РёС‡РЅС‹Р№ С‡Р°С‚' else text)
            target = validate_target(self.api, raw, self.admin)
            if any(t['chat_id'] == target['chat_id'] for t in targets):
                raise ValueError()
            targets.append(target)
            self.store.put('config', config)
            self.store.put('session', None)
            self.say('РџРѕР»СѓС‡Р°С‚РµР»СЊ РґРѕР±Р°РІР»РµРЅ. РќР°Р·РЅР°С‡СЊС‚Рµ РµРјСѓ СЂСѓР±СЂРёРєРё С‡РµСЂРµР· В«Р СѓР±СЂРёРєРё в†’ РџРѕР»СѓС‡Р°С‚РµР»РёВ».', markup={'remove_keyboard': True})
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
            self.say('Р СѓР±СЂРёРєР° СЃРѕС…СЂР°РЅРµРЅР°. Р’С‹Р±РµСЂРёС‚Рµ РґР»СЏ РЅРµС‘ РїРѕР»СѓС‡Р°С‚РµР»РµР№.')
            return self.menu()
        session['step'] = 'queries' if step == 'title' else 'keywords'
        self.store.put('session', session)
        self.say('Р’РІРµРґРёС‚Рµ ' + ('РїРѕРёСЃРєРѕРІС‹Рµ Р·Р°РїСЂРѕСЃС‹' if step == 'title' else 'РєР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР°') + ' С‡РµСЂРµР· С‚РѕС‡РєСѓ СЃ Р·Р°РїСЏС‚РѕР№.')


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
        ('start', 'РџР°РЅРµР»СЊ СѓРїСЂР°РІР»РµРЅРёСЏ'), ('add', 'Р”РѕР±Р°РІРёС‚СЊ СЂСѓР±СЂРёРєСѓ'), ('topics', 'Р СѓР±СЂРёРєРё Рё РЅР°Р·РЅР°С‡РµРЅРёСЏ'),
        ('recipients', 'РџРѕР»СѓС‡Р°С‚РµР»Рё'), ('recipient', 'Р”РѕР±Р°РІРёС‚СЊ РїРѕР»СѓС‡Р°С‚РµР»СЏ'), ('schedule', 'Р Р°СЃРїРёСЃР°РЅРёРµ'), ('cancel', 'РћС‚РјРµРЅР°'))])
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
        except ValueError:
            print('Telegram unavailable; retrying')
            time.sleep(5)


if __name__ == '__main__':
    main()
