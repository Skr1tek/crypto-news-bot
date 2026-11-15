import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import os
import bot
from control import Store, Controller

class NewsLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'state.db'
        self.store = Store(self.path, {'categories':{},'targets':[]})
        self.addCleanup(self.store.db.close)
        self.api = Mock()
        self.controller = Controller(self.store,self.api,123)

    def message(self,text,user=123):
        self.controller.handle({'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text}})

    def test_limit_wizard_persists_and_rejects_invalid_numbers(self):
        self.message('/limit')
        for value in ('0','51','-1','2.5','abc'):
            self.message(value)
            self.assertNotIn('news_limit',self.store.config())
        self.message('12')
        reopened = Store(self.path,{})
        self.addCleanup(reopened.db.close)
        self.assertEqual(reopened.config()['news_limit'],12)
        self.assertIsNone(self.store.get('session'))

    def test_unauthorized_cannot_change_count(self):
        self.message('/limit',999)
        self.message('12',999)
        self.api.assert_not_called()
        self.assertNotIn('news_limit',self.store.config())

    def test_delivery_respects_count_per_target_and_deduplicates(self):
        config = {'news_limit':2,'categories':{'a':{'queries':['q'],'keywords':['k']}},
                  'targets':[{'chat_id':'one','categories':['a']},{'chat_id':'two','categories':['a']}]}
        items=[dict(title=f'Новость {i}',url=f'https://example.org/{i}',categories=['a'],
                    date=datetime.now(timezone.utc),event=False) for i in range(5)]
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'test','NEWS_STATE_PATH':str(self.path)},clear=True), \
             patch.object(bot,'collect_news',return_value=items),patch.object(bot,'send_message') as send:
            self.assertEqual(bot.main(config=config),4)
            self.assertTrue(all(len(call.args[2])==2 for call in send.call_args_list))
            self.assertEqual(bot.main(config=config),4)
            self.assertEqual(bot.main(config=config),2)
            self.assertEqual(bot.main(config=config),0)

    def test_configuration_rejects_bool_and_out_of_range(self):
        path=Path(self.tmp.name)/'config.json'
        for value in (True,False,0,51,1.5,'10'):
            path.write_text(json.dumps({'news_limit':value}),encoding='utf-8')
            with self.subTest(value=value),patch.dict(os.environ,{'NEWS_CONFIG_PATH':str(path),'TELEGRAM_CHAT_ID':'123'},clear=True):
                with self.assertRaises(ValueError):
                    bot.load_config()
