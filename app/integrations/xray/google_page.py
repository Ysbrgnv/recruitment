"""Google HTML adapter; selectors are best-effort, unknown markup is an error."""
import re
from urllib.parse import urlparse,parse_qs,urljoin,unquote
from html import unescape
RESULT_SELECTOR='#search a[href], #rso a[href], main a[href], [role="main"] a[href]'
PROFILE_SELECTOR='a[href*="linkedin.com/in/"], a[href*="hh.kz/resume/"], a[href*="career.habr.com/"], a[href^="/url?"], a[href*="google.com/goto?"], a[href*="google.kz/goto?"], a[href^="/goto?"], a[href*="share.google/"]' 

def result_links(page):
    return page.locator(RESULT_SELECTOR+', '+PROFILE_SELECTOR)

def page_state(page):
    text=page.locator('body').inner_text(timeout=10000)
    patterns=[r'did not match any documents',r'no results found for',r'no results containing all your search terms',r'по вашему запросу ничего не найдено',r'не найдено ни одного документа',r'ничего не найдено',r'ештеңе табылмады']
    if any(re.search(p,text,re.I) for p in patterns):return 'empty'
    return 'results' if result_links(page).count() else 'unknown'

def is_google_host(host):
    return (host or '').lower() in {'google.com','www.google.com','google.kz','www.google.kz','google.co.uk','www.google.co.uk','google.de','www.google.de','google.ru','www.google.ru','google.uz','www.google.uz'}

def unwrap_google_link(value):
    """Decode only recognized redirect wrappers; never treat search q as a URL."""
    if not isinstance(value,str):return ''
    current=unescape(value).strip()
    visited=set()
    for _ in range(6):
        if current in visited:return ''
        visited.add(current)
        if re.match(r'^https?%(?:25)*3a',current,re.I):
            current=unquote(current);continue
        if current.startswith('//'):current='https:'+current
        try:
            p=urlparse(current)
            if p.username or p.password:return ''
            host=(p.hostname or '').lower()
            if p.scheme and p.scheme not in {'http','https'}:return ''
            if p.path.rstrip('/') in {'/url','/local_url','/interstitial','/goto'} and (not host or is_google_host(host)):
                params=parse_qs(p.query)
                candidates=[v for key in ('url','q','target') for v in params.get(key,[])]
                target=next((v for v in candidates if re.match(r'^(?:https?://|https?%(?:25)*3a|//|/url\?)',v,re.I)),None)
                if target is None:return current if p.path.rstrip('/')=='/goto' else ''
                current=unescape(target).strip();continue
            return current
        except (ValueError,TypeError):return ''
    return ''

def next_page_url(page,current_query):
    for href in page.locator('a#pnnext, a[aria-label="Next page"], a[aria-label="Следующая страница"], a[aria-label="Келесі бет"]').evaluate_all('(nodes)=>nodes.map(a=>a.href)'):
        url=urljoin(page.url,href);p=urlparse(url);params=parse_qs(p.query)
        if p.scheme=='https' and is_google_host(p.hostname) and p.path=='/search' and params.get('q')==[current_query] and params.get('start',[''])[0].isdigit():return url
    return None

