import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock
import fedmonitor as fed
from control import Store,Controller

class FedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'s.db'
        self.store=Store(self.path,{'categories':{},'targets':[]})
        self.addCleanup(self.store.db.close)
        self.api=Mock()
        self.controller=Controller(self.store,self.api,123)
        self.watch={'inn':'7831000108','name':'ТАВРИЧЕСКИЙ БАНК (АО)','guid':'c0264aec-6d25-4ca5-a8a5-351d297e1b3b','targets':['123']}
        self.now=datetime(2026,11,16,12,tzinfo=timezone.utc)
        self.record={'url':'https://fedresurs.ru/bankruptmessages/9ef26ff1-cbb9-48cd-af74-7f79ee7aa01a',
                     'number':'25536049','date':self.now.astimezone(fed.MSK),'debtor':self.watch['name'],'text':'Текст <сообщения> & детали. '*500}

    def message(self,text,user=123):
        self.controller.handle({'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text}})

    def callback(self,data):
        self.controller.handle({'callback_query':{'id':'q','from':{'id':123},'data':data,'message':{'chat':{'id':123,'type':'private'}}}})

    def test_inn_checksum_and_day_boundary(self):
        self.assertTrue(fed.valid_inn('7831000108'))
        for value in ('7831000109','783100010','abc','783100010800'):
            self.assertFalse(fed.valid_inn(value))
        self.assertEqual(fed.moscow_day(datetime(2026,11,15,21,tzinfo=timezone.utc)).isoformat(),'2026-11-16')

    def test_admin_add_validation_and_confirmation(self):
        self.message('/fedadd',999)
        self.api.assert_not_called()
        self.message('/fedadd')
        self.message('7831000109')
        self.assertIsNone(self.store.get('fed_resolve'))
        self.message('7831000108')
        self.assertEqual(self.store.get('fed_resolve'),'7831000108')
        self.assertIsNone(self.store.get('fed_watch'))
        self.store.put('fed_pending',dict(self.watch,nonce='abc',created=__import__('time').time()))
        self.callback('fed_confirm:abc')
        self.assertEqual(self.store.get('fed_watch')['7831000108']['targets'],['123'])

    def test_text_parts_are_complete_escaped_and_limited(self):
        parts=fed.message_parts(self.watch,self.record)
        self.assertGreater(len(parts),1)
        self.assertTrue(all('25536049' in p and '16.11.2026' in p and self.watch['name'] in p for p in parts))
        self.assertTrue(all(len(p.encode('utf-16-le'))//2<=4096 for p in parts))
        self.assertTrue(any('&lt;сообщения&gt;' in p for p in parts))
        self.assertTrue(all(self.record['url'] in p for p in parts))

    def reader(self):
        reader=Mock()
        reader.list_today.return_value=[{'url':self.record['url']}]
        reader.message.return_value=self.record
        return reader

    def test_confirmed_parts_not_repeated_and_failure_retried(self):
        reader=self.reader()
        calls=[]
        def api(method,**kwargs):
            calls.append(kwargs['text'])
            if len(calls)==2:
                raise ValueError('fail')
        result=fed.scan(self.path,[self.watch],api,lambda target:None,self.now,reader)
        self.assertTrue(result['errors'])
        api2=Mock()
        result=fed.scan(self.path,[self.watch],api2,lambda target:None,self.now,reader)
        self.assertFalse(result['errors'])
        self.assertNotEqual(api2.call_args_list[0].kwargs['text'],calls[0])
        again=fed.scan(self.path,[self.watch],api2,lambda target:None,self.now,reader)
        self.assertEqual(again['sent'],0)

    def test_blocked_source_is_error_not_empty_success(self):
        reader=self.reader()
        reader.list_today.side_effect=fed.FedError('Сайт недоступен')
        result=fed.scan(self.path,[self.watch],self.api,lambda target:None,self.now,reader)
        self.assertTrue(result['errors'])
        self.api.assert_not_called()

    def test_yesterday_is_not_sent(self):
        reader=self.reader()
        reader.message.return_value=dict(self.record,date=self.record['date']-timedelta(days=1))
        fed.scan(self.path,[self.watch],self.api,lambda target:None,self.now,reader)
        self.api.assert_not_called()

    def test_inline_command_and_remove_confirmation(self):
        self.message('/fedadd 7831000108')
        self.assertEqual(self.store.get('fed_resolve'),'7831000108')
        self.store.put('fed_watch',{'7831000108':self.watch})
        self.callback('fed_delete:7831000108')
        self.assertIn('7831000108',self.store.get('fed_watch'))
        self.callback('fed_remove:7831000108')
        self.assertEqual(self.store.get('fed_watch'),{})

    def test_failed_recipient_does_not_block_other_recipient(self):
        reader=self.reader()
        watch=dict(self.watch,targets=['bad','123'])
        def validate(chat):
            if chat=='bad':
                raise ValueError('no access')
        result=fed.scan(self.path,[watch],self.api,validate,self.now,reader)
        self.assertTrue(result['errors'])
        self.assertEqual(result['sent'],1)
        self.assertTrue(all(c.kwargs['chat_id']=='123' for c in self.api.call_args_list))

    def test_valid_empty_source_and_order_validation(self):
        reader=self.reader()
        reader.list_today.return_value=[]
        result=fed.scan(self.path,[self.watch],self.api,lambda chat:None,self.now,reader)
        self.assertEqual(result['checked'],1)
        self.assertFalse(result['errors'])
        self.api.assert_not_called()
        page=Mock()
        page.locator.return_value.evaluate_all.return_value=[
            {'href':'/bankruptmessages/9ef26ff1-cbb9-48cd-af74-7f79ee7aa01a','text':'25536049 от 16.11.2026'},
            {'href':'/bankruptmessages/86270567-f181-4e94-9402-04ada5378807','text':'25515473 от 13.11.2026'}]
        source=fed.BrowserReader();source.page=page
        self.assertEqual(len(source.rows()),2)
        page.locator.return_value.evaluate_all.return_value.reverse()
        with self.assertRaises(fed.FedError):
            source.rows()

    def test_coordinator_resolves_without_adding_until_confirmation(self):
        from concurrent.futures import Future
        worker=Mock();future=Future();worker.submit.return_value=future
        self.store.put('fed_resolve','7831000108')
        coordinator=fed.Coordinator(self.store,self.api,123,worker,lambda target:None)
        coordinator.tick(self.now)
        future.set_result({k:self.watch[k] for k in ('inn','name','guid')})
        coordinator.tick(self.now)
        self.assertIsNone(self.store.get('fed_watch'))
        self.assertEqual(self.store.get('fed_pending')['inn'],'7831000108')
        self.assertIsNone(self.store.get('fed_resolve'))

    def test_message_fields_from_visible_page_and_full_text_chunks(self):
        source=fed.BrowserReader()
        source.visit=Mock()
        page=Mock()
        body='25536049 от 16.11.2026 15:03:22 МСК\nДолжник\nА56-88395/2025\nТАВРИЧЕСКИЙ БАНК (АО)\nИНН\n7831000108\nТекст сообщения\nПолный текст <сообщения>\nСвязанные сообщения\n24154864'
        def locator(selector):
            if selector=='body':
                return Mock(inner_text=lambda:body)
            empty=Mock(count=lambda:0)
            empty.filter.return_value=empty
            return empty
        page.locator.side_effect=locator
        source.page=page
        record=source.message({'url':self.record['url'],'number':'25536049'},self.watch)
        self.assertEqual(record['debtor'],self.watch['name'])
        self.assertEqual(record['date'].hour,15)
        self.assertEqual(record['text'],'Полный текст <сообщения>')
        import html
        parts=fed.message_parts(self.watch,self.record)
        reconstructed=''.join(html.unescape(p.split('МСК\n',1)[1].rsplit('\n<a ',1)[0]) for p in parts)
        self.assertEqual(reconstructed,self.record['text'])
