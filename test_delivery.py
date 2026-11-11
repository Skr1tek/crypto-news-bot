import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, Mock

import bot


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {'TELEGRAM_BOT_TOKEN': 'test-token',
                    'TELEGRAM_CHAT_ID': 'legacy',
                    'NEWS_STATE_PATH': self.tmp.name + '/sent.sqlite3'}
        self.item = dict(title='ЦФА правила', url='https://example.org/news',
                         date=datetime.now(timezone.utc), event=False,
                         source='Test', categories=['cfa'])

    def run_bot(self, items, post):
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(bot, 'collect_news', return_value=items), \
             patch.object(bot.requests, 'post', post):
            bot.main()

    def test_repeat_run_does_not_send_seen_items(self):
        post = Mock(return_value=Mock(json=lambda: {'ok': True}))
        self.run_bot([self.item], post)
        self.run_bot([self.item], post)
        self.assertEqual(post.call_count, 1)

    def test_failed_target_retried_without_repeating_success(self):
        self.env['TELEGRAM_TARGETS'] = json.dumps([
            {'chat_id': 'one', 'categories': ['cfa']},
            {'chat_id': 'two', 'categories': ['cfa']},
            {'chat_id': 'three', 'categories': ['events']}])
        post = Mock(side_effect=[Mock(json=lambda: {'ok': True}),
                                 Mock(json=lambda: {'ok': False})])
        with self.assertRaises(SystemExit):
            self.run_bot([self.item], post)
        retry = Mock(return_value=Mock(json=lambda: {'ok': True}))
        self.run_bot([self.item], retry)
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(retry.call_args.kwargs['json']['chat_id'], 'two')

    def test_title_and_url_aliases_deduplicate(self):
        post = Mock(return_value=Mock(json=lambda: {'ok': True}))
        self.run_bot([self.item], post)
        changed = dict(self.item, title='Измененный заголовок')
        self.run_bot([changed], post)
        changed = dict(self.item, url='https://elsewhere.test/story')
        self.run_bot([changed], post)
        self.assertEqual(post.call_count, 1)

    def test_legacy_config_and_invalid_category(self):
        with patch.dict(os.environ, self.env, clear=True):
            config = bot.load_config()
        self.assertEqual(config['targets'][0]['chat_id'], 'legacy')
        self.env['TELEGRAM_TARGETS'] = '[{"chat_id":"x","categories":["unknown"]}]'
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(ValueError):
                bot.load_config()

    def test_custom_categories_filter_before_limit(self):
        config = {'categories': {'custom': {'queries': ['test'], 'keywords': ['новость']}},
                  'targets': [{'chat_id': 'x', 'categories': ['custom']}]}
        path = self.tmp.name + '/config.json'
        with open(path, 'w', encoding='utf-8') as stream:
            json.dump(config, stream)
        self.env['NEWS_CONFIG_PATH'] = path
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(bot.load_config()['targets'][0]['categories'], ['custom'])

    def test_unseen_items_are_selected_before_five_item_limit(self):
        post = Mock(return_value=Mock(json=lambda: {'ok': True}))
        items = [dict(self.item, title=f'ЦФА правило {i}',
                      url=f'https://example.org/{i}') for i in range(8)]
        self.run_bot(items, post)
        self.run_bot(items, post)
        self.assertEqual(post.call_count, 2)
        self.assertIn('ЦФА правило 7', post.call_args.kwargs['json']['text'])

    def test_collect_keeps_overlapping_categories(self):
        import time
        entry = {'title': 'Крипто новость', 'link': 'https://example.org/item',
                 'published_parsed': time.gmtime()}
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(bot.requests, 'get', return_value=Mock(content=b'rss')), \
             patch.object(bot.feedparser, 'parse', return_value=Mock(entries=[entry])):
            items = bot.collect_news()
        self.assertEqual(set(c for item in items for c in item['categories']),
                         {'regulation', 'cryptobanks', 'cfa', 'events'})

    def test_long_messages_split_and_do_not_repeat(self):
        post = Mock(return_value=Mock(json=lambda: {'ok': True}))
        items = [dict(self.item, title=f'{i} ' + 'крипто ' * 100,
                      url='https://example.org/' + str(i) + 'x' * 1400)
                 for i in range(5)]
        self.run_bot(items, post)
        count = post.call_count
        self.assertGreater(count, 1)
        for call in post.call_args_list:
            self.assertLessEqual(len(call.kwargs['json']['text'].encode('utf-16-le')) // 2, 4096)
        self.run_bot(items, post)
        self.assertEqual(post.call_count, count)

