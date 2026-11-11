import unittest
from unittest.mock import Mock, patch
import requests
from control import Telegram

class TelegramDiagnosticsTests(unittest.TestCase):
    def test_network_categories_and_token_redaction(self):
        cases = [(requests.exceptions.SSLError, 'TLS'),
                 (requests.exceptions.ProxyError, 'proxy'),
                 (requests.Timeout, 'timeout'),
                 (requests.ConnectionError, 'connection')]
        for kind, label in cases:
            with self.subTest(kind=kind), patch('control.requests.post', side_effect=kind('SECRET-TOKEN')):
                with self.assertRaises(ValueError) as error:
                    Telegram('SECRET-TOKEN')('getWebhookInfo')
                self.assertIn(label, str(error.exception))
                self.assertNotIn('SECRET-TOKEN', str(error.exception))

    def test_http_codes_are_reported_without_server_text(self):
        for code in (400, 401, 403, 404, 409, 429, 502):
            response = Mock(status_code=code)
            response.json.return_value = {'ok': False, 'error_code': code, 'description': 'SECRET-TOKEN'}
            with self.subTest(code=code), patch('control.requests.post', return_value=response):
                with self.assertRaises(ValueError) as error:
                    Telegram('SECRET-TOKEN')('getMe')
                self.assertIn(str(code), str(error.exception))
                self.assertNotIn('SECRET-TOKEN', str(error.exception))

    def test_non_json_and_malformed_responses_are_safe(self):
        for value in ([], {'ok': True}, 'SECRET-TOKEN'):
            with patch('control.requests.post', return_value=Mock(status_code=200, json=lambda: value)):
                with self.assertRaises(ValueError) as error:
                    Telegram('SECRET-TOKEN')('getMe')
                self.assertIn('response', str(error.exception))
                self.assertNotIn('SECRET-TOKEN', str(error.exception))

    def test_success_returns_result(self):
        response = Mock(status_code=200, json=lambda: {'ok': True, 'result': {'id': 77}})
        with patch('control.requests.post', return_value=response):
            self.assertEqual(Telegram('test')('getMe'), {'id': 77})
