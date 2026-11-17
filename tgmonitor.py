"""Public-channel reader. User session reads; Bot API delivers. No account posting."""
import argparse
import asyncio
from contextlib import closing
from datetime import datetime,timezone,timedelta
import getpass
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from urllib.parse import urlsplit

MSK=timezone(timedelta(hours=3))

def channel_name(value):
    value=value.strip()
    if value.startswith('https://'):
        p=urlsplit(value)
        if p.netloc!='t.me' or p.query or p.fragment:
            raise ValueError('Нужна ссылка https://t.me/имя_канала или @имя_канала.')
        value=p.path.strip('/')
        if value.startswith('s/'):value=value[2:]
    value=value.removeprefix('@')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',value):
        raise ValueError('Нужен публичный канал: @имя или https://t.me/имя. Закрытые ссылки не поддерживаются.')
    return value.lower()


def post_parts(entity,message):
    username=channel_name(entity.username)
    if not isinstance(message.id,int) or message.id<=0:
        raise ValueError('Неверный номер поста.')
    url='https://t.me/'+username+'/'+str(message.id)
    stamp=message.date.astimezone(MSK).strftime('%d.%m.%Y %H:%M МСК')
    header='📣 <b>'+html.escape(entity.title[:200])+'</b>\n'+stamp+'\n'
    footer='\n<a href="'+url+'">Открыть пост</a>'
    text=message.raw_text or 'Публикация с медиа — откройте оригинал.'
    parts=[];start=0
    while start<len(text):
        low,high=1,min(3000,len(text)-start)
        while low<high:
            size=(low+high+1)//2
            candidate=header+html.escape(text[start:start+size])+footer
            if len(candidate.encode('utf-16-le'))//2<=3900:low=size
            else:high=size-1
        parts.append(header+html.escape(text[start:start+low])+footer)
        start+=low
    return parts


class ReaderState:
    def __init__(self,path):
        self.db=sqlite3.connect(path,timeout=10)
        self.db.execute('CREATE TABLE IF NOT EXISTS tg_cursors (username TEXT PRIMARY KEY, entity INTEGER, last_id INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS tg_sent (entity INTEGER, message INTEGER, chat TEXT, part INTEGER, PRIMARY KEY(entity,message,chat,part))')
        self.db.execute('CREATE TABLE IF NOT EXISTS tg_source_status (username TEXT PRIMARY KEY, title TEXT, status TEXT, at REAL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS tg_requests (actor INTEGER PRIMARY KEY, username TEXT, at REAL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS tg_limits (id INTEGER PRIMARY KEY, until REAL)')
        self.db.commit()
    def close(self):self.db.close()
    def pause(self,until):
        with self.db:self.db.execute('INSERT OR REPLACE INTO tg_limits VALUES(1,?)',(until,))
    def blocked(self):
        row=self.db.execute('SELECT until FROM tg_limits WHERE id=1').fetchone()
        return bool(row and row[0]>time.time())
    def cursor(self,username):
        row=self.db.execute('SELECT last_id FROM tg_cursors WHERE username=?',(username,)).fetchone()
        return row[0] if row else None
    def entity_id(self,username):
        row=self.db.execute('SELECT entity FROM tg_cursors WHERE username=?',(username,)).fetchone()
        return row[0] if row else None
    def advance(self,username,entity,message):
        with self.db:self.db.execute('INSERT OR REPLACE INTO tg_cursors VALUES(?,?,?)',(username,entity,message))
    def status(self,username,title,text):
        with self.db:self.db.execute('INSERT OR REPLACE INTO tg_source_status VALUES(?,?,?,?)',(username,title,text,time.time()))
    def known(self,entity,message,chat):
        return {row[0] for row in self.db.execute('SELECT part FROM tg_sent WHERE entity=? AND message=? AND chat=?',(entity,message,str(chat)))}
    def sent(self,entity,message,chat,part):
        with self.db:self.db.execute('INSERT OR IGNORE INTO tg_sent VALUES(?,?,?,?)',(entity,message,str(chat),part))


async def poll_channel(state,client,watch,api,validate,preview=None):
    name=channel_name(watch['username'])
    entity=await client.get_entity(name)
    if not getattr(entity,'broadcast',False) or getattr(entity,'megagroup',False) or channel_name(entity.username)!=name:
        raise ValueError('Источник не является доступным публичным каналом.')
    cursor_key=name+(':'+watch['generation'] if watch.get('generation') else '')
    old_entity=state.entity_id(cursor_key)
    if old_entity is not None and old_entity!=entity.id:
        raise ValueError('Имя канала сменило владельца. Удалите источник и проверьте его перед повторным добавлением.')
    cursor=state.cursor(cursor_key)
    if preview is not None or cursor is None:
        messages=await client.get_messages(entity,limit=1)
        if preview is None:
            state.advance(cursor_key,entity.id,max((m.id for m in messages),default=0))
            state.status(name,entity.title,'Подключён. Отправляются только новые посты после первой проверки.')
            return
        if not messages:
            api('sendMessage',chat_id=preview,text='В канале @'+name+' нет доступных публикаций.')
            return
    else:
        messages=await client.get_messages(entity,limit=50,min_id=cursor,reverse=True)
    for message in sorted(messages,key=lambda m:m.id):
        if preview is None and message.id<=cursor:continue
        text=message.raw_text or ''
        matches=not watch['keywords'] or any(word.casefold() in text.casefold() for word in watch['keywords'])
        if (preview is not None or matches) and not getattr(message,'action',None):
            for chat in ([preview] if preview is not None else watch['targets']):
                known=set() if preview is not None else state.known(entity.id,message.id,chat)
                if -1 in known:continue
                validate(chat)
                for index,part in enumerate(post_parts(entity,message)):
                    if index in known:continue
                    api('sendMessage',chat_id=chat,text=part,parse_mode='HTML',disable_web_page_preview=True)
                    if preview is None:state.sent(entity.id,message.id,chat,index)
                if preview is None:state.sent(entity.id,message.id,chat,-1)
        if preview is None:state.advance(cursor_key,entity.id,message.id)
    state.status(name,entity.title,'Проверка выполнена; повторы не отправляются.')


def ui(controller,message,action,text):
    store=controller.store
    watches=store.get('tg_watch') or {}
    session=store.get('session') or {}
    if action=='tg_menu':
        store.put('session',None)
        return controller.say('Мониторинг публичных Telegram-каналов. Проверка каждые 5 минут; только новые посты после первого подключения. До 20 каналов. Последний пост — только по кнопке.',
            [[('Добавить канал','tg_add'),('Каналы','tg_list')],[('Меню','menu')]])
    if action=='tg_add':
        store.put('session',{'step':'tg_channel'})
        return controller.say('Введите @имя_канала или https://t.me/имя_канала. /cancel — отмена.')
    if action=='tg_list':
        controller.say('Каналов: '+str(len(watches)))
        with closing(ReaderState(store.path)) as state:
            for name,watch in watches.items():
                row=state.db.execute('SELECT title,status FROM tg_source_status WHERE username=?',(name,)).fetchone()
                controller.say((row[0] if row else '@'+name)+'\n'+(row[1] if row else 'Ожидает подключения сборщика.')+
                    '\nСлова: '+(', '.join(watch['keywords']) or 'все посты')+'\nПолучателей: '+str(len(watch['targets'])),
                    [[('Последний пост','tg_latest:'+name)],[('Ключевые слова','tg_words:'+name),('Получатели','tg_targets:'+name)],[('Удалить','tg_delete:'+name)]])
        return
    if action and ':' in action:
        verb,raw=action.split(':',1)
        name=channel_name(raw.split(':',1)[0])
        if name not in watches:raise ValueError('Канал не найден.')
        watch=watches[name]
        if verb=='tg_delete':
            return controller.say('Удалить @'+name+' из мониторинга?',[[('Да, удалить','tg_remove:'+name),('Отмена','tg_menu')]])
        if verb=='tg_remove':
            watches.pop(name);store.put('tg_watch',watches)
            with closing(ReaderState(store.path)) as state:
                with state.db:
                    state.db.execute('DELETE FROM tg_cursors WHERE username=?',(name+(':'+watch['generation'] if watch.get('generation') else ''),))
                    state.db.execute('DELETE FROM tg_requests WHERE username=?',(name,))
            return controller.say('Канал удалён.')
        if verb=='tg_latest':
            with closing(ReaderState(store.path)) as state:
                with state.db:
                    state.db.execute('DELETE FROM tg_requests WHERE at<?',(time.time()-600,))
                    if state.db.execute('SELECT 1 FROM tg_requests WHERE actor=?',(controller.admin,)).fetchone():
                        return controller.say('Предыдущий запрос ещё ожидается.')
                    state.db.execute('INSERT INTO tg_requests VALUES(?,?,?)',(controller.admin,name,time.time()))
            return controller.say('Последний пост запрошен только для вас. Сборщик должен быть запущен на сервере; история рассылки не изменится.')
        if verb=='tg_words':
            store.put('session',{'step':'tg_keywords','name':name})
            return controller.say('Введите ключевые слова через запятую. Совпадение любого слова в тексте/подписи; «все» — без фильтра. /cancel — отмена.')
        if verb in ('tg_targets','tg_toggle'):
            choices={str(controller.admin):'Мой личный чат'}
            choices.update({t['chat_id']:t.get('title',t['chat_id']) for t in store.config()['targets']})
            if verb=='tg_toggle':
                chat=raw.split(':',1)[1]
                if chat not in choices:raise ValueError('Неизвестный получатель.')
                if chat in watch['targets']:watch['targets'].remove(chat)
                else:
                    from control import validate_target
                    validate_target(controller.api,chat,controller.admin)
                    watch['targets'].append(chat)
                store.put('tg_watch',watches)
            controller.say('Получатели @'+name+'. Новых добавляйте через общее меню «Получатели».')
            for chat,title in choices.items():
                controller.say(('✅ ' if chat in watch['targets'] else '⬜ ')+title,[[('Переключить','tg_toggle:'+name+':'+chat)]])
            return
    if session.get('step')=='tg_channel':
        name=channel_name(text)
        if len(watches)>=20 or name in watches:raise ValueError('Канал уже добавлен или достигнут лимит 20.')
        watches[name]={'username':name,'generation':uuid.uuid4().hex,'keywords':[],'targets':[str(controller.admin)]}
        store.put('tg_watch',watches);store.put('session',None)
        return controller.say('Канал @'+name+' добавлен. Сборщик проверит его доступность. По умолчанию новые посты отправляются в ваш личный чат.',[[('Настроить слова','tg_words:'+name),('Получатели','tg_targets:'+name)]])
    if session.get('step')=='tg_keywords':
        name=session['name']
        words=[] if text.casefold()=='все' else list(dict.fromkeys(w.strip() for w in text.split(',') if w.strip()))
        if name not in watches or len(words)>20 or any(len(w)>100 for w in words) or not text:raise ValueError('Неверные слова.')
        watches[name]['keywords']=words;store.put('tg_watch',watches);store.put('session',None)
        return controller.say('Ключевые слова сохранены.')


async def run(login=False):
    try:
        from telethon import TelegramClient,errors
    except ImportError:
        raise SystemExit('Установите requirements-telegram.txt.') from None
    from control import Store,Telegram
    from access import authorized,delivery_target
    api_id=os.environ.get('TG_API_ID','')
    api_hash=os.environ.get('TG_API_HASH','')
    if not api_id.isdigit() or not re.fullmatch(r'[0-9a-fA-F]{32}',api_hash):
        raise SystemExit('Задайте TG_API_ID и TG_API_HASH на сервере.')
    session=os.environ.get('TG_SESSION_PATH','/var/lib/newsbot/telegram-reader.session')
    if not Path(session).is_absolute():raise SystemExit('TG_SESSION_PATH должен быть абсолютным.')
    os.umask(0o077)
    # One reader/login process per session; never open Telethon's session twice.
    import fcntl
    lock_fd=os.open(session+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:fcntl.flock(lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        raise SystemExit('Сессия уже используется. Остановите newsbot-telegram перед повторным входом.') from None
    client=TelegramClient(session,int(api_id),api_hash,receive_updates=False,flood_sleep_threshold=0,request_retries=1)
    await client.connect()
    try:
        if login:
            await client.start(phone=lambda:getpass.getpass('Телефон Telegram: '),
                code_callback=lambda:getpass.getpass('Код входа: '),password=lambda:getpass.getpass('Пароль двухэтапной защиты: '))
            if (await client.get_me()).bot:raise SystemExit('Нужен аккаунт пользователя, не бота.')
            print('Сессия сохранена на сервере. Коды и файл сессии никому не передавайте.')
            return
        if not await client.is_user_authorized():raise SystemExit('Сначала выполните tgmonitor.py --login через SSH.')
        me=await client.get_me()
        if me.bot:raise SystemExit('Нужна сессия пользователя, не бота.')
        path=os.environ.get('NEWS_STATE_PATH','/var/lib/newsbot/sent.sqlite3')
        owner=int(os.environ['TELEGRAM_ADMIN_ID'])
        api=Telegram(os.environ['TELEGRAM_BOT_TOKEN'])
        store=Store(path,{'categories':{},'targets':[]})
        state=ReaderState(path)
        next_poll=0
        try:
            while True:
                if state.blocked():
                    await asyncio.sleep(5)
                    continue
                watches=store.get('tg_watch') or {}
                for actor,name,at in state.db.execute('SELECT actor,username,at FROM tg_requests').fetchall():
                    if name in watches and time.time()-at<600 and authorized(path,owner,actor):
                        try:
                            def guarded(method,**kwargs):
                                if not authorized(path,owner,actor):raise ValueError('Доступ отозван.')
                                return api(method,**kwargs)
                            await poll_channel(state,client,watches[name],guarded,
                                lambda chat: delivery_target(guarded,chat,owner,path),preview=actor)
                        except errors.FloodWaitError as exc:
                            next_poll=max(next_poll,time.time()+exc.seconds)
                            state.pause(next_poll)
                            print('Telegram просит паузу:',exc.seconds,'секунд')
                            break
                        except Exception:
                            if authorized(path,owner,actor):
                                try:api('sendMessage',chat_id=actor,text='Последний пост недоступен. Проверьте канал и состояние сборщика.')
                                except Exception:pass
                    with state.db:state.db.execute('DELETE FROM tg_requests WHERE actor=?',(actor,))
                if time.time()>=next_poll:
                    for name,watch in watches.items():
                        try:
                            await poll_channel(state,client,watch,api,lambda chat:delivery_target(api,chat,owner,path))
                        except errors.FloodWaitError as exc:
                            next_poll=time.time()+max(300,exc.seconds)
                            state.pause(next_poll)
                            state.status(name,'@'+name,'Telegram ограничил частоту. Ожидаем разрешённое время.')
                            break
                        except Exception:
                            state.status(name,'@'+name,'Не выполнено: проверьте публичность канала, сессию и получателей. Это не означает отсутствие постов.')
                        await asyncio.sleep(2)
                    next_poll=max(next_poll,time.time()+300)
                await asyncio.sleep(5)
        finally:
            state.close();store.db.close()
    finally:
        await client.disconnect()
        os.close(lock_fd)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Мониторинг публичных Telegram-каналов')
    parser.add_argument('--login',action='store_true')
    args=parser.parse_args()
    try:asyncio.run(run(args.login))
    except (KeyboardInterrupt,SystemExit):raise
    except Exception:raise SystemExit('Сборщик не запущен. Проверьте доступ к Telegram, настройки и сессию; секреты в журнал не выводятся.') from None
