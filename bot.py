"""Telegram digest of recent Russian crypto-regulation headlines (RSS, no AI API)."""
import calendar
from contextlib import closing
import hashlib
import json
import sqlite3
from pathlib import Path
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


# Conservative headline heuristic, not full semantic understanding.
STORY_STOP = set('и в во на с со по для из от к о об до за при как что это стал стала стали впервые первые провела форме'.split())
STORY_SUFFIX = ('иями', 'ами', 'ями', 'ого', 'ему', 'ому', 'ыми', 'ими', 'ий', 'ый', 'ой', 'ая', 'яя', 'ое', 'ее', 'ые', 'ие', 'ых', 'их', 'ую',
                'юю', 'ов', 'ев', 'ам', 'ям', 'ах', 'ях', 'ом', 'ем', 'а', 'я', 'ы', 'и', 'у', 'ю', 'е')


def story_tokens(title):
    # Google News appends publisher names; they must not distinguish stories.
    headline = title.rsplit(' - ', 1)[0].casefold().replace('ё', 'е')
    result = set()
    for word in re.findall(r'[a-zа-я]+|[0-9]+', headline):
        if word in STORY_STOP:
            continue
        if word.isdigit():
            result.add(word)
            continue
        if len(word) < 3:
            continue
        for suffix in STORY_SUFFIX:
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[:-len(suffix)]
                break
        result.add(word[:6])
    return result


def same_story(left, right):
    a, b = story_tokens(left), story_tokens(right)
    if not a or not b:
        return False
    # Numbers can distinguish separate placements, dates or amounts.
    if {x for x in a if x.isdigit()} != {x for x in b if x.isdigit()}:
        return False
    shared = len(a & b)
    exact = re.sub(r'\W+', '', left.rsplit(' - ', 1)[0].casefold()) == re.sub(r'\W+', '', right.rsplit(' - ', 1)[0].casefold())
    return (exact or (len(a) >= 3 and a == b) or (shared >= 4 and shared / min(len(a), len(b)) >= 0.5
                       and shared / len(a | b) >= 0.3))


def rank_items(items, limit=5):
    groups = []
    for item in sorted(items, key=lambda x: (x['event'], x['date']), reverse=True):
        for group in groups:
            if same_story(item['title'], group[0]['title']):
                group.append(item)
                break
        else:
            groups.append([item])
    ranked = []
    for group in groups:
        representative = dict(group[0])
        representative['_aliases'] = group
        if any('categories' in item for item in group):
            representative['categories'] = sorted({c for item in group for c in item.get('categories', [])})
        ranked.append(representative)
    return ranked[:limit]


def collect_news(config=None):
    config = load_config() if config is None else config
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    items = []
    queries = {}
    for category, settings in config['categories'].items():
        for query in settings['queries']:
            queries.setdefault(query, []).append(category)
    for query, categories in queries.items():
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
            if not title or not link or not stamp or len(link) > 1500:
                continue
            if urlsplit(link).scheme not in ('https', 'http'):
                continue
            date = datetime.fromtimestamp(calendar.timegm(stamp), tz=timezone.utc)
            matched = [name for name in categories
                       if any(word.casefold() in title.casefold()
                              for word in config['categories'][name]['keywords'])]
            if date < cutoff or not matched:
                continue
            items.append({'title': title, 'url': link, 'date': date,
                          'categories': matched,
                          'event': any(word in title.casefold() for word in EVENT_WORDS),
                          'source': entry.get('source', {}).get('title', 'Новостная публикация')})
    # Keep the full pool: category selection and history filtering precede the limit.
    return items


def format_digest(items):
    today = datetime.now(MSK).strftime('%d.%m.%Y')
    lines = [f'📰 <b>Криптообзор — {today}</b>', '']
    if not items:
        lines.append('За последние 48 часов подходящих публикаций не найдено.')
    for index, item in enumerate(items, 1):
        prefix = '📅 ' if item['event'] else ''
        lines.append(f'{index}. {prefix}<a href="{html.escape(item["url"], quote=True)}">{html.escape(item["title"][:350])}</a>')
        lines.append('')
    lines.append('Подборка заголовков Google News RSS. Это не проверенный аналитический обзор. Даты форумов и участников уточняйте на сайтах организаторов.')
    return '\n'.join(lines)


DEFAULT_CATEGORIES = {
    name: {'queries': [query], 'keywords': list(KEYWORDS)}
    for name, query in zip(('regulation', 'cryptobanks', 'cfa', 'events'), QUERIES)
}


def load_config():
    path = os.environ.get('NEWS_CONFIG_PATH', '').strip()
    config = json.loads(Path(path).read_text(encoding='utf-8')) if path else {}
    if not isinstance(config, dict):
        raise ValueError('Configuration must be an object')
    categories = config.get('categories', DEFAULT_CATEGORIES)
    if not isinstance(categories, dict) or not categories:
        raise ValueError('categories must be a nonempty object')
    for name, settings in categories.items():
        if not isinstance(name, str) or not name or not isinstance(settings, dict):
            raise ValueError('Invalid category')
        for field in ('queries', 'keywords'):
            values = settings.get(field)
            if not isinstance(values, list) or not values or not all(isinstance(v, str) and v.strip() for v in values):
                raise ValueError('Category queries and keywords must be nonempty string lists')
    raw_targets = os.environ.get('TELEGRAM_TARGETS', '').strip()
    targets = json.loads(raw_targets) if raw_targets else config.get('targets')
    if targets is None:
        targets = [{'chat_id': os.environ.get('TELEGRAM_CHAT_ID', '').strip()}]
    if not isinstance(targets, list) or not targets:
        raise ValueError('targets must be a nonempty list')
    normalized, seen = [], set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError('Invalid target')
        chat_id = target.get('chat_id')
        selected = target.get('categories', list(categories))
        if isinstance(chat_id, bool) or not isinstance(chat_id, (str, int)) or not str(chat_id).strip():
            raise ValueError('Each target needs chat_id')
        chat_id = str(chat_id).strip()
        if chat_id in seen:
            raise ValueError('Duplicate chat_id')
        if not isinstance(selected, list) or not selected or any(not isinstance(c, str) or c not in categories for c in selected):
            raise ValueError('Unknown or empty target categories')
        normalized.append({'chat_id': chat_id, 'categories': selected})
        seen.add(chat_id)
    return {'categories': categories, 'targets': normalized}


def item_keys(item):
    # Store both aliases: the same URL or normalized headline is already delivered.
    title = re.sub(r'\W+', '', item['title'].casefold())
    url = item['url'].split('#', 1)[0]
    return [hashlib.sha256(value.encode()).hexdigest() for value in ('title:' + title, 'url:' + url)]


def send_message(token, chat_id, items):
    try:
        response = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': format_digest(items), 'parse_mode': 'HTML',
                  'disable_web_page_preview': True}, timeout=30)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or not result.get('ok'):
            raise ValueError('Delivery not confirmed')
    except (requests.RequestException, ValueError):
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
        db.execute('CREATE TABLE IF NOT EXISTS stories (chat_id TEXT, title TEXT, published REAL, PRIMARY KEY (chat_id, title))')
        db.execute('DELETE FROM stories WHERE published < ?', ((datetime.now(timezone.utc) - timedelta(days=7)).timestamp(),))
        db.commit()
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
            recent = list(db.execute('SELECT title, published FROM stories WHERE chat_id=?', (chat_id,)))
            # Match recent confirmed stories before filling the five-story limit.
            fresh = []
            for item in rank_items(candidates, limit=None):
                aliases = item.get('_aliases', [item])
                keys = {key for alias in aliases for key in item_keys(alias)}
                seen_story = any(abs(item['date'].timestamp() - stamp) <= 72 * 3600
                                 and same_story(item['title'], title) for title, stamp in recent)
                if not known.intersection(keys) and not seen_story:
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
                    for alias in item.get('_aliases', [item]):
                        db.executemany('INSERT OR IGNORE INTO sent VALUES (?, ?)',
                                       [(chat_id, key) for key in item_keys(alias)])
                        db.execute('INSERT OR REPLACE INTO stories VALUES (?, ?, ?)',
                                   (chat_id, alias['title'], alias['date'].timestamp()))
                db.commit()
                print('Telegram digest delivered')
    if failed:
        raise SystemExit('Telegram delivery failed; check credentials, chat access and network') from None


if __name__ == '__main__':
    main()

