"""Owner-issued administration rights, isolated conversations, shared configuration."""
from contextlib import closing
import copy
import json
import sqlite3
import time
import uuid
from control import Controller, TelegramError, validate_target
import fedmonitor


def authorized(path,owner,user):
    if isinstance(user,bool) or not isinstance(user,int) or user<=0:
        return False
    if user==owner:
        return True
    try:
        with closing(sqlite3.connect(path,timeout=5)) as db:
            row=db.execute("SELECT value FROM control WHERE key='access_admins'").fetchone()
        return str(user) in (json.loads(row[0]) if row else {})
    except (sqlite3.Error,ValueError,TypeError):
        return False


class ScopedStore:
    private={'session','ai_mode','ai_draft','ai_history','fed_resolve','fed_pending','fed_manual','fed_latest','fed_notice'}
    def __init__(self,store,user):
        self.base,self.user,self.path=store,user,store.path
    def key(self,key):
        return 'admin:'+str(self.user)+':'+key if key in self.private else key
    def get(self,key):
        return self.base.get(self.key(key))
    def put(self,key,value):
        self.base.put(self.key(key),value)
        if key=='manual_request' and value:
            self.base.put('manual_actor',self.user)
    def config(self):
        return copy.deepcopy(self.get('config'))


def delivery_target(api,chat,owner,path):
    """Bot posting rights plus management rights of at least one current administrator."""
    with closing(sqlite3.connect(path,timeout=5)) as db:
        row=db.execute("SELECT value FROM control WHERE key='access_admins'").fetchone()
    users=[owner]+[int(v) for v in (json.loads(row[0]) if row else {})]
    for user in users:
        try:
            return validate_target(api,chat,user)
        except ValueError:
            pass
    raise ValueError('Права публикации или права текущих администраторов не подтверждены.')


class AccessManager:
    def __init__(self,store,api,owner,ai_worker,fed_worker):
        self.store,self.api,self.owner=store,api,owner
        self.ai_worker,self.fed_worker=ai_worker,fed_worker
        self.controllers={};self.fed_runners={}
        self.ensure(owner)

    def ensure(self,user):
        if user in self.controllers:
            return self.controllers[user]
        scope=self.store if user==self.owner else ScopedStore(self.store,user)
        def guarded(method,**kwargs):
            if not authorized(self.store.path,self.owner,user):
                raise TelegramError('Доступ отозван владельцем.')
            return self.api(method,**kwargs)
        controller=Controller(scope,guarded,user,self.ai_worker)
        controller.owner=self.owner
        self.controllers[user]=controller
        if self.fed_worker is not None:
            self.fed_runners[user]=fedmonitor.Coordinator(scope,guarded,user,self.fed_worker,
                lambda chat: delivery_target(guarded,chat,self.owner,self.store.path),automatic=user==self.owner)
        return controller

    def list_admins(self):
        c=self.controllers[self.owner]
        c.say('Владелец: '+str(self.owner)+'\nАдминистраторы имеют доступ ко всем настройкам. Только владелец выдаёт и отзывает доступ.',
            [[('Выдать доступ','access_add')],[('Меню','menu')]])
        for user,person in (self.store.get('access_admins') or {}).items():
            c.say(person['name']+' ('+user+')',[[('Отозвать доступ','access_remove:'+user)]])

    def handle(self,update):
        callback=update.get('callback_query')
        message=callback.get('message',{}) if callback else update.get('message',{})
        actor=callback.get('from',{}) if callback else message.get('from',{})
        user=actor.get('id')
        if message.get('chat',{}).get('type')!='private' or message.get('chat',{}).get('id')!=user or not authorized(self.store.path,self.owner,user):
            return
        controller=self.ensure(user)
        action=callback.get('data','') if callback else ''
        text=message.get('text','').strip()
        session=controller.store.get('session') or {}
        access=(action.startswith('access_') or text=='/admins' or session.get('step')=='access_user' and text!='/cancel')
        if not access:
            return controller.handle(update)
        if callback:
            controller.api('answerCallbackQuery',callback_query_id=callback['id'])
        if user!=self.owner:
            return controller.say('Выдавать и отзывать доступ может только владелец.')
        try:
            if text=='/admins' or action=='access_menu':
                self.store.put('session',None)
                self.store.put('access_draft',None)
                return self.list_admins()
            if action=='access_add':
                self.store.put('session',{'step':'access_user'})
                return controller.say('Попросите пользователя открыть @News_crp_bot и нажать /start. Затем выберите его. Доступ будет выдан только после подтверждения. /cancel — отмена.',
                    markup={'keyboard':[[{'text':'Выбрать администратора','request_users':{'request_id':4,'user_is_bot':False,'max_quantity':1,'request_name':True}}]],'resize_keyboard':True,'one_time_keyboard':True})
            if action.startswith('access_remove:'):
                target=int(action.split(':',1)[1])
                if target==self.owner or str(target) not in (self.store.get('access_admins') or {}):
                    raise ValueError()
                nonce=uuid.uuid4().hex[:16]
                self.store.put('access_draft',{'user':target,'nonce':nonce,'at':time.time(),'kind':'revoke'})
                return controller.say('Отозвать полный доступ у '+str(target)+'?',[[('Да, отозвать','access_revoke:'+nonce),('Отмена','access_menu')]])
            if action.startswith(('access_grant:','access_revoke:')):
                draft=self.store.get('access_draft') or {}
                kind='grant' if action.startswith('access_grant:') else 'revoke'
                if draft.get('kind')!=kind or draft.get('nonce')!=action.split(':',1)[1] or time.time()-draft.get('at',0)>600:
                    raise ValueError()
                admins=self.store.get('access_admins') or {}
                target=draft['user']
                if target==self.owner:
                    raise ValueError()
                if kind=='grant':
                    if len(admins)>=10 and str(target) not in admins:
                        return controller.say('Достигнут лимит 10 дополнительных администраторов.')
                    admins[str(target)]={'name':draft['name']}
                else:
                    admins.pop(str(target),None)
                self.store.put('access_admins',admins)
                self.store.put('access_draft',None)
                self.store.put('session',None)
                if kind=='revoke':
                    self.drop(target)
                return controller.say('Доступ выдан. Пользователь может открыть /start.' if kind=='grant' else 'Доступ отозван.',markup={'remove_keyboard':True})
            if session.get('step')=='access_user':
                shared=message.get('users_shared',{})
                users=shared.get('users',[])
                if shared.get('request_id')!=4 or len(users)!=1:
                    return controller.say('Выберите человека кнопкой «Выбрать администратора». /cancel — отмена.')
                target=users[0].get('user_id')
                if isinstance(target,bool) or not isinstance(target,int) or target<=0 or target==self.owner:
                    raise ValueError()
                person=validate_target(controller.api,target,self.owner)
                nonce=uuid.uuid4().hex[:16]
                self.store.put('access_draft',{'user':target,'name':person['title'],'kind':'grant','nonce':nonce,'at':time.time()})
                self.store.put('session',None)
                return controller.say('Выдать '+person['title']+' полный доступ к настройкам, рассылке, ЕФРСБ и ИИ? Только вы сможете управлять списком администраторов.',
                    [[('Выдать полный доступ','access_grant:'+nonce),('Отмена','access_menu')]])
        except (ValueError,KeyError,TypeError):
            controller.say('Доступ не изменён. Проверьте выбор пользователя, /start у него и срок подтверждения. /cancel — отмена.')

    def drop(self,user):
        if user==self.owner:
            return
        controller=self.controllers.pop(user,None)
        if controller and controller.ai_future:
            controller.ai_future.cancel()
        runner=self.fed_runners.pop(user,None)
        if runner and runner.future:
            runner.future.cancel()
        for key in ScopedStore.private:
            self.store.put('admin:'+str(user)+':'+key,None)

    def tick(self,now):
        for user,controller in list(self.controllers.items()):
            if not authorized(self.store.path,self.owner,user):
                self.drop(user)
                continue
            controller.tick_ai()
            if user in self.fed_runners:
                self.fed_runners[user].tick(now)
