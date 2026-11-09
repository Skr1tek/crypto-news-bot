"""Telegram digest of recent Russian crypto-regulation headlines (RSS, no AI API)."""
import calendar
import html
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import feedparser
import requests

MSK = timezone(timedelta(hours=3))
QUERIES = (
    'криптовалюта регулирование Банк России закон',
    'криптобанк Россия цифровая валюта',
    'цифровые финансовые активы цифровой депозитарий',
    'форум конференция блокчейн криптовалюта Россия спикеры',
)
KEYWORDS = ('крипт', 'цифров', 'блокчейн', 'токен', 'майнинг', 'стейблкоин', 'цфа')
EVENT_WORDS = ('форум', 'конференц', 'саммит', 'встреч', 'мероприят', 'спикер')


def rank_items(items):
    unique = {}
    for item in items:
        key = re.sub(r'\W+', '', item['title'].casefold())[:120]
        if key not in unique or item['date'] > unique[key]['date']:
            unique[key] = item
    return sorted(unique.values(), key=lambda item: (item['event'], item['date']), reverse=True)[:5]


def collect_news():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    items = []
    for query in QUERIES:
        url = ('https://news.google.com/rss/search?q=' + quote(query + ' when:2d')
               + '&hl=ru&gl=RU&ceid=RU:ru')
        try:
            response = requests.get(url, timeout=20, headers={'User-Agent': 'Mozilla/5.0 CryptoDigest/1.0'})
            response.raise_for_status()
            entries = feedparser.parse(response.content).entries
        except (requests.RequestException, ValueError) as exc:
            print('Feed unavailable:', type(exc).__name__)
            continue
        for entry in entries:
            title = entry.get('title', '').strip()
            link = entry.get('link', '')
            stamp = entry.get('published_parsed')
            if not title or not link or not stamp:
                continue
            if urlsplit(link).scheme not in ('https', 'http'):
                continue
            date = datetime.fromtimestamp(calendar.timegm(stamp), tz=timezone.utc)
            if date < cutoff or not any(word in title.casefold() for word in KEYWORDS):
                continue
            items.append({'title': title, 'url': link, 'date': date,
                          'event': any(word in title.casefold() for word in EVENT_WORDS),
                          'source': entry.get('source', {}).get('title', 'Новостная публикация')})
    return rank_items(items)


def format_digest(items):
    today = datetime.now(MSK).strftime('%d.%m.%Y')
    lines = [f'📰 <b>Криптообзор — {today}</b>', '']
    if not items:
        lines.append('За последние 48 часов подходящих публикаций не найдено.')
    for index, item in enumerate(items, 1):
        prefix = '📅 ' if item['event'] else ''
        lines.append(f'{index}. {prefix}<a href="{html.escape(item["url"], quote=True)}">{html.escape(item["title"])}</a>')
        lines.append('')
    lines.append('Подборка заголовков Google News RSS. Это не проверенный аналитический обзор. Даты форумов и участников уточняйте на сайтах организаторов.')
    return '\n'.join(lines)


def main():
    token = os.environ['TELEGRAM_BOT_TOKEN']
    chat_id = os.environ['TELEGRAM_CHAT_ID']
    message = format_digest(collect_news())
    response = requests.post(
        f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML',
              'disable_web_page_preview': True}, timeout=30)
    response.raise_for_status()
    if not response.json().get('ok'):
        raise RuntimeError('Telegram API did not confirm delivery')
    print('Telegram digest delivered')


if __name__ == '__main__':
    main()
