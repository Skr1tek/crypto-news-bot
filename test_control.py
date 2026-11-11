import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path
from control import Store, Controller, validate_target, due
from datetime import datetime, timezone

class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.store = Store(self.path, {'categories': {}, 'targets': []})
        self.addCleanup(self.store.db.close)
        self.api = Mock()
        self.c = Controller(self.store, self.api, 123)

    def message(self, text, user=123, chat=123):
        self.c.handle({'update_id': 1, 'message': {'from': {'id': user}, 'chat': {'id': chat, 'type': 'private'}, 'text': text}})

    def test_unauthorized_cannot_read_or_change(self):
        self.message('/add', user=999)
        self.assertFalse(self.api.called)
        self.assertEqual(self.store.config()['categories'], {})

    def test_wizard_persists_and_cancel(self):
        self.message('/add')
        self.message('Банки')
        self.message('банкротство банков; отзыв лицензии')
        self.message('банкрот; лиценз')
        reopened = Store(self.path, {})
        self.addCleanup(reopened.db.close)
        config = reopened.config()
        self.assertEqual(next(iter(config['categories'].values()))['title'], 'Банки')
        self.message('/add')
        self.message('/cancel')
        self.assertIsNone(self.store.get('session'))

    def test_validation_rejects_alias_bool_and_zero(self):
        for bad in [True, 'https://evil.test', '@channel', '0', '1.2']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_target(self.api, bad, 123)
        self.api.assert_not_called()

    def test_channel_requires_bot_post_permission_and_admin_ownership(self):
        self.api.side_effect = [dict(id=-1001, type='channel', title='News'), dict(id=77),
                                dict(status='administrator', can_post_messages=False)]
        with self.assertRaises(ValueError):
            validate_target(self.api, '-1001', 123)

    def test_schedule_moscow(self):
        self.assertFalse(due(datetime(2026, 11, 11, 4, 59, tzinfo=timezone.utc), None))
        self.assertTrue(due(datetime(2026, 11, 11, 5, 0, tzinfo=timezone.utc), None))
        self.assertFalse(due(datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc), '2026-11-11'))


class RoutingTests(ControlTests):
    def callback(self, data, user=123, chat=123):
        self.c.handle({'callback_query': {'id': 'q', 'from': {'id': user}, 'data': data,
                       'message': {'chat': {'id': chat, 'type': 'private'}}}})

    def test_unauthorized_callback_does_not_ack_or_mutate(self):
        self.callback('add', user=999)
        self.api.assert_not_called()
        self.assertIsNone(self.store.get('session'))

    def test_edit_delete_and_multiple_recipients_survive_restart(self):
        self.store.put('config', {'categories': {'a': {'title': 'A', 'queries': ['q'], 'keywords': ['k']}},
            'targets': [{'chat_id': '123', 'categories': []}, {'chat_id': '-1001', 'categories': []}]})
        self.callback('edit:a:title')
        self.message('Новое имя')
        self.assertEqual(self.store.config()['categories']['a']['title'], 'Новое имя')
        from unittest.mock import patch
        with patch('control.validate_target'):
            self.callback('toggle:a:123')
            self.callback('toggle:a:-1001')
        reopened = Store(self.path, {})
        self.addCleanup(reopened.db.close)
        config = reopened.config()
        self.assertTrue(all(t['categories'] == ['a'] for t in config['targets']))
        self.callback('delete:a')
        self.assertIn('a', self.store.config()['categories'])
        self.callback('confirm:a')
        self.assertEqual(self.store.config()['categories'], {})
        self.assertTrue(all(not t['categories'] for t in self.store.config()['targets']))

    def test_native_chat_picker_and_remove_recipient(self):
        self.message('/recipient')
        markup = self.api.call_args.kwargs['reply_markup']
        self.assertTrue(markup['keyboard'][1][0]['request_chat']['chat_is_channel'])
        from unittest.mock import patch
        with patch('control.validate_target', return_value={'chat_id': '-1001', 'title': 'N', 'categories': []}):
            self.c.handle({'message': {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
                                      'chat_shared': {'request_id': 1, 'chat_id': -1001}}})
        self.assertEqual(len(self.store.config()['targets']), 1)
        self.callback('deltarget:-1001')
        self.assertEqual(self.store.config()['targets'], [])

    def test_invalid_input_does_not_save_partial_topic(self):
        self.message('/add')
        self.message('Name')
        self.message(' ')
        self.assertEqual(self.store.config()['categories'], {})
        self.assertEqual(self.store.get('session')['step'], 'queries')

    def test_private_target_requires_reachable_started_chat(self):
        self.api.side_effect = [{'id': 999, 'type': 'private'}, ValueError('blocked')]
        with self.assertRaises(ValueError):
            validate_target(self.api, 999, 123)
        self.api.side_effect = [{'id': 999, 'type': 'private', 'first_name': 'Reader'}, True]
        self.assertEqual(validate_target(self.api, 999, 123)['chat_id'], '999')
        self.assertEqual(self.api.call_args.args[0], 'sendChatAction')

    def test_channel_requires_owner_admin(self):
        self.api.side_effect = [{'id': -1001, 'type': 'channel'}, {'id': 77},
            {'status': 'administrator', 'can_post_messages': True}, {'status': 'member'}]
        with self.assertRaises(ValueError):
            validate_target(self.api, -1001, 123)

    def test_telegram_errors_hide_token(self):
        import requests
        from control import Telegram
        from unittest.mock import patch
        with patch('control.requests.post', side_effect=requests.ConnectionError('https://token-secret')):
            with self.assertRaises(ValueError) as error:
                Telegram('token-secret')('getMe')
        self.assertNotIn('token-secret', str(error.exception))

if __name__ == '__main__':
    unittest.main()
