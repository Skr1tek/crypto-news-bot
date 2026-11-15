import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import Mock,patch
import bot
import assistant_api

class RubricTests(unittest.TestCase):
    def test_separate_messages_with_titles_and_shared_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            config={'categories':{'a':{'title':'Банки <РФ>','queries':['q'],'keywords':['k']},
                                  'b':{'title':'ЦФА','queries':['q'],'keywords':['k']}},
                    'targets':[{'chat_id':'123','categories':['a','b']}]}
            items=[dict(title='Банк отозвал лицензию',url='https://example.org/1',date=datetime.now(timezone.utc),event=False,categories=['a']),
                   dict(title='Размещение облигаций завершено',url='https://example.org/2',date=datetime.now(timezone.utc),event=False,categories=['b'])]
            post=Mock(return_value=Mock(json=lambda:{'ok':True}))
            with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'test','NEWS_STATE_PATH':str(Path(tmp)/'s.db')},clear=True), \
                 patch.object(bot,'collect_news',return_value=items),patch.object(bot.requests,'post',post):
                bot.main(config=config)
                self.assertEqual(post.call_count,2)
                texts=[call.kwargs['json']['text'] for call in post.call_args_list]
                self.assertTrue(any('Банки &lt;РФ&gt;' in t and items[1]['title'] not in t for t in texts))
                self.assertTrue(any('<b>ЦФА' in t and items[0]['title'] not in t for t in texts))
                bot.main(config=config)
                self.assertEqual(post.call_count,2)

