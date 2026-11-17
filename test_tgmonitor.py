import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from datetime import datetime,timezone
from unittest.mock import Mock,AsyncMock
from tgmonitor import channel_name,post_parts,ReaderState,poll_channel

class TelegramSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state=ReaderState(Path(self.tmp.name)/'s.db');self.addCleanup(self.state.close)
        self.watch={'username':'public_news','keywords':['банк'],'targets':['123']}
        self.entity=NS(id=100,username='public_news',title='Канал <новости>',broadcast=True,megagroup=False)
        self.client=NS(get_entity=AsyncMock(return_value=self.entity),get_messages=AsyncMock())
        self.post=NS(id=12,raw_text='Банк <сообщение>',date=datetime(2026,11,17,tzinfo=timezone.utc))
        self.api=Mock();self.validate=Mock()
    def test_normalize_public_channels(self):
        for value in ('@public_news','https://t.me/public_news','https://t.me/s/public_news'):
            self.assertEqual(channel_name(value),'public_news')
        for bad in ('https://evil.test/public_news','https://t.me/+secret','https://t.me/c/123/1','name/12','abc'):
            with self.assertRaises(ValueError):channel_name(bad)
    async def test_first_poll_only_sets_baseline(self):
        self.client.get_messages.return_value=[self.post]
        await poll_channel(self.state,self.client,self.watch,self.api,self.validate)
        self.api.assert_not_called();self.assertEqual(self.state.cursor('public_news'),12)
    async def test_new_posts_deduplicate_and_retry_failed_delivery(self):
        self.state.advance('public_news',100,10)
        self.client.get_messages.return_value=[self.post]
        self.api.side_effect=ValueError('delivery failed')
        with self.assertRaises(ValueError):await poll_channel(self.state,self.client,self.watch,self.api,self.validate)
        self.assertEqual(self.state.cursor('public_news'),10)
        self.api.side_effect=None
        await poll_channel(self.state,self.client,self.watch,self.api,self.validate)
        self.assertEqual(self.state.cursor('public_news'),12)
        self.client.get_messages.return_value=[]
        calls=self.api.call_count
        await poll_channel(self.state,self.client,self.watch,self.api,self.validate)
        self.assertEqual(self.api.call_count,calls)
    async def test_latest_is_explicit_and_ignores_keyword_filter(self):
        self.client.get_messages.return_value=[self.post]
        await poll_channel(self.state,self.client,dict(self.watch,keywords=['нетсовпадений']),self.api,self.validate,preview=456)
        self.assertEqual(self.api.call_args.kwargs['chat_id'],456)
        self.assertIsNone(self.state.cursor('public_news'))
    async def test_nonmatching_post_is_skipped_but_cursor_advances(self):
        self.state.advance('public_news',100,10)
        self.client.get_messages.return_value=[self.post]
        await poll_channel(self.state,self.client,dict(self.watch,keywords=['другое']),self.api,self.validate)
        self.api.assert_not_called()
        self.assertEqual(self.state.cursor('public_news'),12)
    async def test_confirmation_history_is_per_recipient(self):
        self.state.advance('public_news',100,10)
        self.client.get_messages.return_value=[self.post]
        self.state.sent(100,12,'123',-1)
        await poll_channel(self.state,self.client,dict(self.watch,targets=['123','456']),self.api,self.validate)
        self.assertTrue(all(call.kwargs['chat_id']=='456' for call in self.api.call_args_list))
    async def test_private_or_reassigned_channel_rejected(self):
        self.entity.broadcast=False
        with self.assertRaises(ValueError):await poll_channel(self.state,self.client,self.watch,self.api,self.validate)
        self.entity.broadcast=True
        self.state.advance('public_news',999,10)
        with self.assertRaises(ValueError):await poll_channel(self.state,self.client,self.watch,self.api,self.validate)
    def test_active_links_escaping_and_size(self):
        parts=post_parts(self.entity,NS(id=12,raw_text='😀<банк>'*1500,date=self.post.date))
        self.assertTrue(all('https://t.me/public_news/12' in part for part in parts))
        self.assertTrue(all(len(part.encode('utf-16-le'))//2<4096 for part in parts))
        self.assertTrue(any('&lt;банк&gt;' in part for part in parts))

    def test_flood_pause_survives_restart(self):
        import time
        self.state.pause(time.time()+600)
        reopened=ReaderState(Path(self.tmp.name)/'s.db')
        try:self.assertTrue(reopened.blocked())
        finally:reopened.close()

    def test_worst_case_html_expansion_fits(self):
        self.entity.title='"'*200
        parts=post_parts(self.entity,NS(id=12,raw_text='"&'*4000,date=self.post.date))
        self.assertTrue(all(len(part.encode('utf-16-le'))//2<=3900 for part in parts))

    def test_ui_authorization_and_private_latest_queue(self):
        from control import Store,Controller
        store=Store(Path(self.tmp.name)/'s.db',{'categories':{},'targets':[]})
        try:
            c=Controller(store,self.api,123)
            def message(user,text):
                c.handle({'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text}})
            message(999,'/tgadd');message(999,'@public_news')
            self.assertIsNone(store.get('tg_watch'))
            message(123,'/tgadd');message(123,'https://t.me/public_news')
            self.assertEqual(store.get('tg_watch')['public_news']['targets'],['123'])
            c.handle({'callback_query':{'id':'q','from':{'id':123},'data':'tg_latest:public_news','message':{'chat':{'id':123,'type':'private'}}}})
            row=self.state.db.execute('SELECT actor,username FROM tg_requests').fetchone()
            self.assertEqual(row,(123,'public_news'))
        finally:store.db.close()
