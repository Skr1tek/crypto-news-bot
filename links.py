"""Resolve Google News wrappers to publisher articles; never fetch publisher URLs."""
import html
import ipaddress
import json
import re
from urllib.parse import urlsplit, urljoin
import requests


def safe_article_url(url):
    try:
        p = urlsplit(url)
        host = (p.hostname or '').lower()
        if p.scheme not in ('http','https') or not host or p.username or p.password or len(url)>1500:
            return False
        if (host == 'google.com' or host.endswith('.google.com')) or '.' not in host or host.endswith(('.local','.internal','.localhost')):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            pass
        return bool(p.path.strip('/') or p.query)
    except (ValueError,TypeError):
        return False


def publisher_url(url):
    if safe_article_url(url):
        return url
    try:
        p=urlsplit(url)
        match=re.fullmatch(r'/(?:rss/)?(?:articles|read)/([A-Za-z0-9_-]{1,2048})',p.path)
        if p.scheme!='https' or p.hostname!='news.google.com' or p.username or p.password or not match:
            return None
        article=match[1]
        page='https://news.google.com/rss/articles/'+article+'?hl=ru&gl=RU&ceid=RU:ru'
        for _ in range(4):
            response=requests.get(page,timeout=15,allow_redirects=False,headers={'User-Agent':'Mozilla/5.0'},cookies={'CONSENT':'YES+','SOCS':'CAI'})
            response.raise_for_status()
            if response.status_code not in (301,302,303,307,308):
                break
            destination=urljoin(page,response.headers.get('Location',''))
            if safe_article_url(destination):
                return destination
            parts=urlsplit(destination)
            if parts.scheme!='https' or parts.hostname!='news.google.com' or parts.username or parts.password:
                return None
            page=destination
        else:
            return None
        attrs=dict(re.findall(r'data-n-a-(sg|ts)=["\']([^"\']+)["\']',response.text))
        if not attrs.get('ts','').isdigit() or not attrs.get('sg'):
            return None
        context=[['X','X',['X','X'],None,None,1,1,'US:en',None,1,None,None,None,None,None,0,1],
                 'X','X',1,[1,1,1],1,1,None,0,0,None,0]
        request=['garturlreq',context,article,int(attrs['ts']),html.unescape(attrs['sg'])]
        response=requests.post('https://news.google.com/_/DotsSplashUi/data/batchexecute',
            data={'f.req':json.dumps([[['Fbv4je',json.dumps(request),None,'generic']]])},
            timeout=15,allow_redirects=False)
        response.raise_for_status()
        for line in response.text.splitlines():
            if not line.startswith('[['):
                continue
            for row in json.loads(line):
                if len(row)>2 and row[1]=='Fbv4je' and isinstance(row[2],str):
                    result=json.loads(row[2])
                    if isinstance(result,list) and len(result)>1 and safe_article_url(result[1]):
                        return result[1]
    except (requests.RequestException,ValueError,TypeError,IndexError,KeyError):
        return None
    return None
