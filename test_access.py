import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from control import Store
from access import AccessManager, ScopedStore, authorized

class AccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'s.db',{'categories':{},'targets':[]})
        self.addCleanup(self.store.db.close)
        self.api=Mock(side_effect=lambda method,**kw: {'id':456,'type':'private','first_name':'Иван'} if method=='getChat' else True)
        self.manager=AccessManager(self.store,self.api,123,None,None)
    def message(self,user,text='',**fields):
        self.manager.handle({'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text,**fields}})
    def click(self,user,action,chat=None):
        self.manager.handle({'callback_query':{'id':'q','from':{'id':user},'data':action,
          'message':{'chat':{'id':user if chat is None else chat,'type':'private'}}}})
    def grant(self):
        self.message(123,'/admins')
        self.click(123,'access_add')
        self.message(123,users_shared={'request_id':4,'users':[{'user_id':456}]})
        draft=self.store.get('access_draft')
        self.click(123,'access_grant:'+draft['nonce'])
    def test_grant_confirm_and_persistence(self):
        self.message(123,'/admins');self.click(123,'access_add')
        self.message(123,users_shared={'request_id':4,'users':[{'user_id':456}]})
        self.assertFalse(authorized(self.store.path,123,456))
        self.click(123,'access_grant:'+self.store.get('access_draft')['nonce'])
        self.assertTrue(authorized(self.store.path,123,456))
        self.message(456,'/add')
        self.assertEqual(ScopedStore(self.store,456).get('session')['step'],'title')
    def test_nonowner_cannot_grant_or_revoke(self):
        self.grant();self.click(456,'access_add');self.click(456,'access_remove:123')
        self.assertTrue(authorized(self.store.path,123,123))
        self.assertEqual(list((self.store.get('access_admins') or {})),['456'])
        self.click(999,'access_add');self.assertFalse(authorized(self.store.path,123,999))
    def test_sessions_are_separate_configuration_shared(self):
        self.grant();self.message(456,'/add');self.message(123,'/schedule')
        self.assertEqual(ScopedStore(self.store,456).get('session')['step'],'title')
        ScopedStore(self.store,456).put('schedule',['12:00'])
        self.assertEqual(self.store.get('schedule'),['12:00'])
    def test_revocation_blocks_old_buttons_and_guarded_api(self):
        self.grant();self.message(456,'/start')
        guarded=self.manager.controllers[456].api
        self.click(123,'access_remove:456')
        self.click(123,'access_revoke:'+self.store.get('access_draft')['nonce'])
        self.assertFalse(authorized(self.store.path,123,456))
        self.message(456,'/add')
        with self.assertRaises(ValueError): guarded('sendMessage',chat_id=456,text='late')
        self.assertIsNone(ScopedStore(self.store,456).get('session'))
    def test_owner_cannot_be_removed_and_group_spoof_rejected(self):
        self.click(123,'access_remove:123')
        self.assertTrue(authorized(self.store.path,123,123))
        self.click(123,'access_add',chat=999)
        self.assertNotEqual((self.store.get('session') or {}).get('step'),'access_user')

    def test_old_confirmation_cannot_be_reused(self):
        self.message(123,'/admins');self.click(123,'access_add')
        self.message(123,users_shared={'request_id':4,'users':[{'user_id':456}]})
        draft=self.store.get('access_draft'); draft['at']=0
        self.store.put('access_draft',draft)
        self.click(123,'access_grant:'+draft['nonce'])
        self.assertFalse(authorized(self.store.path,123,456))

    def test_delegates_do_not_start_automatic_feed_scans(self):
        from datetime import datetime,timezone
        worker=Mock()
        self.manager.fed_worker=worker
        self.grant();self.message(456,'/start')
        runner=self.manager.fed_runners[456]
        self.store.put('fed_watch',{'7831000108':{'inn':'7831000108','name':'Банк','guid':'','targets':['456']}})
        runner.tick(datetime.now(timezone.utc))
        worker.submit.assert_not_called()
        ScopedStore(self.store,456).put('fed_latest','7831000108')
        runner.tick(datetime.now(timezone.utc))
        self.assertEqual(worker.submit.call_args.args[-1],456)

    def test_delegate_can_change_shared_settings_and_has_private_ai_history(self):
        self.grant();self.message(456,'/limit');self.message(456,'12')
        self.assertEqual(self.store.config()['news_limit'],12)
        scoped=ScopedStore(self.store,456)
        scoped.put('ai_history',[{'role':'user','content':'Личный вопрос'}])
        self.assertIsNone(self.store.get('ai_history'))
        self.assertEqual(scoped.get('ai_history')[0]['content'],'Личный вопрос')
