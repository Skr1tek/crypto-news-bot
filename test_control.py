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
        self.c.handle({'callback_query': {'id': 'q', 'from': {'id': user}, 'data': da…3221 tokens truncated…n    except (requests.RequestException, ValueError):
        # Request exceptions may contain the URL, which includes the bot token.
        raise ValueError('Telegram delivery failed') from None


def main(config=None, target_validator=None):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        raise SystemExit('Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID or TELEGRAM_TARGETS')
    try:
        config = load_config() if config is None else config
    except (ValueError, TypeError, OSError):
        raise SystemExit('Invalid news configuration; check categories and Telegram targets') from None
    state = Path(os.environ.get('NEWS_STATE_PATH', 'data/sent.sqlite3'))
    state.parent.mkdir(parents=True, exist_ok=True)
    failed = False
    with closing(sqlite3.connect(state)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS sent (chat_id TEXT, item_key TEXT, PRIMARY KEY (chat_id, item_key))')
        items = collect_news() if target_validator is None else collect_news(config)
        for target in config['targets']:
            chat_id = target['chat_id']
            if not target['categories']:
                continue
            if target_validator is not None:
                try:
                    target_validator(chat_id)
                except ValueError:
                    failed = True
                    continue
            known = {row[0] for row in db.execute('SELECT item_key FROM sent WHERE chat_id = ?', (chat_id,))}
            candidates = [item for item in items
                          if set(item.get('categories', config['categories'])) & set(target['categories'])]
            # Merge rubric memberships before selecting and deduplicating.
            fresh = []
            for item in rank_items(candidates, limit=None):
                keys = item_keys(item)
                if not known.intersection(keys):
                    fresh.append(item)
                    known.update(keys)
                if len(fresh) == 5:
                    break
            if not fresh and items:
                print('No new matching news for target', chat_id)
                continue
            # Batches stay below Telegram's message limit even with long links.
            batches, batch = [], []
            for item in fresh:
                if batch and len(format_digest(batch + [item]).encode('utf-16-le')) // 2 > 4000:
                    batches.append(batch)
                    batch = []
                batch.append(item)
            batches.append(batch)
            for batch in batches:
                try:
                    send_message(token, chat_id, batch)
                except ValueError:
                    failed = True
                    break
                for item in batch:
                    db.executemany('INSERT OR IGNORE INTO sent VALUES (?, ?)',
                                   [(chat_id, key) for key in item_keys(item)])
                db.commit()
                print('Telegram digest delivered')
    if failed:
        raise SystemExit('Telegram delivery failed; check credentials, chat access and network') from None


if __name__ == '__main__':
    main()

