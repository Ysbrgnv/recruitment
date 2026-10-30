"""Task-scoped manual screen bridge. Playwright stays on its owner thread."""
import secrets
import threading
import time
from collections import deque

_lock=threading.Lock()
_sessions={}
_changed=threading.Condition(_lock)

def start(task_id):
    token=secrets.token_hex(24)
    with _lock:
        _sessions[task_id]={'token':token,'commands':deque(),'frame':None,'seq':0,'until':0,'capture':0,'error':None}
        _changed.notify_all()
    return token

def stop(task_id,token):
    with _lock:
        if _sessions.get(task_id,{}).get('token')==token:
            _sessions.pop(task_id,None)
            _changed.notify_all()

def frame(task_id):
    with _lock:
        s=_sessions.get(task_id)
        if not s:return None
        s['until']=time.monotonic()+3
        return {k:s[k] for k in ('token','frame','seq','error')}

def enqueue(task_id,data):
    with _lock:
        s=_sessions.get(task_id)
        if not s or data.get('token')!=s['token']:return False
        if len(s['commands'])>=32:raise ValueError('Очередь ввода занята')
        kind=data.get('kind')
        if kind=='click':
            x,y=data.get('x'),data.get('y')
            if type(x) not in (int,float) or type(y) not in (int,float) or not 0<=x<=1 or not 0<=y<=1:raise ValueError('Неверные координаты')
        elif kind=='wheel':
            import math
            for field in ('delta_x','delta_y'):
                value=data.get(field)
                if type(value) not in (int,float) or not math.isfinite(value) or abs(value)>2000:
                    raise ValueError('Неверная величина прокрутки')
            for field in ('x','y'):
                value=data.get(field)
                if type(value) not in (int,float) or not 0<=value<=1:
                    raise ValueError('Неверные координаты')
        elif kind=='key':
            if data.get('key') not in {'Tab','Shift+Tab','Enter','Space','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Backspace'}:raise ValueError('Недопустимая клавиша')
        elif kind=='text':
            if not isinstance(data.get('text'),str) or len(data['text'])>256:raise ValueError('Неверный текст')
        else:raise ValueError('Неверная команда')
        s['commands'].append(dict(data));return True

def pump(page,task_id,token):
    with _lock:
        s=_sessions.get(task_id)
        if not s or s['token']!=token:return
        commands=list(s['commands']);s['commands'].clear()
        capture=s['until']>time.monotonic() and time.monotonic()-s['capture']>=0.2
        if capture:s['capture']=time.monotonic()
    try:
        for c in commands:
            if c['kind']=='click':
                size=page.evaluate('() => ({width:innerWidth,height:innerHeight})')
                page.mouse.click(c['x']*size['width'],c['y']*size['height'])
            elif c['kind']=='wheel':
                size=page.evaluate('() => ({width:innerWidth,height:innerHeight})')
                page.mouse.move(c['x']*size['width'],c['y']*size['height'])
                page.mouse.wheel(c['delta_x'],c['delta_y'])
            elif c['kind']=='key':page.keyboard.press(c['key'])
            else:page.keyboard.insert_text(c['text'])
        image=page.screenshot(type='jpeg',quality=55,full_page=False,timeout=700,animations='allow') if capture else None
        with _lock:
            if _sessions.get(task_id,{}).get('token')==token:
                if image:s['frame']=image;s['seq']+=1
                s['error']=None
                _changed.notify_all()
    except Exception:
        with _lock:
            if _sessions.get(task_id,{}).get('token')==token:s['error']='Не удалось обновить экран. Попробуйте ещё раз.'


def open_stream(task_id, token):
    """Bound connections; never expose another pause's frame."""
    with _lock:
        s = _sessions.get(task_id)
        if not s or not isinstance(token, str) or not secrets.compare_digest(s['token'], token):
            return False
        if s.get('streams', 0) >= 2 or sum(v.get('streams', 0) for v in _sessions.values()) >= 16:
            return False
        s['streams'] = s.get('streams', 0) + 1
        s['until'] = time.monotonic() + 3
        return True


def close_stream(task_id, token):
    with _lock:
        s = _sessions.get(task_id)
        if s and s['token'] == token:
            s['streams'] = max(0, s.get('streams', 0) - 1)


def next_frame(task_id, token, after, timeout=1):
    deadline = time.monotonic() + timeout
    with _changed:
        while True:
            s = _sessions.get(task_id)
            if not s or s['token'] != token:
                return None
            s['until'] = time.monotonic() + 3
            if s['frame'] and s['seq'] != after:
                return {'frame': s['frame'], 'seq': s['seq']}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {'frame': None, 'seq': after}
            _changed.wait(remaining)

# In server mode all web/worker processes use the same task-scoped screen store.
import os as _os
if _os.getenv('TASK_QUEUE_BACKEND','local') == 'postgres':
    from shared_screen import start,stop,frame,enqueue,pump,open_stream,close_stream,next_frame

