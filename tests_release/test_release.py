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
import nano_client as nano,hh_client as hh,workflow,config_sync,mail_service
import recruitment_data as recruitment_data, recruitment_service as recruitment_service
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

class ResumeQueryValidationTests(unittest.TestCase):
    VACANCY={'name':'Pre-sale менеджер','area':{'name':'Алматы'}}
    SAFE='("Pre-sale менеджер" OR "пресейл менеджер") AND ("RFP" OR "PoC")'
    NARROW='("Pre-sale менеджер" OR "пресейл менеджер") AND ("RFP" OR "RFI") AND ("PoC" OR "пилот")'
    def invalid(self,q,v=None):
        with self.assertRaises(AIUnavailableError) as e:nano.validate_resume_language_query(q,v or self.VACANCY)
        self.assertTrue(e.exception.request_sent)
    def test_presales_synonyms(self):
        v={'name':'Специалист предпродажной поддержки','area':{'name':'Алматы'}};q='("Pre-sales manager" OR "пресейл менеджер") AND ("RFP" OR "PoC" OR "корпоративные клиенты")'
        self.assertEqual(nano.validate_resume_language_query(q,v),q)
    def test_fullstack_parenthetical_stack(self):
        v={'name':'Senior Fullstack Developer (TypeScript / Node.js / Next.js)','area':{'name':'Алматы'}};q='("Senior Fullstack Developer" OR "Senior Full-stack Developer") AND ("TypeScript" OR "Node.js" OR "Next.js")'
        self.assertEqual(nano.validate_resume_language_query(q,v),q)
    def test_one_quoted_role_rejected(self):
        self.invalid('("Pre-sale менеджер") AND ("RFP" OR "PoC")')
    def test_unquoted_second_role_rejected(self):
        self.invalid('("Pre-sale менеджер" OR presales) AND ("RFP" OR "PoC")')
    def test_common_support_token_does_not_prove_presales_role(self):
        vacancy={'name':'Специалист предпродажной поддержки','area':{'name':'Алматы'}}
        self.invalid('("Специалист технической поддержки" OR "Support manager") AND ("RFP" OR "PoC")',vacancy)
    def test_unrelated_role_rejected(self):self.invalid('("Accountant" OR "Controller") AND ("RFP" OR "PoC")')
    def test_role_group_required(self):self.invalid('"Pre-sale менеджер" AND ("RFP" OR "PoC")')
    def test_area_boundaries(self):
        v={'name':'Pre-sale менеджер','area':{'name':'Рим'}}
        self.invalid('("Pre-sale менеджер" OR "пресейл менеджер") AND ("RFP" OR "Рим")',v)
        q='("Pre-sale менеджер" OR "пресейл менеджер") AND ("RFP" OR "римский")';self.assertEqual(nano.validate_resume_language_query(q,v),q)
    def test_and_in_quotes_not_counted(self):
        q='("Pre-sale менеджер" OR "пресейл менеджер") AND ("research AND development" OR "RFP")';self.assertEqual(nano.validate_resume_language_query(q,self.VACANCY),q)
    def test_lowercase_and_counted(self):self.invalid('("Pre-sale менеджер" OR "пресейл менеджер") and ("RFP") and ("PoC")')
    def test_android_not_operator(self):
        q='("Pre-sale менеджер" OR "пресейл менеджер") AND ("Android" OR "RFP")';self.assertEqual(nano.validate_resume_language_query(q,self.VACANCY),q)
    def test_early_closing_parenthesis(self):self.invalid(')("Pre-sale менеджер" OR "пресейл менеджер") AND ("RFP")(')
    def test_not_quotes_empty_long(self):
        for q in (self.SAFE+' NOT "x"',self.SAFE+' "','', '"Pre-sale менеджер" '+('x'*nano.QUERY_MAX_LENGTH)):
            with self.subTest(q=q[:20]):self.invalid(q)
    def test_unbalanced_opening(self):self.invalid(self.SAFE[:-1])
    def test_generate_safe(self):
        with patch.object(nano,'ask',return_value=self.SAFE):self.assertEqual(nano.generate_resume_language_query(self.VACANCY),self.SAFE)
    def test_generate_narrow(self):
        with patch.object(nano,'ask',return_value=self.NARROW):self.invalid_generated()
    def invalid_generated(self):
        with self.assertRaises(AIUnavailableError):nano.generate_resume_language_query(self.VACANCY)

class LeadershipCandidateFilterTests(unittest.TestCase):
    VACANCY={'name':'Pre-sale менеджер','area':{'id':'1','name':'Алматы'}}
    def req(self,v=None):return recruitment_data.vacancy_requirements(v or self.VACANCY)
    def resume(self,**kw):
        r={'id':'r','title':'Пресейл-инженер','area':{'id':'1','name':'Алматы'},'total_experience':{'months':60}};r.update(kw);return r
    def accepted(self,r,v=None):self.assertEqual(recruitment_data.resume_filter_result(r,self.req(v))[::2],(True,'accepted'))
    def rejected(self,r,v=None):
        accepted,notes,reason=recruitment_data.resume_filter_result(r,self.req(v));self.assertFalse(accepted);self.assertEqual(reason,'leadership');self.assertEqual(notes,['Руководящий профиль не соответствует неруководящей вакансии'])
    def test_non_leadership_vacancy_rejects_commercial_director(self):self.rejected(self.resume(title='Коммерческий директор'))
    def test_non_leadership_vacancy_rejects_head_of_presales(self):self.rejected(self.resume(title='Head of Pre-sales'))
    def test_presales_engineer_is_accepted(self):self.accepted(self.resume())
    def test_leadership_vacancy_accepts_head_of_presales(self):
        v={'name':'Руководитель отдела Pre-sales','area':{'id':'1','name':'Алматы'}};self.assertTrue(self.req(v)['is_leadership_vacancy']);self.accepted(self.resume(title='Head of Pre-sales'),v)
    def test_old_leadership_position_does_not_reject_current_individual_contributor(self):self.accepted(self.resume(experience=[{'position':'Руководитель отдела продаж','start':'2018-01','end':'2020-01'},{'position':'Пресейл-инженер','start':'2024-01','end':None}]))
    def test_current_sales_director_rejected(self):self.rejected(self.resume(experience=[{'position':'Директор по продажам','start':'2024-01','end':None}]))
    def test_false_positive_titles_are_accepted(self):
        for x in ('Lead Generation Manager','Ведущий специалист','Project Manager'):
            with self.subTest(title=x):self.accepted(self.resume(title=x))
    def test_professional_roles_dict_rejects_leadership(self):self.rejected(self.resume(professional_roles=[{'name':'Руководитель направления'}]))
    def test_professional_roles_strings_are_supported(self):self.rejected(self.resume(professional_roles=['Head of Pre-sales']))
    def test_unsorted_experience_prefers_current_position(self):self.accepted(self.resume(experience=[{'position':'Коммерческий директор','start':'2025-01','end':'2025-06'},{'position':'Пресейл-инженер','start':'2022-01','end':None}]))
    def test_without_current_position_latest_start_is_used(self):
        self.accepted(self.resume(experience=[{'position':'Директор по продажам','start':'2020-01','end':'2021-01'},{'position':'Пресейл-инженер','start':'2023-05','end':'2024-01'}]));self.rejected(self.resume(experience=[{'position':'Пресейл-инженер','start':'2020-01','end':'2021-01'},{'position':'Директор по продажам','start':'2023-05','end':'2024-01'}]))
    def test_vacancy_false_positive_phrases_are_not_leadership(self):
        for x in ('Product Manager','Project Manager','Ведущий специалист','Lead Generation Manager'):
            with self.subTest(name=x):self.assertFalse(self.req({'name':x})['is_leadership_vacancy'])
    def test_pool_skips_leader_and_continues_to_next_candidate(self):
        metrics={'scanned':0,'search_items':0,'already_sent':0,'excluded_city':0,'excluded_experience':0,'load_failed':0,'pool':0};items=[{'id':'leader','area':{'id':'1'}},{'id':'engineer','area':{'id':'1'}}];resumes={'leader':self.resume(id='leader',title='Коммерческий директор'),'engineer':self.resume(id='engineer')}
        with patch.object(recruitment_service,'search_resumes',return_value={'items':items,'pages':1}),patch.object(recruitment_service,'fetch_resume_details',side_effect=lambda x:resumes[x]),patch.object(recruitment_service,'was_sent_to_ai',return_value=False),patch.object(recruitment_service,'reset_history_cache'):
            pool=recruitment_service._search_candidate_pool('v',self.VACANCY,self.req(),'q',1,1,metrics,lambda *a:None)
        self.assertEqual(len(pool),1);self.assertEqual(pool[0][0],'engineer');self.assertEqual(metrics['excluded_leadership'],1);self.assertEqual(metrics['pool'],1)

class RelevanceContractTests(unittest.TestCase):
    def payload(self,suitability='suitable',status='met',item_evidence=None):
        if item_evidence is None:item_evidence=['Подтвержден конкретный опыт']
        return {'candidate_number':1,'suitability':suitability,'mandatory_assessment':[{'requirement_id':'target_function','requirement':'Целевая функция','status':status,'evidence':item_evidence}],'scores':{key:0 for key in nano.LIMITS},'evidence':{key:['данных недостаточно'] for key in nano.LIMITS},'strengths':[],'gaps':[],'missing_data':[],'recommendation':'Ручная проверка','human_review_required':True}
    def evaluate(self,payload):
        with patch.object(nano,'ask',return_value=json.dumps(payload,ensure_ascii=False)):return nano.evaluate_one_resume({}, {}, 1, [])
    def test_valid_three_outcomes(self):
        for suitability,status,evidence in [('suitable','met',['Факт']),('not_suitable','not_met',[]),('manual_review','uncertain',[])]:
            with self.subTest(suitability=suitability):self.assertTrue(self.evaluate(self.payload(suitability,status,evidence))['structured'])
    def test_contradictions_and_met_without_evidence_are_unstructured(self):
        for payload in [self.payload('suitable','not_met',[]),self.payload('suitable','uncertain',[]),self.payload('suitable','met',[])]:
            with self.subTest(payload=payload):self.assertFalse(self.evaluate(payload)['structured'])


class MailRelevanceRenderingTests(unittest.TestCase):
    def item(self,suitability,status,evidence=None,missing=None):
        return {'number':1,'url':'https://example.invalid/resume','evaluation_text':'test','evaluation':{'structured':True,'suitability':suitability,'mandatory_assessment':[{'requirement_id':'target_function','requirement':'Опыт целевой функции','status':status,'evidence':evidence or []}],'scores':{key:0 for key in nano.LIMITS},'evidence':{key:['данных недостаточно'] for key in nano.LIMITS},'strengths':[],'gaps':[],'missing_data':missing or [],'recommendation':'Нейтральный комментарий','human_review_required':True,'total':10,'level':'Низкий'}}
    def render(self,item):return mail_service.build_candidate_email({'name':'Vacancy','area':{'name':'Almaty'}},item,'query')
    def test_three_suitability_statuses_are_rendered(self):
        cases=[('suitable','met',['Подтвержден опыт'],'ПОДХОДИТ'),('not_suitable','not_met',['Опыт не подтвержден'],'НЕ ПОДХОДИТ'),('manual_review','uncertain',['Есть неполные сведения'],'ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА')]
        for suitability,status,evidence,label in cases:
            with self.subTest(suitability=suitability):
                body=self.render(self.item(suitability,status,evidence,['Нужны данные о RFP']))
                self.assertIn('СТАТУС СООТВЕТСТВИЯ:',body);self.assertIn(label,body);self.assertIn(evidence[0],body)
    def test_mandatory_status_labels_and_messages_are_rendered(self):
        cases=[('suitable','met','✅ Подтверждено'),('not_suitable','not_met','❌ Не подтверждено'),('manual_review','uncertain','⚠ Недостаточно данных')]
        for suitability,status,label in cases:
            with self.subTest(status=status):self.assertIn(label,self.render(self.item(suitability,status,['Evidence fact'],['Не хватает деталей'])))
        self.assertIn('Кандидат не засчитан как соответствующий',self.render(self.item('not_suitable','not_met',['Нет опыта'])))
        manual=self.render(self.item('manual_review','uncertain',['Неполный факт'],['Не хватает RFP']))
        self.assertIn('Требуется решение рекрутера',manual);self.assertIn('Не хватает RFP',manual)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.p=patch.object(state,'DB_PATH',Path(self.tmp.name)/'db');self.p.start();self.addCleanup(self.p.stop)
        state.init_db();ops.init()
        state.create_task('t','v',2,'test@example.com','hh')
        vacancy={'id':'v','name':'test'};pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[])]
        state.update_task('t',query='test',checkpoint={'vacancy':vacancy,'pool':pool,'pool_complete':True})
    def item(self,v,r,n,notes):return {'resume_id':r['id'],'number':n,'url':'https://example.invalid/resume','evaluation':{'structured':True,'suitability':'suitable','mandatory_assessment':[{'requirement_id':'target_function','requirement':'Целевая профессиональная функция','status':'met','evidence':['Подтвержден конкретный функциональный опыт']}],'scores':{key:0 for key in nano.LIMITS},'evidence':{key:['данных недостаточно'] for key in nano.LIMITS},'strengths':[],'human_review_required':True,'total':50,'level':'test','gaps':[],'missing_data':[],'recommendation':'Нейтральный комментарий'},'evaluation_text':'test'}
    def relevance_item(self,v,r,n,notes,suitability,status,evidence=None):
        item=self.item(v,r,n,notes);item['evaluation']['suitability']=suitability
        item['evaluation']['mandatory_assessment']=[{'requirement_id':'target_function','requirement':'Опыт целевой функции','status':status,'evidence':[] if evidence is None else evidence}]
        return item
    def invalid_item(self,v,r,n,notes,status='invalid_response'):
        return {'resume_id':r['id'],'number':n,'evaluation':{'structured':False,'human_review_required':True,'status':status,'total':None,'level':'Требуется ручная проверка','gaps':[]},'evaluation_text':'invalid'}
    def set_requested(self,count,pool=None):
        task=state.get_task('t');checkpoint=task['checkpoint']
        if pool is not None:checkpoint={**checkpoint,'pool':pool}
        state.update_task('t',requested_count=count,checkpoint=checkpoint)
    def test_query_validation_error_after_ai_response_fails_prepare_without_unknown_operation(self):
        import web_server
        error=AIUnavailableError('deterministic query validation rejection',request_sent=True)
        with patch.object(web_server,'prepare_query',side_effect=error),patch.object(web_server.operations,'mark_inflight_unknown') as mark_unknown:
            web_server.run_task('t',prepare=True)
        mark_unknown.assert_called_once_with('t')
        self.assertEqual(ops.unknowns('t'),[])
        task=state.get_task('t')
        self.assertEqual(task['status'],'failed')
        self.assertEqual(task['stage'],'failed')
        self.assertNotEqual(task['status'],'waiting_manual')

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
    def test_suitable_is_emailed_and_counted(self):
        self.set_requested(1,pool=[('r1',{'id':'r1'},[])])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=self.item),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        mail.assert_called_once();self.assertEqual(result['metrics']['successful_responses'],1);self.assertEqual(result['metrics']['shortfall'],0)
    def test_not_suitable_is_emailed_but_not_counted_and_next_is_processed(self):
        self.set_requested(2);calls=[]
        def build(v,r,n,notes):
            calls.append(r['id'])
            return self.relevance_item(v,r,n,notes,'not_suitable','not_met',['Целевая функция не подтверждена']) if r['id']=='r1' else self.item(v,r,n,notes)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(calls,['r1','r2']);self.assertEqual(mail.call_count,2);self.assertEqual(result['metrics']['emails_sent'],2);self.assertEqual(result['metrics']['successful_responses'],1)
    def test_manual_review_is_emailed_but_not_counted_and_next_is_processed(self):
        self.set_requested(2);calls=[]
        def build(v,r,n,notes):
            calls.append(r['id'])
            item=self.relevance_item(v,r,n,notes,'manual_review','uncertain',['Участие в клиентских встречах указано без роли в цикле продаж']) if r['id']=='r1' else self.item(v,r,n,notes)
            if r['id']=='r1':item['evaluation']['missing_data']=['Не описано участие в RFP или demo']
            return item
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(calls,['r1','r2']);self.assertEqual(mail.call_count,2);self.assertEqual(result['metrics']['emails_sent'],2);self.assertEqual(result['metrics']['successful_responses'],1)
    def test_two_not_suitable_use_pool_ordinals_one_and_two(self):
        self.set_requested(2);numbers=[]
        def build(v,r,n,notes):
            numbers.append((r['id'],n))
            return self.relevance_item(v,r,n,notes,'not_suitable','not_met',['Целевая функция не подтверждена'])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(numbers,[('r1',1),('r2',2)]);self.assertEqual(mail.call_count,2)
        self.assertEqual(result['metrics']['successful_responses'],0)

    def test_manual_review_after_not_suitable_uses_number_two(self):
        self.set_requested(2);numbers=[]
        def build(v,r,n,notes):
            numbers.append((r['id'],n))
            if r['id']=='r1':return self.relevance_item(v,r,n,notes,'not_suitable','not_met',['Нет целевого опыта'])
            return self.relevance_item(v,r,n,notes,'manual_review','uncertain',['Недостаточно сведений'])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent'):
            workflow.run_hh('t',lambda *a:None)
        self.assertEqual(numbers,[('r1',1),('r2',2)])

    def test_suitable_after_non_counted_candidates_uses_number_three_and_closes_count(self):
        pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[]),('r3',{'id':'r3'},[]),('r4',{'id':'r4'},[])]
        self.set_requested(3,pool=pool);numbers=[]
        def build(v,r,n,notes):
            numbers.append((r['id'],n))
            if r['id']=='r1':return self.relevance_item(v,r,n,notes,'not_suitable','not_met',['Нет целевого опыта'])
            if r['id']=='r2':return self.relevance_item(v,r,n,notes,'manual_review','uncertain',['Недостаточно сведений'])
            return self.item(v,r,n,notes)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(numbers,[('r1',1),('r2',2),('r3',3)]);self.assertEqual(mail.call_count,3)
        self.assertEqual(result['metrics']['successful_responses'],1);self.assertEqual(result['metrics']['shortfall'],0)

    def test_skipped_candidate_keeps_gap_in_pool_ordinals(self):
        pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[]),('r3',{'id':'r3'},[])]
        self.set_requested(2,pool=pool)
        skipped=ops.begin('t','ai','r2');ops.finish(skipped,'skipped',{'reason':'manual skip'})
        numbers=[]
        def build(v,r,n,notes):
            numbers.append((r['id'],n))
            return self.relevance_item(v,r,n,notes,'not_suitable','not_met',['Нет целевого опыта'])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent'):
            workflow.run_hh('t',lambda *a:None)
        self.assertEqual(numbers,[('r1',1),('r3',3)])

    def test_after_unknown_manual_skip_next_candidate_keeps_pool_ordinal(self):
        pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[]),('r3',{'id':'r3'},[])]
        self.set_requested(2,pool=pool);numbers=[]
        def first_build(v,r,n,notes):
            numbers.append((r['id'],n))
            if r['id']=='r2':raise AIUnavailableError('read timeout',True)
            return self.relevance_item(v,r,n,notes,'not_suitable','not_met',['Нет целевого опыта'])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=first_build),patch.object(workflow,'send_candidate_email',return_value='Sent'):
            with self.assertRaises(workflow.NeedsManual):workflow.run_hh('t',lambda *a:None)
        unknown=ops.get_op('t','ai','r2');ops.resolve('t',unknown['id'],'skip','Reviewed timeout outcome')
        def resumed_build(v,r,n,notes):
            numbers.append((r['id'],n));return self.item(v,r,n,notes)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=resumed_build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(numbers,[('r1',1),('r2',2),('r3',3)]);self.assertEqual(mail.call_count,1)
        self.assertEqual(result['metrics']['successful_responses'],1)

    def test_existing_succeeded_result_keeps_saved_number(self):
        self.set_requested(2)
        saved=self.item(None,{'id':'r1'},17,[])
        oid=ops.reserve_and_begin_ai('t','r1','v','v','','test@example.com');ops.finish(oid,'succeeded',saved)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=self.item) as ai,patch.object(workflow,'send_candidate_email',return_value='Sent'):
            workflow.run_hh('t',lambda *a:None)
        self.assertEqual(ops.get_op('t','ai','r1')['data']['number'],17)
        self.assertEqual(ai.call_args.args[2],2)

    def test_existing_succeeded_ai_and_email_are_not_renumbered_or_resent(self):
        self.set_requested(1)
        saved=self.item(None,{'id':'r1'},23,[])
        ai_oid=ops.reserve_and_begin_ai('t','r1','v','v','','test@example.com');ops.finish(ai_oid,'succeeded',saved)
        mail_oid=ops.begin('t','hh_email','r1',{'item':saved});ops.finish(mail_oid,'succeeded',{'status':'Sent'})
        with patch.object(workflow.service,'_build_candidate_result',side_effect=AssertionError('AI repeated')),patch.object(workflow,'send_candidate_email') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        mail.assert_not_called();self.assertEqual(ops.get_op('t','ai','r1')['data']['number'],23)
        self.assertEqual(result['metrics']['successful_responses'],1)

    def test_retry_same_resume_uses_same_pool_ordinal(self):
        self.set_requested(2);numbers=[]
        def build(v,r,n,notes):
            numbers.append((r['id'],n))
            if r['id']=='r2' and sum(candidate=='r2' for candidate,_ in numbers)==1:raise AIUnavailableError('connect timeout',False)
            return self.item(v,r,n,notes)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent'):
            with self.assertRaises(AIUnavailableError):workflow.run_hh('t',lambda *a:None)
            with state.database() as con:self.assertEqual(ops.allow_confirmed_unsent_retries('t',con),1)
            workflow.run_hh('t',lambda *a:None)
        self.assertEqual(numbers,[('r1',1),('r2',2),('r2',2)])

    def test_candidate_number_does_not_depend_on_successful_responses(self):
        pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[]),('r3',{'id':'r3'},[])]
        self.set_requested(3,pool=pool);numbers=[]
        def build(v,r,n,notes):
            numbers.append((r['id'],n))
            return self.item(v,r,n,notes) if r['id']=='r1' else self.relevance_item(v,r,n,notes,'not_suitable','not_met',['Нет целевого опыта'])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent'):
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(numbers,[('r1',1),('r2',2),('r3',3)])
        self.assertEqual(result['metrics']['successful_responses'],1)

    def test_candidate_number_does_not_depend_on_emails_sent(self):
        pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[]),('r3',{'id':'r3'},[])]
        self.set_requested(2,pool=pool);numbers=[]
        skipped=ops.begin('t','ai','r2');ops.finish(skipped,'skipped',{'reason':'manual skip'})
        def build(v,r,n,notes):
            numbers.append((r['id'],n))
            return self.relevance_item(v,r,n,notes,'not_suitable','not_met',['Нет целевого опыта']) if r['id']=='r1' else self.item(v,r,n,notes)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent'):
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(numbers,[('r1',1),('r3',3)]);self.assertEqual(result['metrics']['emails_sent'],2)

    def test_mail_quota_two_not_suitable_stops_after_two_emails(self):
        pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[]),('r3',{'id':'r3'},[])]
        self.set_requested(2,pool=pool);calls=[]
        def build(v,r,n,notes):
            calls.append((r['id'],n));return self.relevance_item(v,r,n,notes,'not_suitable','not_met',['Нет целевого опыта'])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(calls,[('r1',1),('r2',2)]);self.assertEqual(mail.call_count,2)
        self.assertIsNone(ops.get_op('t','ai','r3'));self.assertIsNone(ops.get_op('t','hh_email','r3'))
        self.assertEqual(result['considered'],2);self.assertEqual(result['metrics']['emails_sent'],2)
        self.assertEqual(result['metrics']['not_suitable_count'],2);self.assertEqual(result['metrics']['suitable_count'],0)
        self.assertEqual(result['metrics']['manual_review_count'],0);self.assertEqual(result['metrics']['shortfall'],0)

    def test_mail_quota_not_suitable_and_manual_review_stops_after_two(self):
        pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[]),('r3',{'id':'r3'},[])]
        self.set_requested(2,pool=pool);calls=[]
        def build(v,r,n,notes):
            calls.append((r['id'],n))
            if r['id']=='r1':return self.relevance_item(v,r,n,notes,'not_suitable','not_met',['Нет целевого опыта'])
            return self.relevance_item(v,r,n,notes,'manual_review','uncertain',['Недостаточно данных'])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(calls,[('r1',1),('r2',2)]);self.assertEqual(mail.call_count,2)
        self.assertEqual(result['metrics']['not_suitable_count'],1);self.assertEqual(result['metrics']['manual_review_count'],1)
        self.assertEqual(result['metrics']['suitable_count'],0);self.assertEqual(result['metrics']['successful_responses'],0)

    def test_mail_quota_suitable_and_not_suitable_stops_after_two(self):
        pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[]),('r3',{'id':'r3'},[])]
        self.set_requested(2,pool=pool);calls=[]
        def build(v,r,n,notes):
            calls.append((r['id'],n))
            return self.item(v,r,n,notes) if r['id']=='r1' else self.relevance_item(v,r,n,notes,'not_suitable','not_met',['Нет целевого опыта'])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(calls,[('r1',1),('r2',2)]);self.assertEqual(mail.call_count,2)
        self.assertEqual(result['metrics']['suitable_count'],1);self.assertEqual(result['metrics']['not_suitable_count'],1)
        self.assertEqual(result['metrics']['successful_responses'],1)

    def test_unstructured_does_not_take_mail_quota(self):
        pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[]),('r3',{'id':'r3'},[]),('r4',{'id':'r4'},[])]
        self.set_requested(2,pool=pool);calls=[]
        def build(v,r,n,notes):
            calls.append((r['id'],n))
            if r['id']=='r1':return self.invalid_item(v,r,n,notes)
            if r['id']=='r2':return self.relevance_item(v,r,n,notes,'not_suitable','not_met',['Нет целевого опыта'])
            return self.relevance_item(v,r,n,notes,'manual_review','uncertain',['Недостаточно данных'])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(calls,[('r1',1),('r2',2),('r3',3)]);self.assertEqual(mail.call_count,2)
        self.assertIsNone(ops.get_op('t','hh_email','r1'));self.assertIsNone(ops.get_op('t','ai','r4'))
        self.assertEqual(result['metrics']['emails_sent'],2);self.assertEqual(result['metrics']['shortfall'],0)

    def test_ai_unknown_does_not_take_quota_and_pauses(self):
        pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[]),('r3',{'id':'r3'},[])]
        self.set_requested(2,pool=pool);calls=[]
        def build(v,r,n,notes):
            calls.append((r['id'],n))
            if r['id']=='r2':raise AIUnavailableError('read timeout',True)
            return self.relevance_item(v,r,n,notes,'not_suitable','not_met',['Нет целевого опыта'])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            with self.assertRaises(workflow.NeedsManual):workflow.run_hh('t',lambda *a:None)
        self.assertEqual(calls,[('r1',1),('r2',2)]);self.assertEqual(mail.call_count,1)
        self.assertEqual(ops.get_op('t','ai','r2')['state'],'unknown');self.assertIsNone(ops.get_op('t','ai','r3'))
        metrics=workflow._metrics();workflow._sync_metrics('t',metrics);self.assertEqual(metrics['emails_sent'],1)

    def test_existing_unknown_email_blocks_without_retry_or_quota(self):
        self.set_requested(2,pool=[])
        saved=self.relevance_item(None,{'id':'r1'},1,[],'manual_review','uncertain',['Недостаточно данных'])
        ai_oid=ops.reserve_and_begin_ai('t','r1','v','v','','test@example.com');ops.finish(ai_oid,'succeeded',saved)
        mail_oid=ops.begin('t','hh_email','r1',{'item':saved});ops.finish(mail_oid,'unknown',{'item':saved})
        with patch.object(workflow.service,'_build_candidate_result',side_effect=AssertionError('AI repeated')),patch.object(workflow,'send_candidate_email') as mail:
            with self.assertRaises(workflow.NeedsManual):workflow.run_hh('t',lambda *a:None)
        mail.assert_not_called();metrics=workflow._metrics();workflow._sync_metrics('t',metrics)
        self.assertEqual(metrics['emails_sent'],0);self.assertEqual(ops.get_op('t','hh_email','r1')['state'],'unknown')

    def test_existing_succeeded_email_counts_after_resume_and_is_not_resent(self):
        pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[]),('r3',{'id':'r3'},[])]
        self.set_requested(2,pool=pool)
        saved=self.relevance_item(None,{'id':'r1'},1,[],'not_suitable','not_met',['Нет целевого опыта'])
        ai_oid=ops.reserve_and_begin_ai('t','r1','v','v','','test@example.com');ops.finish(ai_oid,'succeeded',saved)
        mail_oid=ops.begin('t','hh_email','r1',{'item':saved});ops.finish(mail_oid,'succeeded',{'status':'Sent'})
        calls=[]
        def build(v,r,n,notes):calls.append((r['id'],n));return self.item(v,r,n,notes)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(calls,[('r2',2)]);self.assertEqual(mail.call_count,1);self.assertIsNone(ops.get_op('t','ai','r3'))
        self.assertEqual(result['metrics']['emails_sent'],2);self.assertEqual(result['metrics']['not_suitable_count'],1)
        self.assertEqual(result['metrics']['suitable_count'],1);self.assertEqual(result['metrics']['shortfall'],0)

    def test_saved_not_suitable_after_restart_is_emailed_without_ai_repeat(self):
        self.set_requested(1,pool=[])
        saved=self.relevance_item(None,{'id':'r1'},1,[],'not_suitable','not_met',['Нет целевого опыта'])
        ai_oid=ops.reserve_and_begin_ai('t','r1','v','v','','test@example.com');ops.finish(ai_oid,'succeeded',saved)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=AssertionError('AI repeated')),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        mail.assert_called_once();self.assertEqual(result['metrics']['emails_sent'],1)
        self.assertEqual(result['metrics']['not_suitable_count'],1);self.assertEqual(result['metrics']['successful_responses'],0)

    def test_saved_manual_review_after_restart_is_emailed_without_ai_repeat(self):
        self.set_requested(1,pool=[])
        saved=self.relevance_item(None,{'id':'r1'},1,[],'manual_review','uncertain',['Недостаточно данных'])
        ai_oid=ops.reserve_and_begin_ai('t','r1','v','v','','test@example.com');ops.finish(ai_oid,'succeeded',saved)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=AssertionError('AI repeated')),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        mail.assert_called_once();self.assertEqual(result['metrics']['emails_sent'],1)
        self.assertEqual(result['metrics']['manual_review_count'],1);self.assertEqual(result['metrics']['successful_responses'],0)

    def test_suitability_metrics_and_shortfall_are_based_on_succeeded_emails(self):
        pool=[('r1',{'id':'r1'},[]),('r2',{'id':'r2'},[]),('r3',{'id':'r3'},[])]
        self.set_requested(3,pool=pool)
        outcomes={'r1':('suitable','met',['Факт']),'r2':('not_suitable','not_met',['Нет опыта']),'r3':('manual_review','uncertain',['Недостаточно данных'])}
        def build(v,r,n,notes):
            suitability,status,evidence=outcomes[r['id']]
            return self.item(v,r,n,notes) if suitability=='suitable' else self.relevance_item(v,r,n,notes,suitability,status,evidence)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email',return_value='Sent') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        self.assertEqual(mail.call_count,3);self.assertEqual(result['metrics']['emails_sent'],3)
        self.assertEqual(result['metrics']['suitable_count'],1);self.assertEqual(result['metrics']['not_suitable_count'],1)
        self.assertEqual(result['metrics']['manual_review_count'],1);self.assertEqual(result['metrics']['successful_responses'],1)
        self.assertEqual(result['metrics']['requested_count'],3);self.assertEqual(result['metrics']['shortfall'],0)

    def test_shortfall_uses_emails_sent(self):
        self.set_requested(2,pool=[('r1',{'id':'r1'},[])])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=self.invalid_item),patch.object(workflow,'send_candidate_email') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        mail.assert_not_called();self.assertEqual(result['metrics']['emails_sent'],0);self.assertEqual(result['metrics']['shortfall'],2)

    def assert_legacy_succeeded_mail_takes_quota(self,status):
        self.set_requested(1,pool=[('r1',{'id':'r1'},[])])
        oid=ops.begin('t','hh_email','legacy-'+status);ops.finish(oid,'succeeded',{'status':status})
        with patch.object(workflow.service,'_build_candidate_result',side_effect=AssertionError('AI must not start')),patch.object(workflow,'send_candidate_email') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        mail.assert_not_called();self.assertEqual(result['metrics']['emails_sent'],1);self.assertEqual(result['considered'],1)

    def test_legacy_succeeded_displayed_mail_takes_quota(self):
        self.assert_legacy_succeeded_mail_takes_quota('Displayed')

    def test_legacy_succeeded_opened_mail_takes_quota(self):
        self.assert_legacy_succeeded_mail_takes_quota('Opened')

    def test_contradictory_suitable_not_met_is_not_emailed(self):
        self.set_requested(1,pool=[('r1',{'id':'r1'},[])])
        build=lambda v,r,n,notes:self.relevance_item(v,r,n,notes,'suitable','not_met',[])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        mail.assert_not_called();self.assertEqual(result['metrics']['successful_responses'],0)
    def test_met_without_evidence_is_not_emailed(self):
        self.set_requested(1,pool=[('r1',{'id':'r1'},[])])
        build=lambda v,r,n,notes:self.relevance_item(v,r,n,notes,'suitable','met',[])
        with patch.object(workflow.service,'_build_candidate_result',side_effect=build),patch.object(workflow,'send_candidate_email') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        mail.assert_not_called();self.assertEqual(result['metrics']['successful_responses'],0)
    def test_legacy_structured_without_suitability_is_not_emailed_or_repeated(self):
        self.set_requested(1,pool=[])
        legacy={'resume_id':'legacy','number':1,'evaluation':{'structured':True,'total':50,'level':'legacy'},'evaluation_text':'legacy'}
        oid=ops.begin('t','ai','legacy');ops.finish(oid,'succeeded',legacy)
        with patch.object(workflow.service,'_build_candidate_result',side_effect=AssertionError('AI repeated')),patch.object(workflow,'send_candidate_email') as mail:
            result=workflow.run_hh('t',lambda *a:None)
        mail.assert_not_called();self.assertEqual(result['metrics']['successful_responses'],0);self.assertEqual([x['resume_id'] for x in result['manual_review']],['legacy'])

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

class OperationFinishFencingTests(unittest.TestCase):
    class Cursor:
        def __init__(self,row=None,rowcount=0):self.row=row;self.rowcount=rowcount
        def fetchone(self):return self.row
        def fetchall(self):return [] if self.row is None else [self.row]

    class Database:
        def __init__(self,operations,jobs,fail_update=False):
            self.operations=operations;self.jobs=jobs;self.fail_update=fail_update
            self.events=[];self.snapshot=None
        def __enter__(self):
            import copy
            self.snapshot=copy.deepcopy((self.operations,self.jobs));return self
        def __exit__(self,exc_type,exc,tb):
            if exc_type:
                operations,jobs=self.snapshot
                self.operations.clear();self.operations.update(operations)
                self.jobs.clear();self.jobs.update(jobs)
            return False
        def execute(self,sql,params=()):
            normalized=' '.join(sql.lower().split());self.events.append((normalized,params))
            if normalized=='begin immediate':return OperationFinishFencingTests.Cursor()
            if normalized.startswith('select') and ' from operations ' in normalized:
                row=self.operations.get(params[0]);return OperationFinishFencingTests.Cursor(dict(row) if row else None)
            if normalized.startswith('select') and ' from task_jobs ' in normalized:
                row=self.jobs.get(params[0]);owned=row and row['owner']==params[1] and row['state']=='running'
                return OperationFinishFencingTests.Cursor(dict(row) if owned else None)
            if normalized.startswith('update operations set state='):
                state,data,updated,oid=params
                if self.fail_update:raise RuntimeError('injected operation update failure')
                row=self.operations.get(oid)
                if row:row.update(state=state,data=data,updated=updated)
                return OperationFinishFencingTests.Cursor(rowcount=int(row is not None))
            raise AssertionError('Unexpected SQL: '+sql)

    def setUp(self):
        self.operations={
            'ai-op':{'id':'ai-op','task_id':'t','kind':'ai','state':'sending','data':'{}','updated':0},
            'mail-op':{'id':'mail-op','task_id':'t','kind':'hh_email','state':'sending','data':'{}','updated':0},
        }
        self.jobs={7:{'id':7,'task_id':'t','owner':'owner-a','state':'running'}}
        self.job={'id':7,'task_id':'t','owner':'owner-a','state':'running'}

    def run_finish(self,oid='ai-op',database=None):
        from transaction_scope import worker_job,bind
        database=database or self.Database(self.operations,self.jobs)
        with patch.object(ops,'database',return_value=database),patch.dict(os.environ,TASK_QUEUE_BACKEND='postgres'):
            with bind(worker_job,self.job):ops.finish(oid,'succeeded',{'result':'saved'})
        return database

    def test_finish_outside_worker_context_keeps_legacy_behavior(self):
        from transaction_scope import worker_job,bind
        database=self.Database(self.operations,self.jobs)
        with patch.object(ops,'database',return_value=database):
            with bind(worker_job,None):ops.finish('ai-op','succeeded',{'result':'saved'})
        self.assertEqual(self.operations['ai-op']['state'],'succeeded')

    def test_finish_with_current_running_owner_succeeds(self):
        database=self.run_finish()
        self.assertEqual(self.operations['ai-op']['state'],'succeeded')
        self.assertTrue(any(' from task_jobs ' in sql for sql,_ in database.events))

    def test_finish_rejects_stale_owner_and_preserves_sending(self):
        self.jobs[7]['owner']='owner-b';before=dict(self.operations['ai-op'])
        with self.assertRaisesRegex(RuntimeError,'ownership|owner|fencing'):self.run_finish()
        self.assertEqual(self.operations['ai-op'],before)

    def test_finish_rejects_job_that_is_not_running(self):
        self.jobs[7]['state']='pending';before=dict(self.operations['ai-op'])
        with self.assertRaisesRegex(RuntimeError,'ownership|owner|fencing'):self.run_finish()
        self.assertEqual(self.operations['ai-op'],before)

    def test_blocked_finish_does_not_store_result_or_allow_retry(self):
        self.jobs[7]['owner']='owner-b'
        with self.assertRaises(RuntimeError):self.run_finish()
        row=self.operations['ai-op']
        self.assertEqual(row['state'],'sending');self.assertEqual(json.loads(row['data']),{})
        self.assertNotEqual(row['state'],'retry_allowed')

    def test_ai_and_hh_email_finish_use_the_same_fencing(self):
        self.jobs[7]['owner']='owner-b'
        for oid in ('ai-op','mail-op'):
            with self.subTest(oid=oid):
                with self.assertRaises(RuntimeError):self.run_finish(oid)
                self.assertEqual(self.operations[oid]['state'],'sending')

    def test_finish_update_failure_rolls_back_transaction(self):
        before=dict(self.operations['ai-op']);database=self.Database(self.operations,self.jobs,fail_update=True)
        with self.assertRaisesRegex(RuntimeError,'injected operation update failure'):self.run_finish(database=database)
        self.assertEqual(self.operations['ai-op'],before)

    def test_ownership_check_happens_before_operation_update(self):
        self.jobs[7]['owner']='owner-b';database=self.Database(self.operations,self.jobs)
        with self.assertRaises(RuntimeError):self.run_finish(database=database)
        sql=[statement for statement,_ in database.events]
        self.assertTrue(any(' from task_jobs ' in statement for statement in sql))
        self.assertFalse(any(statement.startswith('update operations set state=') for statement in sql),
                         'A stale worker must be rejected before UPDATE operations')

class UnknownResolutionActorAuditTests(unittest.TestCase):
    TASK_ID='a'*32
    COOKIE_A='a'*64
    COOKIE_B='b'*64

    def setUp(self):
        import hashlib
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db_patch=patch.object(state,'DB_PATH',Path(self.tmp.name)/'actor-audit.db')
        self.db_patch.start();self.addCleanup(self.db_patch.stop)
        state.init_db();ops.init()
        state.create_task(self.TASK_ID,'vacancy-actor',1,'owner@example.com','hh')
        self.actor_a=hashlib.sha256(self.COOKIE_A.encode()).hexdigest()
        self.actor_b=hashlib.sha256(self.COOKIE_B.encode()).hexdigest()
        state.update_task(self.TASK_ID,owner_session=self.actor_a,status='waiting_manual',stage='manual_resolution')

    def add_unknown(self,kind='ai',key='candidate-1'):
        oid=ops.begin(self.TASK_ID,kind,key)
        ops.finish(oid,'unknown',{'error':'outcome unknown'})
        return oid

    def audit_rows(self):
        with state.database() as con:
            return [dict(row) for row in con.execute('SELECT * FROM resolution_audit ORDER BY id').fetchall()]

    def operation_state(self,kind='ai',key='candidate-1'):
        return ops.get_op(self.TASK_ID,kind,key)['state']

    def post_resolve(self,cookie,oid,action='skip',reason='Reviewed external outcome'):
        import asgi_app
        body=json.dumps({'operation_id':oid,'action':action,'reason':reason}).encode()
        response=asgi_app.dispatch('POST',f'/api/tasks/{self.TASK_ID}/resolve',[
            (b'content-type',b'application/json'),
            (b'cookie',f'task_session={cookie}'.encode()),
        ],body)
        return response

    def test_owner_session_is_written_to_resolution_audit(self):
        oid=self.add_unknown()
        response=self.post_resolve(self.COOKIE_A,oid)
        self.assertEqual(response.status_code,200)
        rows=self.audit_rows();self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['actor_session'],self.actor_a)

    def test_foreign_session_is_forbidden_without_operation_or_audit_change(self):
        oid=self.add_unknown()
        response=self.post_resolve(self.COOKIE_B,oid)
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.operation_state(),'unknown')
        self.assertEqual(self.audit_rows(),[])

    def test_audit_contains_hash_and_never_raw_cookie(self):
        oid=self.add_unknown()
        response=self.post_resolve(self.COOKIE_A,oid)
        self.assertEqual(response.status_code,200)
        row=self.audit_rows()[0]
        self.assertEqual(row['actor_session'],self.actor_a)
        self.assertNotEqual(row['actor_session'],self.COOKIE_A)
        self.assertNotIn(self.COOKIE_A,json.dumps(row,ensure_ascii=False))

    def test_repeated_resolution_is_rejected_without_duplicate_audit(self):
        oid=self.add_unknown()
        first=self.post_resolve(self.COOKIE_A,oid)
        self.assertEqual(first.status_code,200)
        original=self.audit_rows();self.assertEqual(len(original),1)
        second=self.post_resolve(self.COOKIE_A,oid)
        self.assertEqual(second.status_code,409)
        self.assertEqual(self.audit_rows(),original)

    def test_backend_resolve_persists_actor_with_operation_in_one_transaction(self):
        oid=self.add_unknown()
        ops.resolve(self.TASK_ID,oid,'skip','Backend actor test',actor_session=self.actor_a)
        self.assertEqual(self.operation_state(),'skipped')
        rows=self.audit_rows();self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['operation_id'],oid)
        self.assertEqual(rows[0]['task_id'],self.TASK_ID)
        self.assertEqual(rows[0]['actor_session'],self.actor_a)

    def test_backend_resolve_rolls_back_operation_when_audit_insert_fails(self):
        import sqlite3
        oid=self.add_unknown()
        with state.database() as con:
            con.execute("""CREATE TRIGGER reject_resolution_audit
                BEFORE INSERT ON resolution_audit
                BEGIN SELECT RAISE(ABORT,'injected audit failure'); END""")
        with self.assertRaises(sqlite3.DatabaseError):
            ops.resolve(self.TASK_ID,oid,'skip','Atomic audit test',actor_session=self.actor_a)
        self.assertEqual(self.operation_state(),'unknown')
        self.assertEqual(self.audit_rows(),[])

    def test_backend_resolve_without_actor_remains_compatible_and_stores_null(self):
        oid=self.add_unknown()
        ops.resolve(self.TASK_ID,oid,'skip','Administrative compatibility')
        rows=self.audit_rows();self.assertEqual(len(rows),1)
        self.assertIsNone(rows[0]['actor_session'])

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
