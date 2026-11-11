"""Visible browser search, no search API/key/account, no protection bypass."""
import re
import logging
import json
from urllib.parse import urlparse,urlunparse,urljoin,parse_qs
from app.core.errors import UserFacingError
from playwright.sync_api import sync_playwright,Error as BrowserError,TimeoutError as BrowserTimeout
import threading
import os
from app.browser.browser_pause import wait_for_human
SEARCH_LOCK=threading.BoundedSemaphore(8)
logger=logging.getLogger(__name__)

def canonical_profile(value):
    try:
        p=urlparse(value)
        host=(p.hostname or '').lower()
        if host.endswith('.hh.kz'):host='hh.kz'
        if p.scheme not in {'http','https'} or p.username or p.password or p.port not in {None,80,443}:return None
        if host in {'hh.kz','www.hh.kz','hh.ru','www.hh.ru','hh.uz','www.hh.uz','hh.kg','www.hh.kg'} and re.fullmatch(r'/resume/[A-Za-z0-9_-]+/?',p.path):
            return urlunparse(('https',host.removeprefix('www.'),p.path.rstrip('/'),'','',''))
        if (host=='linkedin.com' or host.endswith('.linkedin.com')) and re.fullmatch(r'/in/[A-Za-z0-9_%.-]+/?',p.path):
            return urlunparse(('https','www.linkedin.com',p.path.rstrip('/'),'','',''))
        if host in {'github.com','www.github.com'} and re.fullmatch(r'/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/?',p.path):
            username=p.path.strip('/')
            if username.lower() not in {'about','pricing','features','topics','collections','trending','explore','marketplace','login','signup','settings','orgs','enterprise','sponsors','security','contact','search','readme','site'}:
                return urlunparse(('https','github.com','/'+username.lower(),'','',''))
        if host=='career.habr.com' and re.fullmatch(r'/[A-Za-z0-9_.-]+/?',p.path):
            slug=p.path.strip('/')
            if slug.lower() not in {'vacancies','companies','salaries','courses','articles','users','auth','login','register','search','resumes','skills'}:
                return urlunparse(('https',host,'/'+slug,'','',''))
    except (TypeError,ValueError):pass
    return None


def extract_profile(value):
    from app.integrations.xray.google_page import unwrap_google_link
    return canonical_profile(unwrap_google_link(value))


def rejection_reason(href):
    from app.integrations.xray.google_page import unwrap_google_link,is_google_host
    target=unwrap_google_link(href)
    if not target:return 'invalid_or_unsupported_redirect'
    try:
        parsed=urlparse(target)
        if parsed.scheme not in {'http','https'} or not parsed.hostname:return 'invalid_url'
        if is_google_host(parsed.hostname):return 'google_navigation_or_unhandled_wrapper'
        host=parsed.hostname.lower()
        if host=='linkedin.com' or host.endswith('.linkedin.com') or host=='career.habr.com' or host=='hh.kz' or host.endswith('.hh.kz'):
            return 'unsupported_profile_path'
        return 'not_a_profile_host'
    except (ValueError,TypeError):return 'invalid_url'


class RejectedHrefLog:
    """First 20 rejected occurrences per task, including across resumed searches."""
    def __init__(self,task_id=None,previous=None,persist=None):
        self.task_id=task_id
        self.items=list(previous or [])[:20]
        self.persist=persist

    def add(self,href,reason):
        if len(self.items)>=20:return
        # Retain href structure, but do not persist known credential values.
        raw=str(href)
        raw=re.sub(r'(?i)([?&](?:access_token|refresh_token|token|api_key|password|secret|code)=)[^&#]*',r'\1[REDACTED]',raw)
        raw=re.sub(r'(?i)(https?://)[^/@\s]+@',r'\1[REDACTED]@',raw)
        item={'href':raw[:8192],'reason':reason}
        self.items.append(item)
        logger.warning('XRAY_REJECTED_HREF task=%s index=%d reason=%s href=%s',self.task_id,len(self.items),reason,json.dumps(item['href'],ensure_ascii=True))
        if self.persist:self.persist(list(self.items))

def browser_redirect_url(value):
    """Only explicitly supported Google wrappers are opened in a browser."""
    from app.integrations.xray.google_page import unwrap_google_link,is_google_host
    value=unwrap_google_link(value)
    if value.startswith('/'):value=urljoin('https://www.google.com',value)
    try:
        p=urlparse(value)
        if p.scheme!='https' or p.username or p.password or p.port not in {None,443}:return None
        if is_google_host(p.hostname) and p.path.rstrip('/')=='/goto' and parse_qs(p.query).get('url'):return value
        if p.hostname=='share.google' and re.fullmatch(r'/[A-Za-z0-9_-]+/?',p.path):return value
    except (ValueError,TypeError):pass
    return None


def resolve_browser_profile(context,href,task_id=None):
    """Resolve an opaque Google redirect, intercepting the profile navigation.

    No destination profile document is downloaded or inspected. No CAPTCHA is solved.
    """
    from app.integrations.xray.google_page import is_google_host
    start=browser_redirect_url(href)
    if not start:return None,'not_supported_browser_redirect'
    tab=context.new_page()
    captured=[];denied=[]
    def trusted(value):
        try:
            p=urlparse(value)
            return p.scheme=='https' and not p.username and not p.password and p.port in {None,443} and (is_google_host(p.hostname) or p.hostname in {'share.google','consent.google.com','consent.google.kz'})
        except (ValueError,TypeError):return False
    def reject(value,reason):
        # Log destination shape, not query tokens or credentials.
        try:
            p=urlparse(value)
            safe=urlunparse((p.scheme,p.hostname or '',p.path[:500],'','',''))
        except (ValueError,TypeError):safe='[invalid URL]'
        logger.warning('XRAY_REDIRECT_DESTINATION task=%s reason=%s destination=%s',task_id,reason,json.dumps(safe,ensure_ascii=True))
        denied.append(value)
    def guard(route):
        request=route.request
        if not request.is_navigation_request() or request.frame!=tab.main_frame:
            route.fallback();return
        value=request.url
        from app.integrations.xray.google_page import unwrap_google_link
        visited=set()
        for hop in range(8):
            profile=canonical_profile(unwrap_google_link(value))
            if profile:
                captured.append(profile);route.abort();return
            if not trusted(value):
                reject(value,'untrusted_or_unsupported_destination');route.abort();return
            if value in visited:
                reject(value,'redirect_loop');route.abort();return
            visited.add(value)
            # Browser routing alone does not inspect each HTTP redirect hop.
            # Fetch only this trusted hop; never automatically follow Location.
            try:
                response=route.fetch(url=value,max_redirects=0,timeout=15000)
                status=response.status
                if status in {301,302,303,307,308}:
                    location=response.headers.get('location')
                    if not location:
                        reject(value,'redirect_missing_location');route.abort();return
                    value=urljoin(value,location)
                    continue
                route.fulfill(response=response)
                return
            except BrowserError:
                reject(value,'trusted_hop_fetch_failed');route.abort();return
        reject(value,'redirect_hop_limit');route.abort()
    tab.route('**/*',guard)
    try:
        response=None
        try:response=tab.goto(start,wait_until='domcontentloaded',timeout=20000)
        except BrowserError:
            if not captured and not denied and not is_blocked(tab):return None,'redirect_navigation_failed'
        if captured:return captured[0],None
        if denied:return None,'redirect_destination_not_profile'
        if is_blocked(tab,response):
            wait_for_human(tab,task_id,lambda p:not (captured or denied) and is_blocked(p),ready=lambda _:bool(captured or denied))
            if captured:return captured[0],None
            return None,'redirect_destination_not_profile'
        # Bounded wait for an ordinary delayed redirect, not a repeated search.
        for _ in range(20):
            if captured:return captured[0],None
            if denied:return None,'redirect_destination_not_profile'
            if is_blocked(tab):
                wait_for_human(tab,task_id,lambda p:not (captured or denied) and is_blocked(p),ready=lambda _:bool(captured or denied))
                return (captured[0],None) if captured else (None,'redirect_destination_not_profile')
            tab.wait_for_timeout(250)
        return None,'redirect_not_resolved'
    finally:
        if not tab.is_closed():tab.close()


def is_blocked(page,response=None):
    if response and response.status in {403,429}:return True
    from urllib.parse import urlparse
    current=urlparse(page.url)
    if current.path.startswith('/sorry/') or (current.hostname or '').startswith('consent.google.'):return True
    markers=page.locator('iframe[src*="recaptcha"], iframe[src*="captcha"], #captcha-form, #challenge-form, .g-recaptcha')
    if any(markers.nth(i).is_visible() for i in range(markers.count())):return True
    text=page.locator('body').inner_text(timeout=10000).lower()
    return any(x in text for x in ['unusual traffic from your computer network','verify you are human','наши системы обнаружили необычный трафик','подтвердите, что вы не робот','before you continue to google','прежде чем перейти к google'])

def launch_browser(playwright):
    endpoint=os.getenv('BROWSER_WS_ENDPOINT','').strip()
    if endpoint:
        try:return playwright.chromium.connect(endpoint,timeout=30000)
        except BrowserError as exc:
            raise UserFacingError('Браузерный сервис недоступен. Сохранённые ссылки не потеряны.') from exc
    # Use installed browsers only. Do not install/download anything at runtime.
    for channel in ('chrome','msedge'):
        try:return playwright.chromium.launch(channel=channel,headless=False)
        except BrowserError:continue
    raise UserFacingError('Не удалось открыть установленный Chrome или Edge. Проверьте наличие браузера и разрешение на автоматизацию.')

def search_profiles(query,task_id=None):
    if not isinstance(query,str) or not query.strip():raise UserFacingError('Пустой X-Ray запрос.')
    from app.persistence.app_state import get_task,update_task
    from app.integrations.xray.xray_plan import validate_plan,DEFAULT_SOURCES,source_for_url,parse_query
    from app.integrations.xray.google_page import result_links,page_state,next_page_url
    from urllib.parse import urlencode
    task=get_task(task_id) if task_id else None
    sources=task.get('xray_sources',DEFAULT_SOURCES) if task else DEFAULT_SOURCES
    try:queries=validate_plan(query,sources).splitlines()
    except ValueError as exc:raise UserFacingError(str(exc)+' Создайте новую X-Ray задачу с актуальными площадками.') from exc
    with SEARCH_LOCK, sync_playwright() as playwright:
        browser=launch_browser(playwright)
        try:
            context=browser.new_context(viewport={'width':1280,'height':720},accept_downloads=False);page=context.new_page();page.set_default_timeout(15000)
            seen=set();diagnostics=[];redirect_cache={}
            counts={'links_read':0,'profiles_recognized':0,'invalid_links':0,'unselected_links':0,'search_duplicates':0,'redirect_failures':0}
            rejected=RejectedHrefLog(task_id,(task or {}).get('xray_rejected_hrefs'),
                (lambda items:update_task(task_id,xray_rejected_hrefs=items)) if task_id else None)
            def save_counts():
                if task_id:update_task(task_id,xray_link_counts=dict(counts))
            def record(current,state):
                diagnostics.append({'query':current,'outcome':state})
                if task_id:update_task(task_id,xray_search_diagnostics=diagnostics)
            for combined in queries:
                attempts=[combined]
                while attempts:
                    current=attempts.pop(0);sites,tail=parse_query(current,sources);usable=0
                    if task_id:update_task(task_id,stage='search',message='Google X-Ray: '+current)
                    response=page.goto('https://www.google.com/search?'+urlencode({'q':current,'hl':'ru'}),wait_until='domcontentloaded',timeout=45000)
                    visited={page.url}
                    for number in range(3):
                        if is_blocked(page,response):
                            wait_for_human(page,task_id,is_blocked);response=None
                        state=page_state(page)
                        if state=='unknown':
                            try:result_links(page).first.wait_for(state='attached',timeout=10000)
                            except BrowserTimeout:pass
                            state=page_state(page)
                        if state=='empty':record(current,'no_results');break
                        if state=='unknown':
                            record(current,'unknown_markup')
                            raise UserFacingError('Не удалось распознать выдачу Google. Это не подтверждённое отсутствие результатов. Проверьте окно браузера; ссылки сохранены.')
                        for href in result_links(page).evaluate_all('(items)=>items.map(a=>a.href)'):
                            counts['links_read']+=1
                            url=extract_profile(href)
                            redirect_reason=None
                            if not url and browser_redirect_url(href):
                                key=browser_redirect_url(href)
                                if key not in redirect_cache:
                                    redirect_cache[key]=resolve_browser_profile(context,href,task_id)
                                url,redirect_reason=redirect_cache[key]
                            if not url:
                                if redirect_reason and redirect_reason != 'redirect_destination_not_profile':counts['redirect_failures']+=1
                                counts['invalid_links']+=1;rejected.add(href,redirect_reason or rejection_reason(href));save_counts();continue
                            counts['profiles_recognized']+=1
                            if url and source_for_url(url) in sources and urlparse(url).hostname.removeprefix('www.') in {x.split('/')[0] for x in sites}:
                                usable+=1
                                if url not in seen:
                                    seen.add(url);save_counts();yield {'url':url}
                                else:
                                    counts['search_duplicates']+=1;rejected.add(href,'duplicate_in_search')
                            else:
                                counts['unselected_links']+=1;rejected.add(href,'source_not_selected')
                            save_counts()
                        if number==2:break
                        next_url=next_page_url(page,current)
                        if not next_url or next_url in visited:break
                        visited.add(next_url);page.wait_for_timeout(1500)
                        response=page.goto(next_url,wait_until='domcontentloaded',timeout=45000)
                    record(current,'profile_links_found' if usable else 'no_usable_profiles')
                    if attempts:page.wait_for_timeout(1500)
                page.wait_for_timeout(1500)
            if not seen and counts['redirect_failures']:
                raise UserFacingError('Не удалось распознать переходы Google: профили могли присутствовать в выдаче. Это не подтверждённое отсутствие кандидатов. Подробности: XRAY_REDIRECT_DESTINATION и XRAY_REJECTED_HREF в logs/*.jsonl. Ссылки и история сохранены; после проверки возобновите задачу.')
        except BrowserError as exc:
            raise UserFacingError('Ошибка браузерного Google X-Ray поиска. Сохранённые ссылки не потеряны; проверьте сеть и окно браузера.') from exc
        finally:browser.close()

