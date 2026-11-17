"""Checko EFRSB metadata. Durable conservative quota shared by all callers."""
from contextlib import closing
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime
from urllib.parse import urlsplit
import requests
from fedmonitor import FedError, MSK, valid_inn


class CheckoReader:
    def __init__(self,path=None,key=None,session=None):
        self.path=path or os.environ.get('NEWS_STATE_PATH','sent.sqlite3')
        self.key=key if key is not None else os.environ.get('CHECKO_API_KEY','').strip()
        self.session=session or requests.Session()

    def __enter__(self):
        if not self.key:
            raise FedError('Задайте CHECKO_API_KEY на сервере.')
        self.db=sqlite3.connect(self.path,timeout=10)
        self.db.execute('CREATE TABLE IF NOT EXISTS checko_requests (at REAL NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS checko_cache (id TEXT PRIMARY KEY, at REAL, payload TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS checko_quota (id INTEGER PRIMARY KEY, at REAL, used INTEGER)')
        self.db.commit()
        return self

    def __exit__(self,*args):
        self.db.close()
        self.session.close()

    def request(self,inn,**params):
        if not valid_inn(inn):
            raise FedError('Некорректный ИНН юридического лица.')
        identity=json.dumps([inn,params],sort_keys=True)
        now=time.time()
        cached=self.db.execute('SELECT payload FROM checko_cache WHERE id=? AND at>?',(identity,now-300)).fetchone()
        if cached:
            return json.loads(cached[0])
        # Reserve before network IO: failed and timed-out requests also consume our budget.
        self.db.execute('BEGIN IMMEDIATE')
        count=self.db.execute('SELECT COUNT(*) FROM checko_requests WHERE at>?',(now-86400,)).fetchone()[0]
        quota=self.db.execute('SELECT at,used FROM checko_quota WHERE id=1').fetchone()
        blocked=count>=90 or (quota and quota[1]>=90 and quota[0]>now-86400)
        if blocked:
            self.db.rollback()
            raise FedError('Лимит Checko: максимум 90 запросов за 24 часа; резерв 10 запросов. Повторите позже.')
        self.db.execute('INSERT INTO checko_requests VALUES(?)',(now,))
        self.db.execute('DELETE FROM checko_requests WHERE at<?',(now-172800,))
        self.db.commit()
        try:
            response=self.session.post('https://api.checko.ru/v2/bankruptcy-messages',
                json={'key':self.key,'inn':inn,**params},timeout=30,allow_redirects=False)
            if response.status_code!=200:
                raise FedError('Checko: HTTP '+str(response.status_code)+'. Проверка не выполнена.')
            payload=response.json()
            meta=payload.get('meta',{})
            used=meta.get('today_request_count')
            if isinstance(used,int) and not isinstance(used,bool):
                self.db.execute('INSERT OR REPLACE INTO checko_quota VALUES(1,?,?)',(now,used))
                self.db.commit()
            if meta.get('status')!='ok':
                raise FedError('Checko отклонил запрос. Проверьте ключ, баланс и доступ к методу ЕФРСБ.')
            company=payload.get('company',{})
            if company.get('ИНН')!=inn or not isinstance(payload.get('data',{}).get('Записи'),list):
                raise FedError('Checko вернул неполные данные или другую компанию.')
            self.db.execute('INSERT OR REPLACE INTO checko_cache VALUES(?,?,?)',(identity,now,json.dumps(payload,ensure_ascii=False)))
            self.db.commit()
            return payload
        except FedError:
            raise
        except Exception:
            raise FedError('Checko недоступен или вернул неверный ответ. Попытка учтена в лимите.') from None

    def resolve(self,inn):
        payload=self.request(inn,limit=1,sort='-date')
        company=payload['company']
        name=company.get('НаимСокр') or company.get('НаимПолн')
        if not isinstance(name,str) or not name.strip():
            raise FedError('Checko не вернул наименование компании.')
        return {'inn':inn,'name':name[:250],'guid':''}

    def row(self,item):
        try:
            guid=uuid.UUID(item['GUID'])
            parsed=urlsplit(item['URL'])
            if parsed.scheme!='https' or parsed.netloc!='fedresurs.ru' or parsed.query or parsed.fragment:
                raise ValueError()
            if not re.fullmatch(r'/bankruptmessages?/'+r'[0-9a-fA-F-]+',parsed.path) or uuid.UUID(parsed.path.rsplit('/',1)[1])!=guid:
                raise ValueError()
            date=datetime.strptime(item['Дата'],'%Y-%m-%d').replace(tzinfo=MSK)
            title=item['ТипНаим']
            if not isinstance(title,str) or not title.strip() or len(title)>1000:
                raise ValueError()
            return {'url':'https://fedresurs.ru/bankruptmessages/'+str(guid),'date':date,
                'title':title,'source':'checko','annulled':item.get('Аннул') is True}
        except (ValueError,KeyError,TypeError,AttributeError):
            raise FedError('Checko: неверные дата, тема или ссылка публикации.') from None

    def list_today(self,watch,day):
        rows={}
        page=1
        while True:
            data=self.request(watch['inn'],date_from=day.isoformat(),date_to=day.isoformat(),limit=100,page=page,sort='-date')['data']
            pages=data.get('СтрВсего')
            if not isinstance(pages,int) or isinstance(pages,bool) or not 0<=pages<=100 or data.get('СтрТекущ')!=page:
                raise FedError('Checko: неверная пагинация; проверка неполная.')
            for item in data['Записи']:
                row=self.row(item)
                if row['date'].date()==day:
                    rows[row['url']]=row
            if page>=pages:
                return list(rows.values())
            page+=1

    def latest(self,watch):
        records=self.request(watch['inn'],limit=1,sort='-date')['data']['Записи']
        return self.row(records[0]) if records else None

    def message(self,row,watch):
        return dict(row,text='',debtor=watch['name'],number='')


def publication_context(path):
    """Only cached metadata; no extra Checko calls from AI questions."""
    try:
        with closing(sqlite3.connect(path,timeout=5)) as db:
            cached=db.execute('SELECT payload FROM checko_cache ORDER BY at DESC LIMIT 30').fetchall()
        records={}
        for (raw,) in cached:
            payload=json.loads(raw)
            company=payload['company']
            for row in payload['data']['Записи']:
                records[row['GUID']]={'company':company.get('НаимСокр'),'inn':company['ИНН'],
                    'topic':row.get('ТипНаим'),'date':row.get('Дата'),'url':row.get('URL'),
                    'case':row.get('НомерДела'),'annulled':row.get('Аннул',False)}
        return sorted(records.values(),key=lambda r:r['date'] or '',reverse=True)[:20]
    except (sqlite3.Error,ValueError,KeyError,TypeError):
        return []
