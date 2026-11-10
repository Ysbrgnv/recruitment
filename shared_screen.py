"""Cross-process CAPTCHA frames and bounded input. Only Playwright owner pumps input."""
import json,secrets,time,os
from app.persistence.durable_queue import connect

def init():
    if os.getenv('APP_SCHEMA_MANAGED')=='true':return
    with connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS browser_screens(
          task_id TEXT PRIMARY KEY,token TEXT NOT NULL,commands TEXT NOT NULL,
          frame BYTEA,seq INTEGER NOT NULL DEFAULT 0,until_at DOUBLE PRECISION NOT NULL DEFAULT 0,
          capture_at DOUBLE PRECISION NOT NULL DEFAULT 0,error TEXT,
          streams INTEGER NOT NULL DEFAULT 0,updated DOUBLE PRECISION NOT NULL)""")

def start(task_id):
    init();token=secrets.token_hex(24)
    with connect() as con:
        con.execute("""INSERT INTO browser_screens(task_id,token,commands,updated) VALUES(%s,%s,'[]',%s)
          ON CONFLICT(task_id) DO UPDATE SET token=EXCLUDED.token,commands='[]',frame=NULL,
          seq=0,until_at=0,capture_at=0,error=NULL,streams=0,updated=EXCLUDED.updated""",(task_id,token,time.time()))
    return token

def stop(task_id,token):
    with connect() as con:con.execute('DELETE FROM browser_screens WHERE task_id=%s AND token=%s',(task_id,token))

def stop_all(task_id):
    init()
    with connect() as con:con.execute('DELETE FROM browser_screens WHERE task_id=%s',(task_id,))

def frame(task_id):
    with connect() as con:
        row=con.execute('UPDATE browser_screens SET until_at=%s WHERE task_id=%s RETURNING token,frame,seq,error',(time.time()+3,task_id)).fetchone()
    return row

def enqueue(task_id,data):
    with connect() as con:
        row=con.execute('SELECT * FROM browser_screens WHERE task_id=%s FOR UPDATE',(task_id,)).fetchone()
        if not row or not isinstance(data.get('token'),str) or not secrets.compare_digest(row['token'],data['token']):return False
        commands=json.loads(row['commands'])
        if len(commands)>=32:raise ValueError('Очередь ввода занята')
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
        commands.append(dict(data))
        con.execute('UPDATE browser_screens SET commands=%s WHERE task_id=%s',(json.dumps(commands),task_id))
    return True

def pump(page,task_id,token):
    with connect() as con:
        row=con.execute('SELECT * FROM browser_screens WHERE task_id=%s AND token=%s',(task_id,token)).fetchone()
        if not row:return
        now=time.time()
        if not json.loads(row['commands']) and not (row['until_at']>now and now-row['capture_at']>=0.2) and now-row['updated']<5:return
        # Re-read under the row lock before draining commands, so concurrent input is never lost.
        row=con.execute('SELECT * FROM browser_screens WHERE task_id=%s AND token=%s FOR UPDATE',(task_id,token)).fetchone()
        if not row:return
        commands=json.loads(row['commands']);now=time.time()
        capture=row['until_at']>now and now-row['capture_at']>=0.2
        con.execute("UPDATE browser_screens SET commands='[]',capture_at=%s,updated=%s WHERE task_id=%s",(now if capture else row['capture_at'],now,task_id))
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
        with connect() as con:
            if image:con.execute('UPDATE browser_screens SET frame=%s,seq=seq+1,error=NULL WHERE task_id=%s AND token=%s',(image,task_id,token))
            else:con.execute('UPDATE browser_screens SET error=NULL WHERE task_id=%s AND token=%s',(task_id,token))
    except Exception:
        with connect() as con:con.execute('UPDATE browser_screens SET error=%s WHERE task_id=%s AND token=%s',('Не удалось обновить экран. Попробуйте ещё раз.',task_id,token))

def open_stream(task_id,token):
    if not isinstance(token,str):return False
    with connect() as con:
        # Global stream cap must be atomic across all web processes.
        con.execute('SELECT pg_advisory_xact_lock(714202603)')
        row=con.execute('SELECT * FROM browser_screens WHERE task_id=%s FOR UPDATE',(task_id,)).fetchone()
        if not row or not secrets.compare_digest(row['token'],token):return False
        count=con.execute('SELECT COALESCE(SUM(streams),0) AS n FROM browser_screens WHERE until_at>%s',(time.time(),)).fetchone()['n']
        streams=row['streams'] if row['until_at']>time.time() else 0
        if streams>=2 or count>=16:return False
        con.execute('UPDATE browser_screens SET streams=%s,until_at=%s WHERE task_id=%s',(streams+1,time.time()+3,task_id))
    return True

def close_stream(task_id,token):
    with connect() as con:con.execute('UPDATE browser_screens SET streams=GREATEST(0,streams-1) WHERE task_id=%s AND token=%s',(task_id,token))

def next_frame(task_id,token,after,timeout=1):
    deadline=time.monotonic()+timeout
    while True:
        with connect() as con:
            row=con.execute('UPDATE browser_screens SET until_at=%s WHERE task_id=%s AND token=%s RETURNING frame,seq',(time.time()+3,task_id,token)).fetchone()
        if not row:return None
        if row['frame'] and row['seq']!=after:return row
        if time.monotonic()>=deadline:return {'frame':None,'seq':after}
        time.sleep(0.1)

