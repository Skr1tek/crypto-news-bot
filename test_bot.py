import unittest
from datetime import datetime, timezone
from bot import rank_items, format_digest

class DigestTests(unittest.TestCase):
    def test_prioritizes_events_and_deduplicates(self):
        now=datetime(2026,11,9,tzinfo=timezone.utc)
        entries=[{'title':'ЦФА: новые правила','url':'https://a.test','date':now,'event':False,'source':'A'}, {'title':'Форум блокчейн в Москве','url':'https://b.test','date':now,'event':True,'source':'B'}, {'title':'ЦФА: новые правила','url':'https://a.test','date':now,'event':False,'source':'A'}]
        ranked=rank_items(entries)
        self.assertEqual(len(ranked),2)
        self.assertTrue(ranked[0]['event'])

    def test_message_escapes_untrusted_title(self):
        item={'title':'Крипто <test> & обзор','url':'https://example.org','date':datetime.now(timezone.utc),'event':False,'source':'Test'}
        msg=format_digest([item])
        self.assertIn('&lt;test&gt; &amp;',msg)
        self.assertNotIn('Крипто <test>',msg)

if __name__=='__main__': unittest.main()
