import json
import os
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock,patch
import assistant_api
from control import Controller,Store


def proposal(action='create_topic'):
    return dict(reply='Предлагаю рубрику',action=action,topic_id='',title='Банки',queries=['банкротство банков'],keywords=['банкрот'])

class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'s.db',{'categories':{},'targets':[]})
        self.addCleanup(self.store.db.close)
        self.worker=Mock()
        self.future=Future()
        self.worker.submit.return_value=self.future
        self.api=Mock()
        self.c=Controller(self.store,self.api,123,self.worker)
        self.env=patch.dict(os.environ,{'OPENROUTER_API_KEY':'sk-or-v1-secret-testing','OPENROUTER_MODEL':'test/model'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def message(self,text,user=123):
        self.c.handle({'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text}})

    def callback(self,data):
        self.c.handle({'callback_query':{'id':'q','from':{'id':123},'data':data,'message':{'chat':{'id':123,'type':'private'}}}})

    def test_creation_needs_confirmation_and_is_idempotent(self):
        self.message('/ai')
        self.message('Добавь Банки')
        self.future.set_result(proposal())
        self.c.tick_ai()
        self.assertEqual(self.store.config()['categories'],{})
        nonce=self.store.get('ai_draft')['nonce']
        self.callback('ai_apply:'+nonce)
        self.callback('ai_apply:'+nonce)
        self.assertEqual(len(self.store.config()['categories']),1)
        self.assertEqual(self.store.config()['targets'],[])

    def test_unauthorized_and_secret_messages_never_call_provider(self):
        self.message('/ai',999)
        self.message('Добавь тему',999)
        self.message('/ai')
        self.message('Ключ sk-or-v1-secret-testing')
        self.worker.submit.assert_not_called()

    def test_busy_cancel_and_history_clear(self):
        self.message('/ai')
        self.message('Помоги')
        self.message('Ещё вопрос')
        self.assertEqual(self.worker.submit.call_count,1)
        self.message('/cancel')
        self.future.set_result(proposal())
        self.c.tick_ai()
        self.assertIsNone(self.store.get('ai_draft'))
        self.callback('ai_clear')
        self.assertEqual(self.store.get('ai_history'),[])

    def test_stale_update_does_not_overwrite_newer_topic(self):
        original={'title':'Старая','queries':['q'],'keywords':['k']}
        self.store.put('config',{'categories':{'a':original},'targets':[]})
        self.message('/ai')
        self.message('Измени рубрику a')
        result=proposal('update_topic');result['topic_id']='a'
        self.future.set_result(result);self.c.tick_ai()
        nonce=self.store.get('ai_draft')['nonce']
        changed=dict(original,title='Новое имя вручную')
        self.store.put('config',{'categories':{'a':changed},'targets':[]})
        self.callback('ai_apply:'+nonce)
        self.assertEqual(self.store.config()['categories']['a'],changed)

    def test_schema_and_error_safety(self):
        bad=proposal();bad['action']='execute_shell'
        with self.assertRaises(assistant_api.AssistantError):
            assistant_api.validate_proposal(bad,{'categories':{}})
        response=Mock(status_code=401,text='SECRET KEY')
        with patch('assistant_api.requests.post',return_value=response):
            with self.assertRaises(assistant_api.AssistantError) as error:
                assistant_api.ask('Привет',{'categories':{}},[])
        self.assertNotIn('SECRET',str(error.exception))

    def test_api_contract_and_context_redaction(self):
        payload={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(proposal())}}]}
        with patch('assistant_api.requests.post',return_value=Mock(status_code=200,json=lambda:payload)) as post:
            answer=assistant_api.ask('Добавь рубрику',{'categories':{}},[])
        self.assertEqual(answer['action'],'create_topic')
        self.assertEqual(post.call_args.args[0],'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(post.call_args.kwargs['json']['response_format']['type'],'json_schema')
        self.assertNotIn('tools',post.call_args.kwargs['json'])

    def test_daily_cap_and_invalid_output(self):
        from datetime import datetime
        import bot
        self.store.put('ai_usage',{'day':datetime.now(bot.MSK).date().isoformat(),'count':30})
        self.message('/ai');self.message('Помоги')
        self.worker.submit.assert_not_called()
        self.store.put('ai_usage',{})
        self.message('Помоги')
        self.future.set_result({'reply':'bad'})
        self.c.tick_ai()
        self.assertEqual(self.store.config()['categories'],{})
        self.assertIsNone(self.store.get('ai_draft'))
