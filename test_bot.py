import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from bot import rank_items, format_digest, resolve_article_url

class DigestTests(unittest.TestCase):
    def test_prioritizes_events_and_deduplicates(self):
        now=datetime(2026,11,9,tzinfo=timezone.utc)
        entries=[{'title':'ЦФА: новые правила','url':'https://a.test','date':now,'event':False,'source':'A'}, {'title':'Форум блокчейн в Москве','url':'https://b.test','date':now,'event':True,'source':'B'}, {'title':'ЦФА: новые правила','url':'https://a.test','date':now,'event':False,'source':'A'}]
        ranked=rank_items(entries)
        self.assertEqual(len(ranked),2)
        self.assertTrue(ranked[0]['event'])

    def test_article_url_keeps_publisher_link(self):
        self.assertEqual(resolve_article_url('https://example.org/story'), 'https://example.org/story')

    @patch('bot.gnewsdecoder')
    def test_google_news_link_decoded(self, decoder):
        decoder.return_value = {'status': True, 'decoded_url': 'https://publisher.example/article'}
        self.assertEqual(resolve_article_url('https://news.google.com/rss/articles/abc'), 'https://publisher.example/article')

    @patch('bot.gnewsdecoder')
    def test_unresolved_google_news_link_is_not_sent(self, decoder):
        decoder.return_value = {'status': False}
        self.assertIsNone(resolve_article_url('https://news.google.com/rss/articles/abc'))

    def test_message_escapes_untrusted_title(self):
        item={'title':'Крипто <test> & обзор','url':'https://example.org','date':datetime.now(timezone.utc),'event':False,'source':'Test'}
        msg=format_digest([item])
        self.assertIn('&lt;test&gt; &amp;',msg)
        self.assertNotIn('Крипто <test>',msg)

if __name__=='__main__': unittest.main()
