import unittest
from unittest.mock import patch, Mock
import requests
from links import publisher_url, safe_article_url

class LinkTests(unittest.TestCase):
    def test_publisher_links_do_not_make_network_requests(self):
        with patch('links.requests.get') as get:
            self.assertEqual(publisher_url('https://publisher.test/news/1'), 'https://publisher.test/news/1')
            get.assert_not_called()

    def test_decodes_google_signature_and_rpc_without_fetching_publisher(self):
        import json
        page=Mock(text='<div data-n-a-ts="123" data-n-a-sg="sig"></div>')
        rpc=Mock(text=")]}'\n\n"+json.dumps([['wrb.fr','Fbv4je',json.dumps(['garturlres','https://publisher.test/news/1'])]]))
        with patch('links.requests.get',return_value=page) as get,patch('links.requests.post',return_value=rpc) as post:
            url=publisher_url('https://news.google.com/rss/articles/CBMi_example?oc=5')
            self.assertEqual(url,'https://publisher.test/news/1')
            self.assertEqual(get.call_count,1)
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
            self.assertTrue(post.call_args.args[0].startswith('https://news.google.com/'))

    def test_failures_do_not_return_rss_or_homepage(self):
        with patch('links.requests.get',side_effect=requests.Timeout):
            self.assertIsNone(publisher_url('https://news.google.com/rss/articles/CBMi_example'))

    def test_rejects_unsafe_urls_and_publisher_homepages(self):
        for url in ['javascript:alert(1)','http://localhost/article','http://127.0.0.1/a','https://user:pass@example.org/a',
                    'https://news.google.com/rss/articles/x','https://consent.google.com/m?continue=abc','https://example.org/']:
            with self.subTest(url=url):
                self.assertFalse(safe_article_url(url))

class LinkDeliveryTests(unittest.TestCase):
    def setUp(self):
        import test_delivery
        test_delivery.DeliveryTests.setUp(self)

    def test_resolved_link_and_original_alias_are_persisted(self):
        import bot,os
        item=dict(self.item,url='https://news.google.com/rss/articles/CBMi_example')
        with patch.dict(os.environ,self.env,clear=True),patch.object(bot,'collect_news',return_value=[item]), \
             patch.object(bot,'publisher_url',return_value='https://publisher.test/story') as resolve,patch.object(bot,'send_message') as send:
            self.assertEqual(bot.main(),1)
            self.assertEqual(send.call_args.args[2][0]['url'],'https://publisher.test/story')
            self.assertEqual(bot.main(),0)
            self.assertEqual(resolve.call_count,1)
            changed=dict(item,title='Совсем другая формулировка',url='https://news.google.com/rss/articles/CBMi_other')
            with patch.object(bot,'collect_news',return_value=[changed]):
                self.assertEqual(bot.main(),0) # same decoded URL is already delivered

    def test_decode_failure_does_not_mark_story_delivered(self):
        import bot,os
        item=dict(self.item,url='https://news.google.com/rss/articles/CBMi_example')
        with patch.dict(os.environ,self.env,clear=True),patch.object(bot,'collect_news',return_value=[item]), \
             patch.object(bot,'publisher_url',return_value=None),patch.object(bot,'send_message') as send:
            with self.assertRaises(SystemExit):
                bot.main()
            send.assert_not_called()
        with patch.dict(os.environ,self.env,clear=True),patch.object(bot,'collect_news',return_value=[item]), \
             patch.object(bot,'publisher_url',return_value='https://publisher.test/story'),patch.object(bot,'send_message') as send:
            self.assertEqual(bot.main(),1)
            self.assertEqual(send.call_count,1)
