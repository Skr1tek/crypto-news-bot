import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from datetime import date
from checkoreader import CheckoReader
from fedmonitor import FedError, message_parts

class CheckoTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'state.db')
        self.watch={'inn':'7831000108','name':'Банк <АО>'}
    def reader(self, replies):
        session=Mock()
        session.post.side_effect=[Mock(status_code=200,json=Mock(return_value=x)) for x in replies]
        reader=CheckoReader(self.path,key='secret',session=session)
        return reader,session
    def payload(self, page=1, pages=1):
        return {'company':{'ИНН':self.watch['inn'],'НаимСокр':'Банк'},
          'meta':{'status':'ok','today_request_count':page},
          'data':{'СтрВсего':pages,'СтрТекущ':page,'Записи':[{'GUID':'52AA0DA05112E6EA8104A9DAC18F6950',
          'URL':'https://fedresurs.ru/bankruptmessage/52AA0DA05112E6EA8104A9DAC18F6950',
          'Дата':'2026-11-17','ТипНаим':'Торги <имущество>'}]}}
    def test_pages_links_and_cache(self):
        reader,session=self.reader([self.payload(1,2),self.payload(2,2)])
        with reader:
            rows=reader.list_today(self.watch,date(2026,11,17))
            self.assertEqual(len(rows),1)
            text=message_parts(self.watch,reader.message(rows[0],self.watch))[0]
            self.assertIn('<a href="https://fedresurs.ru/bankruptmessages/52aa0da0-5112-e6ea-8104-a9dac18f6950">',text)
            self.assertIn('Торги &lt;имущество&gt;',text)
            self.assertNotIn('Сообщение №',text)
            reader.list_today(self.watch,date(2026,11,17))
        self.assertEqual(session.post.call_count,2)
    def test_persistent_limit_and_failed_attempts(self):
        reader,session=self.reader([])
        session.post.side_effect=RuntimeError('secret')
        with reader:
            for _ in range(90):
                with self.assertRaises(FedError): reader.request(self.watch['inn'])
        other,other_session=self.reader([self.payload()])
        with other:
            with self.assertRaisesRegex(FedError,'Лимит'): other.request(self.watch['inn'])
        other_session.post.assert_not_called()
    def test_remote_budget_blocks_next_request(self):
        p=self.payload(); p['meta']['today_request_count']=99
        reader,session=self.reader([p])
        with reader:
            reader.request(self.watch['inn'])
            with self.assertRaises(FedError): reader.request(self.watch['inn'],page=2)
        self.assertEqual(session.post.call_count,1)
    def test_api_error_not_empty_result(self):
        reader,_=self.reader([{'meta':{'status':'error','message':'secret'}}])
        with reader:
            with self.assertRaises(FedError): reader.list_today(self.watch,date(2026,11,17))
    def test_external_link_rejected(self):
        p=self.payload();p['data']['Записи'][0]['URL']='https://evil.test/message'
        reader,_=self.reader([p])
        with reader:
            with self.assertRaises(FedError): reader.list_today(self.watch,date(2026,11,17))

    def test_latest_requests_one_record_without_date_filter(self):
        reader,session=self.reader([self.payload()])
        with reader:
            row=reader.latest(self.watch)
        self.assertEqual(row['date'].date(),date(2026,11,17))
        body=session.post.call_args.kwargs['json']
        self.assertEqual(body['sort'],'-date')
        self.assertEqual(body['limit'],1)
        self.assertNotIn('date_from',body)

    def test_latest_button_is_admin_only_and_explicit(self):
        from control import Store,Controller
        store=Store(self.path,{'categories':{},'targets':[]})
        self.addCleanup(store.db.close)
        store.put('fed_watch',{self.watch['inn']:dict(self.watch,guid='',targets=['123'])})
        api=Mock(); controller=Controller(store,api,123)
        def click(user):
            controller.handle({'callback_query':{'id':'q','from':{'id':user},'data':'fed_latest:'+self.watch['inn'],
                'message':{'chat':{'id':user,'type':'private'}}}})
        click(999)
        self.assertIsNone(store.get('fed_latest'))
        click(123)
        self.assertEqual(store.get('fed_latest'),self.watch['inn'])

    def test_latest_preview_does_not_change_delivery_history(self):
        from unittest.mock import patch
        import fedmonitor as fed
        reader,_=self.reader([self.payload()])
        api=Mock()
        with patch.object(fed,'reader_factory',return_value=reader):
            fed.latest_job(self.path,self.watch,api,123)
        self.assertEqual(api.call_args.kwargs['chat_id'],123)
        self.assertEqual(api.call_args.kwargs['parse_mode'],'HTML')
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='fed_sent'").fetchone())

    def test_ai_gets_metadata_and_redacts_checko_key(self):
        from unittest.mock import patch
        import assistant_api
        from checkoreader import publication_context
        reader,_=self.reader([self.payload()])
        with reader: reader.latest(self.watch)
        context=publication_context(self.path)
        self.assertEqual(context[0]['topic'],'Торги <имущество>')
        self.assertNotIn('text',context[0])
        with patch.dict('os.environ',{'CHECKO_API_KEY':'hidden-key'}):
            self.assertNotIn('hidden-key',assistant_api.redact('key hidden-key'))
