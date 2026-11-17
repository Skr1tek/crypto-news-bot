"""Free public-site Fedresurs monitoring. No private API, CAPTCHA bypass or paid service."""
from contextlib import closing
from datetime import datetime,timedelta,timezone
import html
import os
import re
import sqlite3
import time
import uuid
from urllib.parse import urlsplit

MSK=timezone(timedelta(hours=3))
ROOT='https://fedresurs.ru'
GUID=r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
MESSAGE=re.compile(r'^/(?:bankruptmessages|sfactmessage)/('+GUID+r')$',re.I)

class FedError(ValueError):
    pass


def valid_inn(value):
    return isinstance(value,str) and bool(re.fullmatch(r'[0-9]{10}',value)) and sum(int(c)*w for c,w in zip(value[:9],(2,4,10,3,5,9,4,6,8)))%11%10==int(value[-1])


def moscow_day(now):
    return now.astimezone(MSK).date()


def message_url(value):
    p=urlsplit(value)
    if p.scheme!='https' or p.netloc!='fedresurs.ru' or not MESSAGE.fullmatch(p.path) or p.query or p.fragment:
        raise FedError('Неизвестная ссылка сообщения Федресурса.')
    return ROOT+p.path


class BrowserReader:
    """Single ephemeral browser per job; read only the public, rendered website."""
    def __enter__(self):
        self.pw=None
        self.browser=None
        try:
            from playwright.sync_api import sync_playwright
            self.pw=sync_playwright().start()
            clean_env={k:v for k,v in os.environ.items() if k.upper() in (
                'PATH','HOME','LANG','LC_ALL','TEMP','TMP','SYSTEMROOT','WINDIR','USERPROFILE','LOCALAPPDATA','PLAYWRIGHT_BROWSERS_PATH')}
            self.browser=self.pw.chromium.launch(headless=True,channel='chromium',chromium_sandbox=True,
                args=['--disable-dev-shm-usage'],env=clean_env)
            self.context=self.browser.new_context(locale='ru-RU',timezone_id='Europe/Moscow',viewport={'width':1280,'height':900},accept_downloads=False)
            self.page=self.context.new_page()
            self.page.set_default_timeout(30000)
            return self
        except Exception:
            self.__exit__(None,None,None)
            raise FedError('Chromium не запущен. Установите requirements-fedresurs.txt и браузер Playwright, проверьте sandbox и память.') from None

    def __exit__(self,*args):
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        finally:
            if self.pw:
                try:
                    self.pw.stop()
                except Exception:
                    pass

    def visit(self,path):
        try:
            response=self.page.goto(ROOT+path,wait_until='domcontentloaded',timeout=45000)
            if response and response.status>=400:
                raise FedError('Федресурс ограничил доступ (HTTP '+str(response.status)+'). Проверка не выполнена.')
            self.settle()
            accept=self.page.get_by_role('button',name='Принять',exact=True)
            if accept.count() and accept.is_visible():
                accept.click()
                self.settle()
        except FedError:
            raise
        except Exception:
            raise FedError('Сайт Федресурса не загрузился. Это не означает отсутствие публикаций.') from None

    def settle(self):
        self.page.wait_for_load_state('networkidle',timeout=20000)
        text=self.page.locator('body').inner_text().lower()
        if any(word in text for word in ('access blocked','captcha','проверка безопасности','подтвердите, что вы не робот','доступ заблокирован')):
            raise FedError('Федресурс требует проверку человека или ограничил доступ. Автоматическая проверка недоступна.')

    def resolve(self,inn):
        if not valid_inn(inn):
            raise FedError('Некорректный ИНН юридического лица.')
        self.visit('/entities?searchString='+inn)
        matches={}
        for card in self.page.locator('.u-card-result').all():
            text=card.inner_text()
            if not re.search(r'ИНН\s*'+inn+r'(?![0-9])',text):
                continue
            for link in card.locator('a[href^="/companies/"]').all():
                href=link.get_attribute('href') or ''
                if re.fullmatch('/companies/'+GUID,href):
                    matches[href]={'inn':inn,'name':link.inner_text().strip()[:250],'guid':href.rsplit('/',1)[1]}
        if len(matches)!=1:
            raise FedError('Не удалось однозначно найти компанию по ИНН. Сайт может быть недоступен или компания не найдена.')
        return next(iter(matches.values()))

    def rows(self):
        rows=[]
        for item in self.page.locator('a[href]').evaluate_all('(els) => els.map(e => ({href:e.getAttribute("href"), text:e.innerText}))'):
            href=item.get('href') or ''
            if not MESSAGE.fullmatch(href):
                continue
            match=re.fullmatch(r'\s*([0-9]+)\s+от\s+([0-9]{2}\.[0-9]{2}\.[0-9]{4})\s*',item['text'])
            if match:
                rows.append({'number':match[1],'day':datetime.strptime(match[2],'%d.%m.%Y').date(),'url':ROOT+href})
        dates=[row['day'] for row in rows]
        if dates!=sorted(dates,reverse=True):
            raise FedError('Порядок публикаций на сайте изменился. Нужна проверка загрузки всех сообщений.')
        return rows

    def list_today(self,watch,day):
        if not re.fullmatch(GUID,watch['guid']):
            raise FedError('Некорректный идентификатор компании.')
        self.visit('/companies/'+watch['guid']+'/publications')
        self.page.locator('.tab__btn').first.wait_for()
        found={}
        tabs=self.page.locator('.tab__btn')
        for index in range(tabs.count()):
            tab=tabs.nth(index)
            if 'tab__btn_active' not in (tab.get_attribute('class') or ''):
                tab.click()
                self.settle()
            count=tab.locator('.tab__count').inner_text().replace('\xa0','').replace(' ','').strip()
            if count=='0':
                continue
            for _ in range(200):
                rows=self.rows()
                if not rows:
                    raise FedError('Список публикаций не прочитан. Пустой результат не подтверждён.')
                for row in rows:
                    if row['day']==day:
                        found[row['url']]=row
                if min(row['day'] for row in rows)<day:
                    break
                more=self.page.get_by_text('Загрузить еще',exact=True)
                if not more.count() or not more.is_visible():
                    break
                before=len(rows)
                more.click()
                self.settle()
                if len(self.rows())<=before:
                    raise FedError('Не удалось загрузить следующую страницу публикаций.')
            else:
                raise FedError('Слишком много страниц за день. Проверка неполная; сообщения не будут выданы как полный результат.')
        return sorted(found.values(),key=lambda r:(r['day'],int(r['number'])))

    def message(self,row,watch):
        url=message_url(row['url'])
        self.visit(url[len(ROOT):])
        body=self.page.locator('body').inner_text()
        stamp=re.search(r'\b'+re.escape(row['number'])+r'\s+от\s+([0-9]{2}\.[0-9]{2}\.[0-9]{4})(?:\s+([0-9]{2}:[0-9]{2}:[0-9]{2}))?',body)
        if not stamp:
            raise FedError('Не прочитана дата сообщения '+row['number']+'.')
        date=datetime.strptime(stamp[1]+' '+(stamp[2] or '00:00:00'),'%d.%m.%Y %H:%M:%S').replace(tzinfo=MSK)
        section=self.page.locator('.message-text-header').filter(has_text=re.compile(r'^\s*Текст сообщения\s*$'))
        if section.count():
            text=section.first.locator('..').locator('text-formatter').inner_text().strip()
        else:
            text=re.split(r'Связанные сообщения|Данные о подписи',body.split('Текст сообщения',1)[-1],maxsplit=1)[0].strip() if 'Текст сообщения' in body else ''
        if not text:
            raise FedError('Текст сообщения '+row['number']+' недоступен или скрыт источником.')
        debtor='Не указан в сообщении'
        if 'Должник' in body:
            block=body.split('Должник',1)[1].split('ИНН',1)[0]
            lines=[v.strip() for v in block.splitlines() if v.strip()]
            if lines:
                debtor=lines[-1][:250]
        return {'url':url,'number':row['number'],'date':date,'debtor':debtor,'text':text}


def message_parts(watch,record):
    url=message_url(record['url'])
    if record.get('source')=='checko':
        return ['📋 <b>ЕФРСБ · Checko</b>\n<b>'+html.escape(watch['name'][:250])+'</b>\nИНН: '+watch['inn']+\
            '\nТема: '+html.escape(record['title'])+'\nДата: '+record['date'].strftime('%d.%m.%Y')+\
            ('\n⚠️ Сообщение аннулировано' if record.get('annulled') else '')+\
            '\n<a href="'+html.escape(url,quote=True)+'">Открыть сообщение</a>']
    header=('📋 <b>Федресурс</b>\nКомпания: '+html.escape(watch['name'][:250])+'\nИНН: '+watch['inn']+
            '\nДолжник: '+html.escape(record['debtor'][:250])+'\nСообщение №'+html.escape(str(record['number']))+
            ' от '+record['date'].astimezone(MSK).strftime('%d.%m.%Y %H:%M:%S')+' МСК\n')
    footer='\n<a href="'+html.escape(url,quote=True)+'">Посмотреть сообщение</a>'
    text=record['text']
    parts=[]
    start=0
    while start<len(text):
        low,high=1,min(3000,len(text)-start)
        while low<high:
            size=(low+high+1)//2
            candidate=header+html.escape(text[start:start+size])+footer
            if len(candidate.encode('utf-16-le'))//2<=3900:
                low=size
            else:
                high=size-1
        parts.append(header+html.escape(text[start:start+low])+footer)
        start+=low
    return parts


def scan(path,watches,api,validate,now,reader):
    result={'sent':0,'checked':0,'errors':[],'day':moscow_day(now).isoformat()}
    def error(watch,exc):
        reason=str(exc) if isinstance(exc,FedError) else 'Источник не прочитан или отправка не подтверждена. Проверьте доступ к сайту и права получателя.'
        text=watch['inn']+': '+reason
        if text not in result['errors']:
            result['errors'].append(text)
    with closing(sqlite3.connect(path,timeout=10)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS fed_sent (inn TEXT, chat_id TEXT, message_id TEXT, part INTEGER, PRIMARY KEY(inn,chat_id,message_id,part))')
        for watch in watches:
            try:
                if not valid_inn(watch['inn']):
                    raise FedError('Некорректный ИНН в мониторинге.')
                rows=reader.list_today(watch,moscow_day(now))
                result['checked']+=1
            except Exception as exc:
                error(watch,exc)
                continue
            for row in rows:
                complete={r[0] for r in db.execute('SELECT chat_id FROM fed_sent WHERE inn=? AND message_id=? AND part=-1',(watch['inn'],row['url']))}
                recipients=[chat for chat in watch['targets'] if chat not in complete]
                if not recipients:
                    continue
                try:
                    record=reader.message(row,watch)
                    if moscow_day(record['date'])!=moscow_day(now):
                        continue
                    parts=message_parts(watch,record)
                except Exception as exc:
                    error(watch,exc)
                    continue
                for chat in recipients:
                    try:
                        known={r[0] for r in db.execute('SELECT part FROM fed_sent WHERE inn=? AND chat_id=? AND message_id=?',(watch['inn'],chat,record['url']))}
                        validate(chat)
                        for index,text in enumerate(parts):
                            if index in known:
                                continue
                            api('sendMessage',chat_id=chat,text=text,parse_mode='HTML',disable_web_page_preview=True)
                            db.execute('INSERT OR IGNORE INTO fed_sent VALUES(?,?,?,?)',(watch['inn'],chat,record['url'],index))
                            db.commit()
                        db.execute('INSERT OR IGNORE INTO fed_sent VALUES(?,?,?,-1)',(watch['inn'],chat,record['url']))
                        db.commit()
                        result['sent']+=1
                    except Exception as exc:
                        error(watch,exc)
    return result


def reader_factory(path=None):
    if os.environ.get('CHECKO_API_KEY','').strip():
        from checkoreader import CheckoReader
        return CheckoReader(path)
    return BrowserReader()


def resolve_job(inn,path=None):
    with reader_factory(path) as reader:
        return reader.resolve(inn)


def scan_job(path,watches,api,validate,now):
    try:
        with reader_factory(path) as reader:
            return scan(path,watches,api,validate,now,reader)
    except Exception as exc:
        reason=str(exc) if isinstance(exc,FedError) else 'Браузерная проверка не выполнена.'
        return {'sent':0,'checked':0,'errors':[reason],'day':moscow_day(now).isoformat()}


def latest_job(path,watch,api,admin):
    with reader_factory(path) as reader:
        if not hasattr(reader,'latest'):
            raise FedError('Последняя запись доступна при подключении Checko.')
        row=reader.latest(watch)
        if row is None:
            api('sendMessage',chat_id=admin,text='В Checko нет записей по ИНН '+watch['inn'])
            return
        for part in message_parts(watch,reader.message(row,watch)):
            api('sendMessage',chat_id=admin,text=part,parse_mode='HTML',disable_web_page_preview=True)


class Coordinator:
    def __init__(self,store,api,admin,worker,validate):
        self.store,self.api,self.admin,self.worker,self.validate=store,api,admin,worker,validate
        self.future=None
        self.job=None

    def say(self,text,rows=None):
        self.api('sendMessage',chat_id=self.admin,text=text[:3800],reply_markup={'inline_keyboard':rows or [[{'text':'Меню','callback_data':'menu'}]]})

    def tick(self,now):
        if self.future is not None and self.future.done():
            kind,identity=self.job
            future,self.future=self.future,None
            if kind=='latest':
                try:
                    future.result()
                except Exception as exc:
                    self.say(str(exc) if isinstance(exc,FedError) else 'Последняя запись не получена.')
                finally:
                    self.store.put('fed_latest',None)
            elif kind=='resolve':
                try:
                    watch=future.result()
                    watch.update(nonce=uuid.uuid4().hex[:12],created=time.time(),targets=[str(self.admin)])
                    self.store.put('fed_pending',watch)
                    self.say('Найдено: '+watch['name']+'\nИНН '+watch['inn']+'\nДобавить на мониторинг? Проверяется только текущий день по МСК.',
                        [[{'text':'Добавить','callback_data':'fed_confirm:'+watch['nonce']},{'text':'Отмена','callback_data':'fed_menu'}]])
                except Exception as exc:
                    self.say(str(exc) if isinstance(exc,FedError) else 'Не удалось проверить компанию. Попробуйте позже.')
                finally:
                    if self.store.get('fed_resolve')==identity:
                        self.store.put('fed_resolve',None)
            else:
                try:
                    result=future.result()
                except Exception:
                    result={'sent':0,'checked':0,'errors':['Проверка Федресурса прервана. Результат не подтверждён.'],'day':moscow_day(now).isoformat()}
                self.store.put('fed_status',dict(result,checked_at=now.astimezone(MSK).isoformat()))
                self.store.put('fed_last_poll',time.time())
                signature='\n'.join(result['errors'])
                old=self.store.get('fed_notice') or {}
                notified = bool(identity or signature and (old.get('text')!=signature or time.time()-old.get('at',0)>21600))
                if notified:
                    self.say('Федресурс за '+result['day']+': проверено компаний '+str(result['checked'])+', отправлено сообщений '+str(result['sent'])+
                        ('\nПроверка неполная:\n'+signature if signature else '\nПроверка завершена. Уже отправленные сообщения не повторяются.'))
                self.store.put('fed_notice',{'text':signature,'at':time.time() if notified or signature!=old.get('text') else old.get('at',time.time())})
                if self.store.get('fed_manual')==identity:
                    self.store.put('fed_manual',None)
        if self.future is not None:
            return
        inn=self.store.get('fed_resolve')
        watches=list((self.store.get('fed_watch') or {}).values())
        manual=self.store.get('fed_manual')
        latest=self.store.get('fed_latest')
        if latest:
            watch=(self.store.get('fed_watch') or {}).get(latest)
            if not watch:
                self.store.put('fed_latest',None)
                return
            self.job=('latest',latest)
            self.future=self.worker.submit(latest_job,self.store.path,watch,self.api,self.admin)
        elif inn:
            self.job=('resolve',inn)
            self.future=self.worker.submit(resolve_job,inn,self.store.path)
        elif watches and (manual or time.time()-(self.store.get('fed_last_poll') or 0)>=(21600 if os.environ.get('CHECKO_API_KEY','').strip() else 900)):
            self.job=('scan',manual)
            self.future=self.worker.submit(scan_job,self.store.path,watches,self.api,self.validate,now)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Проверка бесплатного сайта Федресурса, без отправки в Telegram')
    parser.add_argument('--check',required=True,metavar='ИНН')
    args=parser.parse_args()
    if not valid_inn(args.check):
        raise SystemExit('Нужен корректный 10-значный ИНН юридического лица.')
    try:
        with reader_factory() as reader:
            company=reader.resolve(args.check)
            print('Компания:',company['name'],'ИНН',company['inn'])
            now=datetime.now(timezone.utc)
            rows=reader.list_today(company,moscow_day(now))
            print('Дата МСК:',moscow_day(now),'Найдено сообщений:',len(rows))
            for row in rows:
                record=reader.message(row,company)
                print(record.get('title') or 'Сообщение №'+record['number'],record['date'].isoformat(),record['url'])
        print('Проверка источника завершена. Отправки в Telegram не было.')
    except Exception as exc:
        raise SystemExit(str(exc) if isinstance(exc,FedError) else 'Проверка не выполнена: сайт недоступен или изменил разметку.') from None
