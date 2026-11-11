import unittest
from datetime import datetime, timezone
from unittest.mock import Mock
import test_delivery
import bot

TITLES = [
    'Нацбанк стал эмитентом цифровых финансовых активов и разместил токенизированные краткосрочные ноты на KASE - Profit.kz',
    'Нацбанк впервые разместил токенизированные краткосрочные ноты на KASE - https://the-tech.kz/',
    'KASE провела первые торги краткосрочными нотами Нацбанка РК в цифровой форме - Inbusiness.kz',
]

class StoryTests(unittest.TestCase):
    def item(self, title, n):
        return dict(title=title, url=f'https://example.org/{n}', date=datetime.now(timezone.utc), event=False)

    def test_user_example_is_one_story(self):
        items = [self.item(t, n) for n, t in enumerate(TITLES)]
        self.assertEqual(len(bot.rank_items(items)), 1)

    def test_distinct_policy_stories_are_kept(self):
        titles = [TITLES[0],
                  'Проверка инвесторов в ноябре: ЦБ готовит тесты для покупки криптовалюты - Блокчейн24',
                  'Банк России обсудит начисление процентов на счета в цифровых рублях - prospect.com.ru']
        self.assertEqual(len(bot.rank_items([self.item(t,n) for n,t in enumerate(titles)])), 3)

    def test_different_amounts_not_merged(self):
        titles = ['Банк разместил цифровые облигации на 100 миллионов рублей',
                  'Банк разместил цифровые облигации на 200 миллионов рублей']
        self.assertEqual(len(bot.rank_items([self.item(t,n) for n,t in enumerate(titles)])), 2)

    def test_overlapping_rubrics_are_preserved(self):
        items = [dict(self.item(t,n), categories=[str(n)]) for n,t in enumerate(TITLES)]
        ranked = bot.rank_items(items)
        self.assertEqual(set(ranked[0]['categories']), {'0', '1', '2'})

class StoryDeliveryTests(unittest.TestCase):
    setUp = test_delivery.DeliveryTests.setUp
    run_bot = test_delivery.DeliveryTests.run_bot
    def test_rephrased_story_not_repeated_next_run(self):
        post = Mock(return_value=Mock(json=lambda: {'ok': True}))
        self.run_bot([dict(self.item, title=TITLES[0])], post)
        self.run_bot([dict(self.item, title=TITLES[2], url='https://another.org/story')], post)
        self.assertEqual(post.call_count, 1)

    def test_failed_story_can_be_retried(self):
        bad = Mock(return_value=Mock(json=lambda: {'ok': False}))
        with self.assertRaises(SystemExit):
            self.run_bot([dict(self.item, title=TITLES[0])], bad)
        good = Mock(return_value=Mock(json=lambda: {'ok': True}))
        self.run_bot([dict(self.item, title=TITLES[2], url='https://another.org/story')], good)
        self.assertEqual(good.call_count, 1)
