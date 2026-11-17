import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import assistant_api as ai
from control import Controller,Store

class WebTests(unittest.TestCase):
    def payload(self,annotations=True):
        result={'reply':'Найденные сведения','action':'advice','topic_id':'','title':'','queries':[],'keywords':[]}
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result),
          'annotations':[{'type':'url_citation','url_citation':{'url':'https://example.org/news','title':'Источник'}}] if annotations else []}}]}
    def test_search_is_opt_in_and_separate_from_normal_chat(self):
        with patch.dict(os.environ,{'OPENROUTER_API_KEY':'secret','OPENROUTER_MODEL':'test/model','OPENROUTER_WEB_SEARCH':'0'}),patch('assistant_api.requests.post') as post:
            with self.assertRaises(ai.AssistantError): ai.ask('Новости',{'categories':{}},[],web=True)
            post.assert_not_called()
    def test_plugin_citations_and_no_internal_context(self):
        with patch.dict(os.environ,{'OPENROUTER_API_KEY':'secret','OPENROUTER_MODEL':'test/model','OPENROUTER_WEB_SEARCH':'1'}),patch('assistant_api.requests.post',return_value=Mock(status_code=200,json=lambda:self.payload())) as post:
            answer=ai.ask('Новости',{'categories':{},'private':'PRIVATE'},[{'role':'user','content':'HISTORY'}],web=True)
        body=post.call_args.kwargs['json']
        self.assertEqual(body['plugins'][0]['engine'],'exa')
        self.assertEqual(body['plugins'][0]['max_results'],3)
        self.assertNotIn('PRIVATE',json.dumps(body));self.assertNotIn('HISTORY',json.dumps(body))
        self.assertIn('https://example.org/news',answer['reply'])
        self.assertEqual(answer['action'],'advice')
    def test_missing_sources_is_not_successful_search(self):
        with patch.dict(os.environ,{'OPENROUTER_API_KEY':'secret','OPENROUTER_MODEL':'test/model','OPENROUTER_WEB_SEARCH':'1'}),patch('assistant_api.requests.post',return_value=Mock(status_code=200,json=lambda:self.payload(False))):
            with self.assertRaises(ai.AssistantError): ai.ask('Новости',{'categories':{}},[],web=True)
    def test_daily_limit_is_shared_and_disabled_costs_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'s.db',{'categories':{},'targets':[]})
            try:
                c=Controller(store,Mock(),123,Mock())
                with patch.dict(os.environ,{'OPENROUTER_API_KEY':'secret','OPENROUTER_MODEL':'test/model','OPENROUTER_WEB_SEARCH':'0'}):
                    c.start_ai('Новости',web=True)
                    c.ai_worker.submit.assert_not_called()
                from datetime import datetime
                import bot
                store.put('web_usage',{'day':datetime.now(bot.MSK).date().isoformat(),'count':5})
                with patch.dict(os.environ,{'OPENROUTER_API_KEY':'secret','OPENROUTER_MODEL':'test/model','OPENROUTER_WEB_SEARCH':'1'}):
                    c.start_ai('Новости',web=True)
                    c.ai_worker.submit.assert_not_called()
            finally: store.db.close()
    def test_sources_reject_unsafe_urls_and_deduplicate(self):
        annotations=[{'type':'url_citation','url_citation':{'url':url,'title':'Title'}} for url in (
          'javascript:alert(1)','https://127.0.0.1/','https://user:pass@example.com/',
          'https://example.com/a','https://example.com/a')]
        self.assertEqual(ai.web_sources(annotations),[('Title','https://example.com/a')])

    def test_command_authorization_and_button_query(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'s.db',{'categories':{},'targets':[]})
            try:
                worker=Mock();c=Controller(store,Mock(),123,worker)
                def message(user,text):
                    c.handle({'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text}})
                with patch.dict(os.environ,{'OPENROUTER_API_KEY':'secret','OPENROUTER_MODEL':'test/model','OPENROUTER_WEB_SEARCH':'1'}):
                    message(999,'/search Новости')
                    worker.submit.assert_not_called()
                    message(123,'/search')
                    self.assertEqual(store.get('session')['step'],'web_query')
                    message(123,'Новости')
                    self.assertTrue(worker.submit.call_args.kwargs['web'])
                    self.assertEqual(store.get('web_usage')['count'],1)
                    self.assertIsNone(store.get('session'))
            finally: store.db.close()

    def test_source_line_stays_clickable_when_reply_split(self):
        url='https://example.org/'+'x'*450
        parts=ai.reply_parts('А'*1750+'\nИсточник\n'+url)
        self.assertTrue(any(url in p for p in parts))
        self.assertTrue(all(len(p)<=1800 for p in parts))
