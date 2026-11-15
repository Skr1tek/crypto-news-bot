import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import Future
from unittest.mock import Mock
from control import Store, Controller, due_slot, parse_schedule, DeliveryRunner

class ScheduleAndSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/'s.db', {'categories': {}, 'targets': []})
        self.addCleanup(self.store.db.close)
        self.api = Mock()
        self.c = Controller(self.store, self.api, 123)
        self.now = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)

    def message(self, text, user=123):
        self.c.handle({'message': {'from': {'id': user}, 'chat': {'id': user, 'type': 'private'}, 'text': text}})

    def callback(self, data):
        self.c.handle({'callback_query': {'id': 'q', 'from': {'id':123}, 'data':data,
                       'message':{'chat':{'id':123,'type':'private'}}}})

    def test_schedule_two_times_and_restart_marker(self):
        slots = parse_schedule('12:00, 08:00, 12:00')
        self.assertEqual(slots, ['08:00','12:00'])
        self.assertEqual(due_slot(self.now, '2026-11-15 08:00', slots), '2026-11-15 12:00')
        self.assertIsNone(due_slot(self.now, '2026-11-15 12:00', slots))
        self.assertIsNone(due_slot(self.now, None, []))
        self.assertIsNone(due_slot(datetime(2026,11,15,4,59,tzinfo=timezone.utc), None, slots))

    def test_schedule_wizard_validates_before_saving(self):
        self.callback('schedule_edit')
        self.message('25:00')
        self.assertIsNone(self.store.get('schedule'))
        self.message('08:00, 12:00')
        self.assertEqual(self.store.get('schedule'), ['08:00','12:00'])
        self.assertIsNone(self.store.get('session'))

    def test_comma_lists_and_explanations(self):
        self.message('/add')
        self.message('Банки')
        self.message('банкротство банков, отзыв лицензии')
        self.message('банкрот, лиценз; АСВ')
        topic = next(iter(self.store.config()['categories'].values()))
        self.assertEqual(topic['queries'], ['банкротство банков', 'отзыв лицензии'])
        self.assertEqual(topic['keywords'], ['банкрот', 'лиценз', 'АСВ'])
        self.assertTrue(any('заголовк' in str(c) for c in self.api.call_args_list))

    def test_unauthorized_now_ignored(self):
        self.message('/now', user=999)
        self.api.assert_not_called()
        self.assertIsNone(self.store.get('manual_request'))

    def runner(self):
        self.future = Future()
        self.worker = Mock()
        self.worker.submit.return_value = self.future
        return DeliveryRunner(self.store, self.api, 123, self.worker)

    def test_manual_is_serialized_and_does_not_consume_schedule(self):
        self.store.put('manual_request','abc')
        runner = self.runner()
        runner.tick(self.now)
        runner.tick(self.now)
        self.assertEqual(self.worker.submit.call_count,1)
        self.future.set_result(0)
        runner.tick(self.now)
        self.assertIsNone(self.store.get('manual_request'))
        self.assertEqual(self.worker.submit.call_count,2) # automatic slot still runs
        self.assertIsNone(self.store.get('last_slot'))
        self.assertTrue(any('Новых' in str(c) for c in self.api.call_args_list))

    def test_schedule_success_and_failure_markers(self):
        runner = self.runner()
        runner.tick(self.now)
        self.future.set_result(3)
        runner.tick(self.now)
        self.assertEqual(self.store.get('last_slot'), '2026-11-15 08:00')
        self.store.put('schedule',['08:00','12:00'])
        self.future = Future()
        self.worker.submit.return_value = self.future
        runner.tick(self.now)
        self.future.set_exception(ValueError('failed'))
        runner.tick(self.now)
        self.assertEqual(self.store.get('last_slot'), '2026-11-15 08:00')

    def test_legacy_day_is_migrated_without_repeat(self):
        self.store.put('last_daily','2026-11-15')
        runner = self.runner()
        runner.tick(self.now)
        self.worker.submit.assert_not_called()

    def test_manual_confirmation_and_duplicate_click(self):
        self.store.put('config', {'categories': {},'targets':[{'chat_id':'123','categories':['a']}]})
        self.message('/now')
        self.assertIsNone(self.store.get('manual_request'))
        self.callback('sendnow')
        request = self.store.get('manual_request')
        self.assertTrue(request)
        self.callback('sendnow')
        self.assertEqual(self.store.get('manual_request'),request)
