import os
import tempfile
from datetime import datetime, timezone
import unittest
from unittest.mock import patch
import requests
import bot

class SecretSafetyTests(unittest.TestCase):
    def test_request_error_does_not_expose_token(self):
        item = {'title': 'Тестовая публикация', 'url': 'https://example.org/article',
                'date': datetime.now(timezone.utc), 'event': False, 'categories': ['regulation']}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'TELEGRAM_BOT_TOKEN': 'FAKE_TEST_TOKEN', 'TELEGRAM_CHAT_ID': '123', 'NEWS_STATE_PATH': tmp + '/state.db'}, clear=True), \
             patch.object(bot, 'collect_news', return_value=[item]), \
             patch.object(bot.requests, 'post', side_effect=requests.ConnectionError('https://api.telegram.org/botFAKE_TEST_TOKEN/sendMessage')):
            with self.assertRaises(SystemExit) as caught:
                bot.main()
            self.assertNotIn('FAKE_TEST_TOKEN', str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_missing_secrets_fail_before_network(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(bot.requests, 'post') as post:
            with self.assertRaises(SystemExit):
                bot.main()
            post.assert_not_called()
