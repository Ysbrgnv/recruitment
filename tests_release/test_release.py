"""Offline regressions. Placeholder credentials only; no external service calls."""
import os,sys,tempfile,unittest,json,threading,time,importlib.util
from pathlib import Path
from unittest.mock import patch,Mock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
TEMP=tempfile.TemporaryDirectory()
os.environ.update(APP_ENV_FILE=str(Path(TEMP.name)/'source.env'),LOG_DIR=str(Path(TEMP.name)/'logs'),RECRUITMENT_DB_PATH=str(Path(TEMP.name)/'initial.db'),TASK_QUEUE_BACKEND='local')
os.environ.pop('DATABASE_URL',None);os.environ.pop('APP_SCHEMA_MANAGED',None)
import requests
import app_state as state,operation_store as ops,tracking_service as tracking
import nano_client as nano,hh_client as hh,workflow,config_sync
from errors import AIUnavailableError,HHUnavailableError
from dotenv import dotenv_values
spec=importlib.util.spec_from_file_location('prepare_local',ROOT/'docker/prepare_local.py');prepare=importlib.util.module_from_spec(spec);spec.loader.exec_module(prepare)

def response(code,payload):
    r=Mock();r.status_code=code;r.json.return_value=payload
    return r

class AITests(unittest.TestCase):
    def test_connection_error_unknown(self):
        with patch.object(nano,'NANO_GPT_API_KEY','placeholder'),patch.object(nano.requests,'post',side_effect=requests.ConnectionError('reset')) as post:
            with self.assertRaises(AIUnavailableError) as e:nano.ask('p','s')
            self.assertTrue(e.exception.request_sent);self.assertEqual(post.call_count,1)
    def test_connect_timeout_unsent(self):
        with patch.object(nano,'NANO_GPT_API_KEY','placeholder'),patch.object(nano.requests,'post',side_effect=requests.exceptions.ConnectTimeout()):
            with self.assertRaises(AIUnavailableError) as e:nano.ask('p','s')
            self.assertFalse(e.exception.request_sent)
    def test_read_timeout_unknown(self):
        with patch.object(nano,'NANO_GPT_API_KEY','placeholder'),patch.object(nano.requests,'post',side_effect=requests.exceptions.ReadTimeout()):
            with self.assertRaises(AIUnavailableError) as e:nano.ask('p','s')
            self.assertTrue(e.exception.request_sent)
    def test_no_redirect_replay(self):
        with patch.object(nano,'NANO_GPT_API_KEY','placeholder'),patch.object(nano.requests,'post',return_value=response(307,{})) as post:
            with self.assertRaises(AIUnavailableError):nano.ask('p','s')
            self.assertIs(post.call_args.kwargs['allow_redirects'],False);self.assertEqual(post.call_count,1)
    def test_missing_key_no_post(self):
        with patch.object(nano,'NANO_GPT_API_KEY',''),patch.object(nano.requests,'post') as post:
            with self.assertRaises(AIUnavailableError) as e:nano.ask('p','s')
            self.assertFalse(e.exception.request_sent);post.assert_not_called()
    def test_bad_payload_unknown(self):
        with patch.object(nano,'NANO_GPT_API_KEY','placeholder'),patch.object(nano.requests,'post',return_value=response(200,{'choices':[{'message':{'content':None}}]})):
            with self.assertRaises(AIUnavailableError) as e:nano.ask('p','s')
            self.assertTrue(e.exception.request_sent)

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.p=patch.object(state,'DB_PATH',Path(self.tmp.name)/'db');self.p.start();self.addCleanup(self.p.stop)
        state.init_db();ops.init()
        state.create_task('t','v',2,'test@example.com','hh')
        vacancy={'id':'v','name':'test'};pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[])]
        state.update_task('t',query='test',checkpoint={'vacancy':vacancy,'pool':pool,'pool_complete':True})
    def item(self,v,r,n,notes):return {'resume_id':r['id'],'number':n,'evaluation':{'structured':True,'human_review_required':True,'total':50,'level':'test','gaps':[]},'evaluation_text':'test'}
    def invalid_item(self,v,r,n,notes,status='invalid_response'):
        return {'resume_id':r['id'],'number':n,'evaluation':{'structured':False,'human_review_required':True,'status':status,'total':None,'level':'Требуется ручная проверка','gaps':[]},'evaluation_text':'invalid'}
    def set_requested(self,count,pool=None):
        task=state.get_task('t');checkpoint=task['checkpoint']
        if pool is not None:checkpoint={**checkpoint,'pool':pool}
        state.update_task('t',requested_count=count,checkpoint=checkpoint)
    def test_structured_evaluation_is_emailed(self):
        self.set_requested(1,pool=[('r1',{'id':'r1'},[])])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=self.item),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        mail.assert_called_once();self.assertEqual(result['metrics']['successful_responses'],1)
    def test_unstructured_evaluation_is_saved_not_emailed_and_search_continues(self):
        self.set_requested(1)
        calls=[]
        def build(v,r,n,notes):
            calls.append(r['id'])
            return self.invalid_item(v,r,n,notes) if r['id']=='r1' else self.item(v,r,n,notes)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(calls,['r1','r2']);mail.assert_called_once()
        self.assertEqual(ops.get_op('t','ai','r1')['state'],'succeeded')
        self.assertIsNone(ops.get_op('t','hh_email','r1'))
        self.assertEqual(result['metrics']['successful_responses'],1);self.assertEqual(result['metrics']['manual_review'],1)
        self.assertEqual([x['resume_id'] for x in result['manual_review']],['r1'])
    def test_unstructured_evaluation_does_not_fill_requested_count(self):
        self.set_requested(1,pool=[('r1',{'id':'r1'},[])])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=self.invalid_item),patch.object(workflow,'send_candidate_email') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        mail.assert_not_called();self.assertEqual(result['metrics']['successful_responses'],0)
        self.assertEqual(result['metrics']['shortfall'],1)
    def test_processing_continues_after_unstructured_evaluation(self):
        self.set_requested(1)
        calls=[]
        def build(v,r,n,notes):
            calls.append(r['id'])
            return self.invalid_item(v,r,n,notes) if r['id']=='r1' else self.item(v,r,n,notes)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            workflow.run_hh('t',lambda *a:None)
        self.assertEqual(calls,['r1','r2']);mail.assert_called_once()
    def test_malformed_ai_result_is_not_emailed(self):
        self.set_requested(1,pool=[('r1',{'id':'r1'},[])])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=lambda v,r,n,notes:self.invalid_item(v,r,n,notes,'invalid_response')),patch.object(workflow,'send_candidate_email') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        mail.assert_not_called();self.assertEqual(ops.get_op('t','ai','r1')['state'],'succeeded')
        self.assertEqual(result['metrics']['successful_responses'],0);self.assertEqual(result['metrics']['shortfall'],1)
    def test_human_review_required_does_not_block_email(self):
        self.set_requested(1,pool=[('r1',{'id':'r1'},[])])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=self.item),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            workflow.run_hh('t',lambda *a:None)
        mail.assert_called_once()
    def test_low_score_does_not_block_email(self):
        self.set_requested(1,pool=[('r1',{'id':'r1'},[])])
        def low(v,r,n,notes):
            item=self.item(v,r,n,notes);item['evaluation'].update(total=0,level='Ключевые требования преимущественно не подтверждены');return item
        with patch.object(workflow.service,'_build_candidate_result',side_effect=low),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            workflow.run_hh('t',lambda *a:None)
        mail.assert_called_once()
    def test_gaps_do_not_block_email(self):
        self.set_requested(1,pool=[('r1',{'id':'r1'},[])])
        def with_gaps(v,r,n,notes):
            item=self.item(v,r,n,notes);item['evaluation']['gaps']=['Не подтверждено'];return item
        with patch.object(workflow.service,'_build_candidate_result',side_effect=with_gaps),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            workflow.run_hh('t',lambda *a:None)
        mail.assert_called_once()
    def test_legacy_evaluation_without_structured_is_not_emailed(self):
        self.set_requested(1,pool=[])
        legacy={'resume_id':'legacy','number':1,'evaluation':{'total':50,'level':'legacy'},'evaluation_text':'legacy'}
        oid=ops.begin('t','ai','legacy');ops.finish(oid,'succeeded',legacy)
        with patch.object(workflow,'send_candidate_email') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        mail.assert_not_called();self.assertIsNone(ops.get_op('t','hh_email','legacy'))
        self.assertEqual(result['metrics']['successful_responses'],0);self.assertEqual(result['metrics']['shortfall'],1)
    def test_existing_succeeded_email_is_not_repeated(self):
        item=self.item(None,{'id':'r1'},1,[])
        oid=ops.begin('t','hh_email','r1',{'item':item});ops.finish(oid,'succeeded',{'status':'Sent'})
        with patch.object(workflow,'send_candidate_email') as mail:
            self.assertEqual(workflow._hh_mail(state.get_task('t'),{'name':'test'},item),'Sent')
        mail.assert_not_called()
    def test_existing_unknown_email_is_not_repeated(self):
        item=self.item(None,{'id':'r1'},1,[])
        oid=ops.begin('t','hh_email','r1',{'item':item});ops.finish(oid,'unknown',{'item':item})
        with patch.object(workflow,'send_candidate_email') as mail:
            with self.assertRaises(workflow.NeedsManual):workflow._hh_mail(state.get_task('t'),{'name':'test'},item)
        mail.assert_not_called();self.assertEqual(ops.get_op('t','hh_email','r1')['state'],'unknown')
    def test_same_task_resume_no_duplicate_ai_or_mail(self):
        calls=[]
        def build(v,r,n,notes):
            calls.append(r['id'])
            if r['id']=='r2' and calls.count('r2')==1:raise AIUnavailableError('connect timeout',False)
            return self.item(v,r,n,notes)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            with self.assertRaises(AIUnavailableError):workflow.run_hh('t',lambda *a:None)
            self.assertEqual(ops.get_op('t','ai','r2')['state'],'failed')
            with state.database() as c:self.assertEqual(ops.allow_confirmed_unsent_retries('t',c),1)
            result=workflow.run_hh('t',lambda *a:None)
            self.assertEqual(result['metrics']['successful_responses'],2)
            self.assertEqual(calls,['r1','r2','r2']);self.assertEqual(mail.call_count,2)
    def test_completed_cycle_survives_new_process_without_resend(self):
        import subprocess
        with patch.object(workflow.service,'_build_candidate_result',side_effect=self.item) as ai,patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            workflow.run_hh('t',lambda *a:None)
            self.assertEqual(ai.call_count,2);self.assertEqual(mail.call_count,2)
        env=dict(os.environ,RECRUITMENT_DB_PATH=str(state.DB_PATH))
        code="""import workflow,operation_store as ops
from unittest.mock import patch
with patch.object(workflow.service,'_build_candidate_result',side_effect=AssertionError('AI repeated')),patch.object(workflow,'send_candidate_email',side_effect=AssertionError('Mail repeated')):
    result=workflow.run_hh('t',lambda *a:None)
    assert result['metrics']['successful_responses']==2
assert len([x for x in workflow._task_ops('t') if x['kind']=='ai'])==2
print('NEW_PROCESS_NO_RESEND_VERIFIED')
"""
        result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('NEW_PROCESS_NO_RESEND_VERIFIED',result.stdout)

    def test_unknown_stays_blocked(self):
        with patch.object(workflow.service,'_build_candidate_result',side_effect=AIUnavailableError('reset',True)) as build:
            with self.assertRaises(workflow.NeedsManual):workflow.run_hh('t',lambda *a:None)
            with state.database() as c:self.assertEqual(ops.allow_confirmed_unsent_retries('t',c),0)
            with self.assertRaises(workflow.NeedsManual):workflow.run_hh('t',lambda *a:None)
            self.assertEqual(build.call_count,1);self.assertTrue(tracking.was_sent_to_ai('r1','v'))
    def test_failure_record_atomic_rollback(self):
        oid=ops.reserve_and_begin_ai('t','r1','v','v','','test@example.com')
        with state.database() as c:c.execute("CREATE TRIGGER injected BEFORE UPDATE ON history BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(Exception):ops.record_ai_before_send_failure(oid,'t','r1','v','unsent')
        self.assertEqual(ops.get_op('t','ai','r1')['state'],'sending');self.assertTrue(tracking.was_sent_to_ai('r1','v'))
    def test_legacy_failed_not_released(self):
        oid=ops.reserve_and_begin_ai('t','r1','v','v','','test@example.com');ops.finish(oid,'failed',{'error':'legacy'})
        with state.database() as c:self.assertEqual(ops.allow_confirmed_unsent_retries('t',c),0)
    def test_safe_failure_available_to_new_task(self):
        oid=ops.reserve_and_begin_ai('t','r1','v','v','','test@example.com');ops.record_ai_before_send_failure(oid,'t','r1','v','unsent')
        self.assertFalse(tracking.was_sent_to_ai('r1','v'))
        self.assertTrue(ops.reserve_and_begin_ai('new','r1','v','v','','test@example.com'))
        with state.database() as c:self.assertEqual(ops.allow_confirmed_unsent_retries('t',c),0)

class HHTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env=Path(self.tmp.name)/'hh.env';self.env.write_text("HH_BEARER=old\nHH_REFRESH_TOKEN=refresh-old\n")
        for name,value in [('ENV_PATH',self.env),('HH_CLIENT_ID','client'),('HH_CLIENT_SECRET','secret')]:
            p=patch.object(hh,name,value);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,HH_BEARER='old',HH_REFRESH_TOKEN='refresh-old',HH_AUTH_REVISION='');p.start();self.addCleanup(p.stop)
        hh._AUTH_LAST_ATTEMPT=None;hh._AUTH_LAST_TIME=0;hh._AUTH_FAILURE_MESSAGE=''
    def test_refresh_success_persisted(self):
        with patch.object(hh.requests,'post',return_value=response(200,{'access_token':'new','refresh_token':'refresh-new'})):
            self.assertTrue(hh.refresh_access_token())
        self.assertEqual(dotenv_values(self.env)['HH_REFRESH_TOKEN'],'refresh-new');self.assertFalse(hh._refresh_state_path().exists())
    def test_invalid_grant_not_repeated_after_restart(self):
        with patch.object(hh.requests,'post',return_value=response(400,{'error':'invalid_grant','error_description':'token has already been refreshed'})) as post:
            self.assertFalse(hh.refresh_access_token());hh._AUTH_LAST_ATTEMPT=None
            self.assertFalse(hh.refresh_access_token());self.assertEqual(post.call_count,1)
        self.assertIn('уже использован',hh._AUTH_FAILURE_MESSAGE)
    def test_generic_403_does_not_refresh(self):
        with patch.object(hh.requests,'get',return_value=response(403,{'errors':[{'type':'api_access_payment','value':'action_must_be_payed'}]})),patch.object(hh.requests,'post') as post:
            with self.assertRaises(HHUnavailableError):hh.hh_get('/resumes')
            post.assert_not_called()
    def test_revoked_does_not_refresh(self):
        with patch.object(hh.requests,'get',return_value=response(403,{'errors':[{'type':'oauth','value':'token_revoked'}]})),patch.object(hh.requests,'post') as post:
            with self.assertRaises(HHUnavailableError):hh.hh_get('/me')
            post.assert_not_called()
    def test_expired_refresh_then_retry(self):
        with patch.object(hh.requests,'get',side_effect=[response(403,{'errors':[{'type':'oauth','value':'token_expired'}]}),response(200,{})]) as get,patch.object(hh.requests,'post',return_value=response(200,{'access_token':'new','refresh_token':'refresh-new'})) as post:
            self.assertEqual(hh.hh_get('/me').status_code,200);self.assertEqual(post.call_count,1)
            self.assertEqual(get.call_args.kwargs['headers']['Authorization'],'Bearer new')
    def test_network_unknown_refresh_not_repeated(self):
        with patch.object(hh.requests,'post',side_effect=requests.ConnectionError('reset')) as post:
            self.assertFalse(hh.refresh_access_token());self.assertFalse(hh.refresh_access_token());self.assertEqual(post.call_count,1)
    def test_saved_rotation_reused_without_post(self):
        self.env.write_text('HH_BEARER=new\nHH_REFRESH_TOKEN=refresh-new\n')
        with patch.object(hh.requests,'post') as post:
            self.assertTrue(hh.recover_auth('old',0));post.assert_not_called()
    def test_concurrent_expiry_single_refresh(self):
        def get(*a,**kw):return response(200,{}) if kw['headers'].get('Authorization')=='Bearer new' else response(403,{'errors':[{'type':'oauth','value':'token_expired'}]})
        def post(*a,**kw):time.sleep(.02);return response(200,{'access_token':'new','refresh_token':'refresh-new'})
        errors=[]
        def run():
            try:hh.hh_get('/me')
            except Exception as e:errors.append(e)
        with patch.object(hh.requests,'get',side_effect=get),patch.object(hh.requests,'post',side_effect=post) as p:
            threads=[threading.Thread(target=run) for _ in range(6)]
            for t in threads:t.start()
            for t in threads:t.join(3)
            self.assertFalse(errors);self.assertTrue(all(not t.is_alive() for t in threads));self.assertEqual(p.call_count,1)
    def test_error_body_not_logged(self):
        with patch.object(hh.requests,'post',return_value=response(400,{'error':'private-secret','error_description':'private-secret'})),self.assertLogs(hh.logger,level='WARNING') as logs:
            self.assertFalse(hh.refresh_access_token())
        self.assertNotIn('private-secret',' '.join(logs.output))

class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.seed=self.root/'seed';self.folder=self.root/'config'
        self.seed.write_text('HH_CLIENT_ID=id\nHH_CLIENT_SECRET=secret\nHH_BEARER=old\nHH_REFRESH_TOKEN=refresh-old\nNANO_GPT_API_KEY=ai-old\n')
    def test_rotation_preserved_when_non_auth_changes(self):
        self.assertTrue(config_sync.sync(self.seed,self.folder))
        p=self.folder/'hh.env';d=dotenv_values(p);d.update(HH_BEARER='rotated',HH_REFRESH_TOKEN='refresh-rotated');config_sync.atomic_env(p,d)
        self.seed.write_text(self.seed.read_text().replace('ai-old','ai-new'))
        self.assertFalse(config_sync.sync(self.seed,self.folder));d=dotenv_values(p)
        self.assertEqual(d['HH_REFRESH_TOKEN'],'refresh-rotated');self.assertEqual(d['NANO_GPT_API_KEY'],'ai-new')
    def test_explicit_new_auth_replaces_and_clears_guard(self):
        config_sync.sync(self.seed,self.folder);guard=self.folder/'.hh-refresh-state.json';guard.write_text('{}')
        self.seed.write_text(self.seed.read_text().replace('refresh-old','refresh-new'))
        self.assertTrue(config_sync.sync(self.seed,self.folder));self.assertFalse(guard.exists())
        self.assertEqual(dotenv_values(self.folder/'hh.env')['HH_REFRESH_TOKEN'],'refresh-new')
    def test_old_env_cannot_inject_database_or_password(self):
        self.seed.write_text(self.seed.read_text()+'DATABASE_URL=old\nGMAIL_APP_PASSWORD=secret\nHH_INTERACTIVE_AUTH=true\n')
        config_sync.sync(self.seed,self.folder);d=dotenv_values(self.folder/'hh.env')
        self.assertNotIn('DATABASE_URL',d);self.assertNotIn('GMAIL_APP_PASSWORD',d);self.assertEqual(d['HH_INTERACTIVE_AUTH'],'false')
    def test_prepare_updates_env_preserves_db_passwords(self):
        env='HH_CLIENT_ID=id\nHH_CLIENT_SECRET=secret\nEMPLOYER_ID=1\nHH_BEARER=old\nNANO_GPT_API_KEY=key\nGMAIL_USER=test@example.com\nGMAIL_APP_PASSWORD=abcdefghijklmnop\n'
        (self.root/'.env').write_text(env);prepare.prepare(self.root)
        folder=self.root/'deploy/secrets';old=(folder/'db_admin').read_bytes()
        (self.root/'.env').write_text(env.replace('NANO_GPT_API_KEY=key','NANO_GPT_API_KEY=new'))
        prepare.prepare(self.root);self.assertEqual((folder/'db_admin').read_bytes(),old)
        self.assertEqual(dotenv_values(folder/'hh_seed')['NANO_GPT_API_KEY'],'new');self.assertEqual(len(list(folder.iterdir())),7)
    def test_prepare_rejects_directory(self):
        env='HH_CLIENT_ID=id\nHH_CLIENT_SECRET=secret\nEMPLOYER_ID=1\nHH_BEARER=old\nNANO_GPT_API_KEY=key\nGMAIL_USER=test@example.com\nGMAIL_APP_PASSWORD=abcdefghijklmnop\n'
        (self.root/'.env').write_text(env);(self.root/'deploy/secrets/browser_key').mkdir(parents=True)
        with self.assertRaises(SystemExit):prepare.prepare(self.root)
    def test_prepare_missing_env_no_mutation(self):
        with self.assertRaises(SystemExit):prepare.prepare(self.root)
        self.assertFalse((self.root/'deploy').exists())

if __name__=='__main__':unittest.main()

