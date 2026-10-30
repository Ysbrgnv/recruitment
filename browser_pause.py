"""Signal the owner browser thread; HTTP handlers never manipulate Playwright."""
import time
from app_state import database,_read,_save,get_task,update_task
from errors import UserFacingError

def request_action(task_id,action):
    if action not in {'open','continue','cancel'}:return False
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        task=_read(con.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())
        if not task or task['status']!='waiting_browser' or task.get('browser_action'):return False
        task['browser_action']=action;_save(con,task)
    return True

def consume_action(task_id):
    import os,json
    if os.getenv('TASK_QUEUE_BACKEND','local')=='postgres':
        with database() as con:
            row=con.execute('SELECT data FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not row:return 'cancel'
            if not json.loads(row['data']).get('browser_action'):return None
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        task=_read(con.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())
        if not task:return 'cancel'
        action=task.get('browser_action')
        if action:task['browser_action']=None;_save(con,task)
        return action

def wait_for_human(page,task_id,check,timeout=1800,ready=None):
    import browser_screen
    import os
    if os.getenv('TASK_QUEUE_BACKEND','local')=='postgres':
        timeout=max(60,min(3600,int(os.getenv('CAPTCHA_WAIT_SECONDS','1800'))))
        update_task(task_id,browser_deadline=time.time()+timeout)
    started=time.monotonic()
    token=browser_screen.start(task_id)
    try:
        return _wait_for_human(page,task_id,check,timeout,ready,token)
    finally:
        browser_screen.stop(task_id,token)
        if os.getenv('TASK_QUEUE_BACKEND','local')=='postgres':
            from telemetry import observe_captcha
            observe_captcha(time.monotonic()-started)

def _wait_for_human(page,task_id,check,timeout=1800,ready=None,screen_token=None):
    from google_page import page_state
    if not task_id:raise UserFacingError('Для ручной проверки требуется идентификатор задачи.')
    update_task(task_id,status='waiting_browser',stage='browser_verification',browser_action=None,message='Пройдите проверку самостоятельно в открытом окне браузера, затем нажмите «Продолжить поиск».')
    page.bring_to_front();deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if page.is_closed():raise UserFacingError('Окно поиска закрыто. Ссылки сохранены; возобновите задачу.')
        import browser_screen
        browser_screen.pump(page,task_id,screen_token)
        action=consume_action(task_id)
        if action=='cancel':raise UserFacingError('Поиск остановлен пользователем. Ссылки сохранены.')
        if action=='open':
            page.bring_to_front()
        if action=='continue':
            # Ignore the old HTTP status; inspect the current page after human navigation.
            if check(page):
                update_task(task_id,message='Проверка всё ещё отображается. Завершите её вручную и повторно нажмите «Продолжить поиск».')
            elif (ready(page) if ready else page_state(page) in {'results','empty'}):
                update_task(task_id,status='running',stage='search',message='Выдача доступна. Поиск продолжается.',browser_action=None)
                return
            else:
                update_task(task_id,message='Выдача пока недоступна. Откройте результаты поиска в этом же окне и нажмите «Продолжить поиск».')
        page.wait_for_timeout(250 if __import__('os').getenv('TASK_QUEUE_BACKEND','local')=='postgres' else 50)
    raise UserFacingError('Срок ручной проверки истёк. Ссылки сохранены; возобновите задачу.')

