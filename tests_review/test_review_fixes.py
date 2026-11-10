"""Dependency-isolated regression tests; no network or live PostgreSQL."""
import ast,sys,types,tempfile,unittest,threading,logging,re,os
from pathlib import Path
from unittest.mock import patch,Mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import app.core as core_package
from app.core import settings as real_settings
TEMP=tempfile.TemporaryDirectory()
os.environ['RECRUITMENT_DB_PATH']=str(Path(TEMP.name)/'test.db')
os.environ.pop('DATABASE_URL',None)
os.environ['TASK_QUEUE_BACKEND']='local'
settings=types.ModuleType('app.core.settings')
settings.BASE_DIR=Path(TEMP.name);settings.MAX_BACKGROUND_WORKERS=1
settings.TASK_RETENTION_HOURS=24;settings.RESERVATION_STALE_MINUTES=30
sys.modules['app.core.settings']=settings
core_package.settings=settings
from app.persistence import app_state
from app.persistence import operation_store as ops
from app.persistence import tracking_service
from app.browser import browser_screen
app_state.init_db();ops.init()
sys.modules['app.core.settings']=real_settings
core_package.settings=real_settings
def isolated_functions(name,names):
    tree=ast.parse((ROOT/name).read_text())
    nodes=[]
    for node in tree.body:
        if isinstance(node,ast.FunctionDef) and node.name in names:nodes.append(node)
        if isinstance(node,ast.ClassDef):
            nodes.extend(n for n in node.body if isinstance(n,ast.FunctionDef) and n.name in names)
    from app.core.errors import UserFacingError
    env={'UserFacingError':UserFacingError,'re':re,'STATE_LOCK':threading.RLock(),'APP_STATE':{'vacancies':['old'],'vacancy_cache':{},'startup_error':None},'EMPLOYER_ID':'1','logger':logging.getLogger('test'),'DEFAULT_EVALUATION_COUNT':1}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),name,'exec'),env)
    return env

class FixTests(unittest.TestCase):
    def setUp(self):
        with app_state.database() as con:
            con.execute('DELETE FROM operations');con.execute('DELETE FROM history')
    def reserve(self):return ops.reserve_and_begin_ai('t','r','v','vacancy','https://example.com','test@example.com')
    def test_atomic_success(self):
        oid=self.reserve();self.assertTrue(oid)
        self.assertEqual(ops.get_op('t','ai','r')['state'],'sending')
        self.assertTrue(tracking_service.was_sent_to_ai('r','v'))
    def test_atomic_rollback(self):
        with patch.object(ops,'begin',side_effect=RuntimeError('injected crash')):
            with self.assertRaises(RuntimeError):self.reserve()
        self.assertFalse(tracking_service.was_sent_to_ai('r','v'))
        self.assertIsNone(ops.get_op('t','ai','r'))
    def test_duplicate_blocked(self):
        self.reserve();self.assertIsNone(self.reserve())
    def test_operation_conflict_rolls_back_reservation(self):
        ops.begin('t','ai','r')
        with self.assertRaises(RuntimeError):self.reserve()
        self.assertFalse(tracking_service.was_sent_to_ai('r','v'))
    def test_count_rejected(self):
        env=isolated_functions('app/web/web_server.py',{'_post_validated'})
        for value in [True,False,1.9,1.0,'2',None,0,11,[],{}]:
            with self.subTest(value=value):
                handler=Mock();handler.read_json.return_value={'requested_count':value}
                env['_post_validated'](handler,'/api/vacancy/123/find-resumes')
                self.assertEqual(handler.send_json.call_args.args[1],400)
    def test_valid_count_reaches_email_validation(self):
        env=isolated_functions('app/web/web_server.py',{'_post_validated'});env['valid_email']=Mock(return_value=False)
        for value in [1,10]:
            handler=Mock();handler.read_json.return_value={'requested_count':value}
            env['_post_validated'](handler,'/api/vacancy/123/find-resumes')
            self.assertIn('почту',handler.send_json.call_args.args[0]['error'])
    def test_refresh_failure_preserves_cache(self):
        env=isolated_functions('app/web/web_server.py',{'load_vacancies'})
        env['fetch_active_vacancies']=Mock(side_effect=RuntimeError('HH unavailable'))
        self.assertFalse(env['load_vacancies']());self.assertEqual(env['APP_STATE']['vacancies'],['old'])
    def test_refresh_success(self):
        env=isolated_functions('app/web/web_server.py',{'load_vacancies'});env['fetch_active_vacancies']=Mock(return_value=['new'])
        self.assertTrue(env['load_vacancies']());self.assertEqual(env['APP_STATE']['vacancies'],['new'])
    def test_refresh_http_failure(self):
        env=isolated_functions('app/web/web_server.py',{'_post_validated'});env['load_vacancies']=lambda:False
        handler=Mock();env['_post_validated'](handler,'/api/refresh')
        self.assertEqual(handler.send_json.call_args.args[1],502)
    def test_wheel_validation(self):
        token=browser_screen.start('screen')
        base={'token':token,'kind':'wheel','x':0.5,'y':0.5,'delta_x':0,'delta_y':120}
        self.assertTrue(browser_screen.enqueue('screen',base))
        for field,value in [('delta_y',float('nan')),('delta_x',float('inf')),('delta_y',2001),('delta_y',True),('x',-1),('y',2)]:
            with self.subTest(field=field,value=value):
                with self.assertRaises(ValueError):browser_screen.enqueue('screen',{**base,field:value})
        self.assertFalse(browser_screen.enqueue('screen',{**base,'token':'wrong'}))
        browser_screen.stop('screen',token)
    def test_wheel_pump(self):
        token=browser_screen.start('screen');page=Mock()
        page.evaluate.return_value={'width':1000,'height':800}
        browser_screen.enqueue('screen',{'token':token,'kind':'wheel','x':0.5,'y':0.25,'delta_x':0,'delta_y':120})
        browser_screen.pump(page,'screen',token)
        page.mouse.move.assert_called_once_with(500,200);page.mouse.wheel.assert_called_once_with(0,120)
        browser_screen.stop('screen',token)
    def test_postgres_executescript_keeps_drop_index_in_order(self):
        from app.persistence.pg_backend import Connection
        raw=Mock()
        Connection(raw).executescript('''
            CREATE TABLE first_table(id INTEGER PRIMARY KEY, value REAL);
            DROP INDEX IF EXISTS one_active_task;
            CREATE TABLE second_table(id INTEGER PRIMARY KEY);
        ''')
        sql=[call.args[0].strip() for call in raw.execute.call_args_list]
        self.assertEqual(sql,[
            'SELECT pg_advisory_xact_lock(714202602)',
            'CREATE TABLE first_table(id BIGSERIAL PRIMARY KEY, value DOUBLE PRECISION)',
            'DROP INDEX IF EXISTS one_active_task',
            'CREATE TABLE second_table(id BIGSERIAL PRIMARY KEY)',
        ])

    def test_postgres_wheel_contract(self):
        env=isolated_functions('app/browser/shared_screen.py',{'enqueue'})
        from contextlib import contextmanager
        row={'token':'token','commands':'[]'}
        con=Mock();con.execute.return_value.fetchone.return_value=row
        @contextmanager
        def connect():yield con
        import secrets,json
        env.update(connect=connect,secrets=secrets,json=json)
        data={'token':'token','kind':'wheel','x':0.5,'y':0.5,'delta_x':0,'delta_y':100}
        self.assertTrue(env['enqueue']('t',data))
        with self.assertRaises(ValueError):env['enqueue']('t',{**data,'delta_y':float('nan')})
if __name__=='__main__':unittest.main()

