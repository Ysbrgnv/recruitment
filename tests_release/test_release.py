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
import nano_client as nano,hh_client as hh,workflow,config_sync,mail_service,recruitment_data as recruitment_data_module,recruitment_service as service
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

class VacancySearchProfileTests(unittest.TestCase):
    def profile(self,titles=None,functions=None,concepts=None,role='presales',reasons=None,excluded=None):
        titles=titles or ['Pre-sales Manager','Sales Engineer','Solution Consultant','Руководитель пресейла'];excluded=excluded or []
        return {'role_family':role,'leadership_intent':{'level':'individual_contributor','evidence':['Вакансия описывает выполнение профессиональных функций без управления подразделением.']},'target_functions':functions or ['выявление потребностей','демонстрация решения'],'accepted_role_titles':titles,'mandatory_search_concepts':concepts if concepts is not None else [{'concept_id':'discovery','alternatives':['выявление потребностей','customer discovery']},{'concept_id':'demo','alternatives':['демонстрация решения','solution demo']}],'domain_context':['B2B'],'preferred_concepts':['RFP','RFI','RFQ','PoC','ROI'],'excluded_role_families':[],'excluded_titles':excluded,'rationale':{'role_family':'Функции вакансии','accepted_role_titles':reasons or {'Sales Engineer':'Presales discovery demo сделки','Solution Consultant':'Presales демонстрация сделки','Руководитель пресейла':'Presales руководство'},'excluded_titles':{x:'Иное направление' for x in excluded}}}
    def test_presales_analyst_titles_without_rationale_are_rejected(self):
        for title in ('Business Analyst','System Analyst'):
            with self.subTest(title=title):
                with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(self.profile([title],reasons={}))
    def test_presales_alternative_titles_are_preserved(self):
        query=nano.render_hh_query(self.profile())
        for title in ('Sales Engineer','Solution Consultant','Руководитель пресейла'):self.assertIn(f'"{title}"',query)
    def test_accepted_and_excluded_overlap_is_rejected(self):
        with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(self.profile(['Руководитель аналитики'],excluded=['Руководитель аналитики']))
    def test_generic_words_do_not_form_core(self):
        with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(self.profile(functions=['B2B','product','client'],concepts=[]))
    def test_discovery_and_demo_groups_are_rendered(self):
        query=nano.render_hh_query(self.profile());self.assertIn('customer discovery',query);self.assertIn('solution demo',query)
    def test_preferred_terms_are_not_hard_filters(self):
        query=nano.render_hh_query(self.profile())
        for value in ('RFP','RFI','RFQ','PoC','ROI'):self.assertNotIn(f'"{value}"',query)
    def test_cross_domain_profiles_do_not_gain_presales_terms(self):
        cases=[self.profile(['Backend Developer'],['разработка backend API'],[{'concept_id':'api','alternatives':['backend API']}],'backend',{}),self.profile(['Business Analyst'],['сбор бизнес-требований'],[{'concept_id':'requirements','alternatives':['сбор бизнес-требований']}],'business_analysis',{})]
        for profile in cases:self.assertNotRegex(nano.render_hh_query(profile).lower(),r'presale|sales engineer|solution consultant|rfp')
    def test_network_manager_keeps_function(self):
        query=nano.render_hh_query(self.profile(['Network Operations Manager'],['эксплуатация сети'],[{'concept_id':'network','alternatives':['network operations']}],'network_operations',{}));self.assertIn('network operations',query)
    def test_deterministic_dedup_preserves_first(self):
        profile=self.profile(['Sales Engineer','sales engineer','Solution Consultant'],concepts=[{'concept_id':'demo','alternatives':['Solution Demo','solution demo']}])
        first=nano.render_hh_query(profile);self.assertEqual(first,nano.render_hh_query(profile));self.assertEqual(first.count('"Sales Engineer"'),1)
    def test_syntax_validator_rejects_invalid_queries(self):
        for query in ('("role"','("role)','() AND "role"','"role" AND OR "function"','"role" AND','NOT "role"'):
            with self.subTest(query=query):
                with self.assertRaises(ValueError):nano.validate_hh_query_syntax(query)
    def test_query_is_one_line(self):
        query=nano.render_hh_query(self.profile());self.assertNotIn('\n',query);self.assertEqual(nano.validate_hh_query_syntax(query),query)


class VacancySearchProfileNormalizationTests(unittest.TestCase):
    def profile(self,domain_context):
        return {'role_family':'presales','leadership_intent':{'level':'individual_contributor','evidence':['Вакансия описывает выполнение профессиональных функций без управления подразделением.']},'target_functions':['выявление потребностей','демонстрация решения'],'accepted_role_titles':['Pre-sales Manager','Sales Engineer'],'mandatory_search_concepts':[{'concept_id':'discovery','alternatives':['выявление потребностей','customer discovery']},{'concept_id':'demo','alternatives':['демонстрация решения','solution demo']}],'domain_context':domain_context,'preferred_concepts':['RFP'],'excluded_role_families':['analytics_without_presales'],'excluded_titles':[],'rationale':{'role_family':'Presales функции','accepted_role_titles':{'Sales Engineer':'Presales discovery demo сделки'},'excluded_titles':{}}}
    def vacancy(self):return {'name':'Pre-sale Manager'}
    def test_domain_context_string_normalizes_to_list(self):
        self.assertEqual(nano.validate_vacancy_search_profile(self.profile('B2B'))['domain_context'],['B2B'])
    def test_domain_context_list_is_unchanged(self):
        self.assertEqual(nano.validate_vacancy_search_profile(self.profile(['B2B','enterprise']))['domain_context'],['B2B','enterprise'])
    def test_domain_context_null_or_object_is_rejected(self):
        for value in (None,{'kind':'B2B'},7,True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(self.profile(value))
    def test_string_accepted_role_title_normalizes_safely(self):
        profile=self.profile('B2B');profile['accepted_role_titles']='Sales Engineer'
        self.assertEqual(nano.validate_vacancy_search_profile(profile)['accepted_role_titles'],['Sales Engineer'])
    def test_mandatory_concepts_object_is_not_normalized(self):
        profile=self.profile('B2B');profile['mandatory_search_concepts']={'concept_id':'demo','alternatives':['demo']}
        with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(profile)
    def test_analyst_titles_still_fail_after_list_normalization(self):
        for title in ('Business Analyst','System Analyst'):
            profile=self.profile('B2B');profile['accepted_role_titles']=title;profile['rationale']['accepted_role_titles']={}
            with self.subTest(title=title):
                with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(profile)
    def test_string_domain_context_builds_structured_query_without_fallback(self):
        profile=self.profile('B2B')
        with patch.object(nano,'ask',return_value=json.dumps(profile,ensure_ascii=False)) as ask:
            query=nano.generate_resume_language_query(self.vacancy())
        self.assertEqual(ask.call_count,1);self.assertIn('("Pre-sales Manager" OR "Sales Engineer")',query)
        functional='("выявление потребностей" OR "customer discovery" OR "демонстрация решения" OR "solution demo")'
        self.assertIn(functional,query);self.assertIn('("B2B")',query);self.assertEqual(query.count(' AND '),2)
        self.assertEqual(nano.validate_hh_query_syntax(query),query);self.assertNotEqual(query,'"Pre-sale Manager"')
    def test_invalid_profile_still_uses_title_fallback_once(self):
        profile=self.profile(None)
        with patch.object(nano,'ask',return_value=json.dumps(profile,ensure_ascii=False)) as ask:
            self.assertEqual(nano.generate_resume_language_query(self.vacancy()),'"Pre-sale Manager"')
        self.assertEqual(ask.call_count,1)


class VacancySearchProfileRationaleTests(unittest.TestCase):
    def profile(self):
        return {'role_family':'presales','leadership_intent':{'level':'individual_contributor','evidence':['Вакансия описывает выполнение профессиональных функций без управления подразделением.']},'target_functions':['выявление потребностей','демонстрация решения'],'accepted_role_titles':['Pre-sales Manager','Sales Engineer','Solution Consultant'],'mandatory_search_concepts':[{'concept_id':'discovery','alternatives':['выявление потребностей','customer discovery']},{'concept_id':'demo','alternatives':['демонстрация решения','solution demo']}],'domain_context':'B2B','preferred_concepts':['RFP'],'excluded_role_families':['analytics_without_presales'],'excluded_titles':[],'rationale':{'role_family':'Роль сопровождает техническую часть B2B-сделки','accepted_role_titles':{'Sales Engineer':'Presales discovery, demo и сопровождение сделки','Solution Consultant':'Presales демонстрация решения и сопровождение сделки'},'excluded_titles':{}}}
    def test_exact_rationale_contract_passes(self):
        validated=nano.validate_vacancy_search_profile(self.profile());self.assertEqual(validated['rationale']['excluded_titles'],{})
    def test_empty_excluded_titles_requires_empty_object(self):
        profile=self.profile();profile['rationale']['excluded_titles']=[]
        with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(profile)
    def test_nonempty_excluded_title_requires_exact_nonempty_reason(self):
        profile=self.profile();profile['excluded_titles']=['Руководитель аналитики'];profile['rationale']['excluded_titles']={}
        with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(profile)
        profile['rationale']['excluded_titles']={'Руководитель аналитики':'Иное направление без Presales-функций'}
        self.assertEqual(nano.validate_vacancy_search_profile(profile)['excluded_titles'],['Руководитель аналитики'])
    def test_rationale_string_or_list_is_rejected(self):
        for value in ('общее обоснование',['обоснование']):
            profile=self.profile();profile['rationale']=value
            with self.subTest(value=value):
                with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(profile)
    def test_nested_rationale_strings_are_rejected(self):
        for field in ('accepted_role_titles','excluded_titles'):
            profile=self.profile();profile['rationale'][field]='общее обоснование'
            with self.subTest(field=field):
                with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(profile)
    def test_extra_rationale_key_is_rejected(self):
        profile=self.profile();profile['rationale']['extra']='нельзя'
        with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(profile)
    def test_missing_role_family_rationale_is_rejected(self):
        profile=self.profile();del profile['rationale']['role_family']
        with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(profile)
    def test_presales_alternatives_require_exact_functional_rationale(self):
        for title in ('Sales Engineer','Solution Consultant'):
            profile=self.profile();profile['accepted_role_titles']=title;profile['rationale']['accepted_role_titles']={title:'Presales discovery, демонстрация решения и сопровождение сделки'}
            with self.subTest(title=title):self.assertEqual(nano.validate_vacancy_search_profile(profile)['accepted_role_titles'],[title])
    def test_analyst_titles_without_functional_rationale_are_rejected(self):
        for title in ('Business Analyst','System Analyst'):
            profile=self.profile();profile['accepted_role_titles']=title;profile['rationale']['accepted_role_titles']={}
            with self.subTest(title=title):
                with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(profile)
    def test_head_of_analytics_is_rejected_even_with_general_reason(self):
        profile=self.profile();profile['accepted_role_titles']='Head of Analytics';profile['rationale']['accepted_role_titles']={'Head of Analytics':'Опыт управления и работы с клиентами'}
        with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(profile)
    def test_exact_full_profile_builds_structured_query_with_one_ai_call(self):
        profile=self.profile()
        with patch.object(nano,'ask',return_value=json.dumps(profile,ensure_ascii=False)) as ask:
            query=nano.generate_resume_language_query({'name':'Pre-sale Manager'})
        self.assertEqual(ask.call_count,1);self.assertIn('("Pre-sales Manager" OR "Sales Engineer" OR "Solution Consultant")',query)
        self.assertIn('customer discovery',query);self.assertIn('solution demo',query);self.assertNotEqual(query,'"Pre-sale Manager"')
    def test_invalid_rationale_uses_safe_fallback_without_second_ai_call(self):
        profile=self.profile();profile['rationale']='общее обоснование'
        with patch.object(nano,'ask',return_value=json.dumps(profile,ensure_ascii=False)) as ask:
            self.assertEqual(nano.generate_resume_language_query({'name':'Pre-sale Manager'}),'"Pre-sale Manager"')
        self.assertEqual(ask.call_count,1)


class VacancySearchProfileContextTests(unittest.TestCase):
    def profile(self):
        return {'role_family':'presales','leadership_intent':{'level':'individual_contributor','evidence':['Вакансия описывает выполнение профессиональных функций без управления подразделением.']},'target_functions':['выявление потребностей клиента','квалификация сделки','демонстрация и защита решения'],'accepted_role_titles':['Pre-sale менеджер','Пресейл-менеджер','Pre-sales Manager','Sales Engineer','Solution Consultant'],'mandatory_search_concepts':[{'concept_id':'role_variants','alternatives':['pre-sale','pre-sales','presales','пресейл','pre-sale менеджер','пресейл-менеджер']},{'concept_id':'discovery','alternatives':['выявление потребностей','customer discovery','квалификация сделки']},{'concept_id':'demo','alternatives':['демонстрация решения','solution demo','защита решения']}],'domain_context':['B2B','enterprise','корпоративные клиенты'],'preferred_concepts':['RFI','RFP','RFQ','PoC','пилот','коммерческое предложение','бизнес-кейс','ROI','ТЭО'],'excluded_role_families':[],'excluded_titles':[],'rationale':{'role_family':'Presales функции','accepted_role_titles':{'Sales Engineer':'Presales discovery, demo и сопровождение сделки','Solution Consultant':'Presales демонстрация решения и сопровождение сделки'},'excluded_titles':{}}}
    def test_bare_presales_concept_is_not_function_group_and_titles_stay_in_title_group(self):
        query=nano.render_hh_query(self.profile());self.assertEqual(query.count('"pre-sale"'),0);self.assertEqual(query.count('"Pre-sales Manager"'),1)
    def test_discovery_and_demo_share_one_broad_function_group(self):
        query=nano.render_hh_query(self.profile())
        functional='("выявление потребностей" OR "customer discovery" OR "квалификация сделки" OR "демонстрация решения" OR "solution demo" OR "защита решения")'
        self.assertEqual(query.count(functional),1);self.assertNotIn(') AND ("демонстрация решения"',query)
    def test_domain_context_group_is_one_or_group_and_preferred_is_not_rendered(self):
        query=nano.render_hh_query(self.profile());context='("B2B" OR "enterprise" OR "корпоративные клиенты")'
        self.assertIn(context,query);self.assertTrue(query.endswith(context));self.assertEqual(query.count(' AND '),2)
        for value in ('RFI','RFP','RFQ','PoC','ROI'):self.assertNotIn(f'"{value}"',query)
    def test_domain_context_is_limited_to_six(self):
        profile=self.profile();profile['domain_context']=['B2B','enterprise','корпоративные клиенты','телеком','облако','интеграция','лишний']
        query=nano.render_hh_query(profile);tail=query.rsplit(' AND ',1)[1]
        self.assertEqual(tail.count('"')//2,6);self.assertNotIn('"лишний"',tail)
    def test_context_deduplicates_case_insensitively_preserving_first(self):
        profile=self.profile();profile['domain_context']=['B2B','Enterprise','b2b','enterprise','телеком']
        query=nano.render_hh_query(profile);tail=query.rsplit(' AND ',1)[1]
        self.assertEqual(tail,'("B2B" OR "Enterprise" OR "телеком")')
    def test_generic_context_words_are_not_emitted(self):
        profile=self.profile();profile['domain_context']=['manager','client','solution','B2B','product','presentation'];profile['preferred_concepts']=['RFP']
        tail=nano.render_hh_query(profile).rsplit(' AND ',1)[1]
        self.assertEqual(tail,'("B2B")')
    def test_cross_domain_profiles_use_only_their_own_context(self):
        cases=[({'role_family':'backend','leadership_intent':{'level':'individual_contributor','evidence':['Вакансия описывает выполнение профессиональных функций без управления подразделением.']},'target_functions':['разработка backend API'],'accepted_role_titles':['Backend Developer'],'mandatory_search_concepts':[{'concept_id':'api','alternatives':['разработка API','backend development']}],'domain_context':['Python','backend'],'preferred_concepts':['FastAPI'],'excluded_role_families':[],'excluded_titles':[],'rationale':{'role_family':'Backend функции','accepted_role_titles':{},'excluded_titles':{}}},('Python','backend')),({'role_family':'business_analysis','leadership_intent':{'level':'individual_contributor','evidence':['Вакансия описывает выполнение профессиональных функций без управления подразделением.']},'target_functions':['сбор бизнес-требований'],'accepted_role_titles':['Business Analyst'],'mandatory_search_concepts':[{'concept_id':'requirements','alternatives':['сбор бизнес-требований','requirements elicitation']}],'domain_context':['финтех','бизнес-процессы'],'preferred_concepts':['BPMN'],'excluded_role_families':[],'excluded_titles':[],'rationale':{'role_family':'Аналитические функции','accepted_role_titles':{},'excluded_titles':{}}},('финтех','бизнес-процессы'))]
        for profile,expected in cases:
            query=nano.render_hh_query(profile)
            for value in expected:self.assertIn(f'"{value}"',query)
            self.assertNotRegex(query.lower(),r'presale|pre-sale|sales engineer|rfp|poc')
            for preferred in profile['preferred_concepts']:self.assertNotIn(f'"{preferred}"',query)
    def test_network_operations_keeps_network_function_and_context(self):
        profile={'role_family':'network_operations','leadership_intent':{'level':'individual_contributor','evidence':['Вакансия описывает выполнение профессиональных функций без управления подразделением.']},'target_functions':['эксплуатация сетевой инфраструктуры'],'accepted_role_titles':['Network Operations Manager'],'mandatory_search_concepts':[{'concept_id':'network','alternatives':['network operations','эксплуатация сетевой инфраструктуры']}],'domain_context':['телеком','корпоративная сеть'],'preferred_concepts':['NOC'],'excluded_role_families':[],'excluded_titles':[],'rationale':{'role_family':'Сетевая эксплуатация','accepted_role_titles':{},'excluded_titles':{}}}
        query=nano.render_hh_query(profile);self.assertIn('эксплуатация сетевой инфраструктуры',query);self.assertIn('("телеком" OR "корпоративная сеть")',query);self.assertNotIn('"NOC"',query)
    def test_empty_context_adds_no_empty_group(self):
        profile=self.profile();profile['domain_context']=[];profile['preferred_concepts']=[]
        query=nano.render_hh_query(profile);functional='("выявление потребностей" OR "customer discovery" OR "квалификация сделки" OR "демонстрация решения" OR "solution demo" OR "защита решения")'
        self.assertNotIn('()',query);self.assertEqual(query.count(' AND '),1);self.assertEqual(query.count(functional),1);self.assertEqual(nano.validate_hh_query_syntax(query),query)
    def test_no_not_and_query_is_deterministic_and_valid(self):
        profile=self.profile();first=nano.render_hh_query(profile);self.assertEqual(first,nano.render_hh_query(profile));self.assertNotRegex(first,r'\bNOT\b');self.assertEqual(nano.validate_hh_query_syntax(first),first)
    def test_safe_fallback_is_unchanged(self):
        self.assertEqual(nano.safe_vacancy_title_fallback({'name':'Pre-sale Manager'}),'"Pre-sale Manager"')


class VacancySearchPlanLeadershipTests(unittest.TestCase):
    def profile(self,level="individual_contributor",evidence=None):
        return {"role_family":"presales","leadership_intent":{"level":level,"evidence":evidence or ["Вакансия описывает выполнение Presales-функций без управления подразделением."]},"target_functions":["выявление потребностей","демонстрация решения"],"accepted_role_titles":["Pre-sales Manager","Sales Engineer"],"mandatory_search_concepts":[{"concept_id":"discovery","alternatives":["выявление потребностей","customer discovery"]},{"concept_id":"demo","alternatives":["демонстрация решения","solution demo"]}],"domain_context":["B2B"],"preferred_concepts":["RFP"],"excluded_role_families":[],"excluded_titles":[],"rationale":{"role_family":"Presales-функции","accepted_role_titles":{"Sales Engineer":"Presales discovery и demo"},"excluded_titles":{}}}
    def test_exact_leadership_intent_contract_passes(self):
        validated=nano.validate_vacancy_search_profile(self.profile("leadership",["Управление командой Presales."]))
        self.assertEqual(validated["leadership_intent"],{"level":"leadership","evidence":["Управление командой Presales."]})
    def test_invalid_leadership_intent_shapes_are_rejected(self):
        invalid=[None,"leadership",[],{"level":"leadership"},{"level":"invalid","evidence":["x"]},{"level":"unknown","evidence":[]},{"level":"unknown","evidence":"не список"},{"level":"unknown","evidence":["x"],"extra":True}]
        for value in invalid:
            profile=self.profile();profile["leadership_intent"]=value
            with self.subTest(value=value):
                with self.assertRaises(ValueError):nano.validate_vacancy_search_profile(profile)
    def test_generate_search_plan_uses_one_ai_call(self):
        profile=self.profile()
        with patch.object(nano,"ask",return_value=json.dumps(profile,ensure_ascii=False)) as ask:
            plan=nano.generate_vacancy_search_plan({"name":"Pre-sale Manager"})
        self.assertEqual(ask.call_count,1);self.assertEqual(plan["profile"]["leadership_intent"]["level"],"individual_contributor");self.assertEqual(plan["query"].count(" AND "),2)
    def test_compatibility_wrapper_returns_only_query_with_one_ai_call(self):
        profile=self.profile()
        with patch.object(nano,"ask",return_value=json.dumps(profile,ensure_ascii=False)) as ask:
            query=nano.generate_resume_language_query({"name":"Pre-sale Manager"})
        self.assertEqual(ask.call_count,1);self.assertIsInstance(query,str);self.assertIn('("B2B")',query)
    def test_invalid_profile_preserves_safe_fallback(self):
        with patch.object(nano,"ask",return_value='{"invalid":true}') as ask:
            plan=nano.generate_vacancy_search_plan({"name":"Pre-sale Manager"})
        self.assertEqual(ask.call_count,1);self.assertIsNone(plan["profile"]);self.assertEqual(plan["query"],'"Pre-sale Manager"')


class CurrentOrLatestExperienceTests(unittest.TestCase):
    def role(self,position,start,end,description="",company="Company"):
        return {"position":position,"start":start,"end":end,"description":description,"company":company}
    def test_ongoing_role_is_selected_even_when_not_first(self):
        resume={"experience":[self.role("Old Director","2018-01-01","2022-01-01"),self.role("Solution Consultant","2023-01-01",None)]}
        result=recruitment_data_module.select_current_or_latest_experience(resume)
        self.assertEqual(result["status"],"current");self.assertEqual(result["title"],"Solution Consultant")
    def test_latest_end_date_wins_when_no_ongoing_role(self):
        resume={"experience":[self.role("Older","2020-01","2021-01"),self.role("Latest","2021-02","2024-03")]}
        result=recruitment_data_module.select_current_or_latest_experience(resume)
        self.assertEqual(result["status"],"latest");self.assertEqual(result["title"],"Latest")
    def test_first_item_is_not_assumed_current(self):
        resume={"experience":[self.role("First but old","2010","2012"),self.role("Second and latest","2020","2024")]}
        self.assertEqual(recruitment_data_module.select_current_or_latest_experience(resume)["title"],"Second and latest")
    def test_equal_end_uses_latest_start_and_full_tie_is_unknown(self):
        resume={"experience":[self.role("A","2020-01","2024-01"),self.role("B","2022-01","2024-01")]}
        self.assertEqual(recruitment_data_module.select_current_or_latest_experience(resume)["title"],"B")
        tied={"experience":[self.role("A","2022-01","2024-01"),self.role("B","2022-01","2024-01")]}
        self.assertEqual(recruitment_data_module.select_current_or_latest_experience(tied)["status"],"unknown")
    def test_multiple_conflicting_ongoing_roles_are_unknown(self):
        resume={"experience":[self.role("Solution Consultant","2023-01",None),self.role("Head of Analytics","2024-01",None)]}
        self.assertEqual(recruitment_data_module.select_current_or_latest_experience(resume)["status"],"unknown")
    def test_missing_or_invalid_dates_are_unknown_without_crash(self):
        cases=[{}, {"experience":[]}, {"experience":[self.role("Unknown","bad","also-bad")]}]
        for resume in cases:
            with self.subTest(resume=resume):self.assertEqual(recruitment_data_module.select_current_or_latest_experience(resume)["status"],"unknown")
    def test_resume_title_is_not_used_as_current_role(self):
        result=recruitment_data_module.select_current_or_latest_experience({"title":"Head of Presales","experience":[]})
        self.assertEqual(result["status"],"unknown");self.assertIsNone(result["title"])
    def test_current_role_exposes_responsibilities_and_company(self):
        result=recruitment_data_module.select_current_or_latest_experience({"experience":[self.role("Solution Consultant","2023-01",None,"<p>Проведение demo</p>",{"name":"ACME"})]})
        self.assertEqual(result["responsibilities"],"Проведение demo");self.assertEqual(result["company"],"ACME")


class CandidateLevelClassificationTests(unittest.TestCase):
    def current(self,title,responsibilities="",status="current"):
        return {"status":status,"title":title,"responsibilities":responsibilities,"item":{},"company":None,"evidence":[]}
    def test_strong_leadership_titles_are_leadership(self):
        for title in ("Head of Analytics","Director of Presales","Chief Revenue Officer","Department Head","Руководитель отдела пресейла","Начальник управления","Директор направления"):
            with self.subTest(title=title):self.assertEqual(recruitment_data_module.classify_candidate_level(self.current(title))["level"],"leadership")
    def test_explicit_management_responsibility_proves_leadership(self):
        for text in ("Управление командой из 8 человек","Руководство подразделением","People management and hiring","5 direct reports","Performance management responsibility"):
            with self.subTest(text=text):self.assertEqual(recruitment_data_module.classify_candidate_level(self.current("Consultant",text))["level"],"leadership")
    def test_presale_and_general_manager_titles_are_individual_contributors(self):
        for title in ("Pre-sale Manager","Product Manager","Account Manager","Project Manager"):
            with self.subTest(title=title):self.assertEqual(recruitment_data_module.classify_candidate_level(self.current(title))["level"],"individual_contributor")
    def test_ambiguous_lead_titles_without_management_are_unknown(self):
        for title in ("Lead Engineer","Ведущий инженер","Team Manager"):
            with self.subTest(title=title):self.assertEqual(recruitment_data_module.classify_candidate_level(self.current(title))["level"],"unknown")
    def test_current_ic_role_is_not_overridden_by_old_leadership(self):
        resume={"experience":[{"position":"Director of Analytics","start":"2018","end":"2021"},{"position":"Solution Consultant","start":"2022","end":None}]}
        current=recruitment_data_module.select_current_or_latest_experience(resume)
        self.assertEqual(recruitment_data_module.classify_candidate_level(current)["level"],"individual_contributor")
    def test_unknown_current_role_stays_unknown(self):
        self.assertEqual(recruitment_data_module.classify_candidate_level({"status":"unknown","title":None,"responsibilities":""})["level"],"unknown")


class CleanPoolFilteringTests(unittest.TestCase):
    def profile(self,level="individual_contributor"):
        return {"role_family":"presales","leadership_intent":{"level":level,"evidence":["Vacancy evidence"]},"target_functions":["выявление потребностей","демонстрация решения"],"accepted_role_titles":["Pre-sales Manager","Sales Engineer","Solution Consultant","Head of Presales"],"mandatory_search_concepts":[{"concept_id":"discovery","alternatives":["discovery","выявление потребностей"]},{"concept_id":"demo","alternatives":["demo","демонстрация решения"]}],"domain_context":["B2B"],"preferred_concepts":[],"excluded_role_families":[],"excluded_titles":[],"rationale":{"role_family":"Presales","accepted_role_titles":{},"excluded_titles":{}}}
    def resume(self,title,description="",old=None,rid="1"):
        experience=list(old or [])+[{"position":title,"start":"2024-01","end":None,"description":description}]
        return {"id":rid,"title":title,"area":{"id":"1"},"experience":experience,"skill_set":[],"professional_roles":[],"total_experience":{"months":60},"alternate_url":f"https://example.invalid/{rid}"}
    def decision(self,vacancy_level,title,description="",old=None):
        return service._pool_filter_decision(self.profile(vacancy_level),self.resume(title,description,old))
    def test_ic_vacancy_excludes_current_leadership(self):
        for title in ("Head of Analytics","Director of Presales"):
            keep,reason,*_=self.decision("individual_contributor",title,"Presales discovery and demo" if "Presales" in title else "Analytics leadership")
            with self.subTest(title=title):self.assertFalse(keep);self.assertIn(reason,{"professional_mismatch","leadership_mismatch"})
    def test_ic_vacancy_keeps_solution_consultant_and_presale_manager(self):
        for title in ("Solution Consultant","Pre-sale Manager"):
            keep,reason,*_=self.decision("individual_contributor",title,"Customer discovery and solution demo")
            with self.subTest(title=title):self.assertTrue(keep);self.assertIsNone(reason)
    def test_ambiguous_lead_unknown_is_not_auto_excluded(self):
        keep,reason,_,candidate,_=self.decision("individual_contributor","Lead Engineer","Presales discovery and demo")
        self.assertTrue(keep);self.assertIsNone(reason);self.assertEqual(candidate["level"],"unknown")
    def test_leadership_presales_keeps_matching_head(self):
        keep,reason,*_=self.decision("leadership","Head of Presales","Presales discovery, demo and team leadership")
        self.assertTrue(keep);self.assertIsNone(reason)
    def test_leadership_presales_rejects_head_of_analytics_professionally(self):
        keep,reason,*_=self.decision("leadership","Head of Analytics","Data analytics and BI")
        self.assertFalse(keep);self.assertEqual(reason,"professional_mismatch")
    def test_leadership_vacancy_rejects_ic_by_level(self):
        keep,reason,*_=self.decision("leadership","Solution Consultant","Presales discovery and demo")
        self.assertFalse(keep);self.assertEqual(reason,"level_mismatch")
    def test_unknown_candidate_kept_with_professional_evidence(self):
        keep,reason,_,candidate,_=self.decision("leadership","Lead Engineer","Presales discovery and demo")
        self.assertTrue(keep);self.assertIsNone(reason);self.assertEqual(candidate["level"],"unknown")
    def test_business_analyst_without_presales_is_professional_mismatch(self):
        keep,reason,*_=self.decision("individual_contributor","Business Analyst","Internal requirements and BPMN")
        self.assertFalse(keep);self.assertEqual(reason,"professional_mismatch")
    def test_sales_engineer_and_solution_consultant_with_functions_are_kept(self):
        for title in ("Sales Engineer","Solution Consultant"):
            keep,reason,*_=self.decision("individual_contributor",title,"Customer discovery and solution demo")
            with self.subTest(title=title):self.assertTrue(keep);self.assertIsNone(reason)
    def test_old_leadership_does_not_exclude_current_solution_consultant(self):
        old=[{"position":"Director of Analytics","start":"2018","end":"2021","description":"Analytics"}]
        keep,reason,*_=self.decision("individual_contributor","Solution Consultant","Presales discovery and demo",old)
        self.assertTrue(keep);self.assertIsNone(reason)
    def test_legacy_profile_is_conservative(self):
        decision=service.professional_prefilter(None,self.resume("Head of Analytics"),{"title":"Head of Analytics","responsibilities":""})
        self.assertEqual(decision["decision"],"keep")
    def test_pool_filters_before_append_and_updates_metrics_audit(self):
        resumes={"1":self.resume("Head of Analytics","Analytics leadership",rid="1"),"2":self.resume("Solution Consultant","Presales discovery and demo",rid="2")}
        metrics={key:0 for key in ("search_items","scanned","already_sent","excluded_city","excluded_experience","load_failed","pool","excluded_professional_mismatch","excluded_leadership_mismatch","excluded_level_mismatch","level_unknown_kept")};audit={}
        with patch.object(service,"search_resumes",return_value={"items":[{"id":"1","area":{"id":"1"}},{"id":"2","area":{"id":"1"}}],"pages":1}),patch.object(service,"fetch_resume_details",side_effect=lambda rid:resumes[rid]),patch.object(service,"was_sent_to_ai",return_value=False),patch.object(service,"reset_history_cache"):
            pool=service._search_candidate_pool("v",{}, {"area_id":"1","area_name":"","minimum_experience_months":0},"q",2,2,metrics,lambda *a:None,profile=self.profile(),pool_exclusions=audit)
        self.assertEqual([x[0] for x in pool],["2"]);self.assertEqual(metrics["excluded_professional_mismatch"],1);self.assertIn("1",audit)
    def test_scan_budget_is_finite(self):
        self.assertEqual(service.CLEAN_POOL_SCAN_MULTIPLIER,2)


class CleanPoolSearchPlanFlowTests(unittest.TestCase):
    def test_prepare_query_saves_profile_with_review(self):
        plan={"profile":{"leadership_intent":{"level":"unknown","evidence":["x"]}},"query":"\"Role\""}
        with patch.object(service,"validate_nano_settings",return_value=[]),patch.object(service,"fetch_vacancy_details",return_value={"name":"Role"}),patch.object(service,"generate_vacancy_search_plan",return_value=plan),patch.object(service,"set_review") as review,patch.object(service,"update_task") as update:
            service.prepare_query("v","t","hh")
        review.assert_called_once_with("t",'"Role"',vacancy_search_profile=plan["profile"]);update.assert_not_called()
    def test_workflow_metrics_include_clean_pool_counters(self):
        metrics=workflow._metrics()
        for key in ("excluded_professional_mismatch","excluded_leadership_mismatch","excluded_level_mismatch","level_unknown_kept"):self.assertIn(key,metrics)
    def test_complete_checkpoint_contract_keeps_profile_and_exclusions(self):
        source=Path(workflow.__file__).read_text(encoding="utf-8")
        self.assertIn("'vacancy_search_profile':profile",source);self.assertIn("'pool_exclusions':pool_exclusions",source);self.assertIn("profile=profile",source)


class AtomicReviewPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db_patch=patch.object(state,'DB_PATH',Path(self.tmp.name)/'atomic-review.db');self.db_patch.start();self.addCleanup(self.db_patch.stop)
        state.init_db();state.create_task('review-task','vacancy-1',2,'review@example.com','hh')
    def profile(self):return {'role_family':'presales','leadership_intent':{'level':'individual_contributor','evidence':['Нет управления подразделением.']}}
    def test_set_review_saves_query_and_profile_in_one_update(self):
        profile=self.profile()
        with patch.object(state,'update_task',wraps=state.update_task) as update:
            state.set_review('review-task','"Pre-sales Manager"',vacancy_search_profile=profile)
        update.assert_called_once();kwargs=update.call_args.kwargs
        self.assertEqual(kwargs['query'],'"Pre-sales Manager"');self.assertEqual(kwargs['vacancy_search_profile'],profile);self.assertEqual(kwargs['status'],'waiting_approval')
    def test_task_contains_profile_immediately_after_set_review(self):
        profile=self.profile();state.set_review('review-task','"Pre-sales Manager"',vacancy_search_profile=profile)
        task=state.get_task('review-task');self.assertEqual(task['query'],'"Pre-sales Manager"');self.assertEqual(task['vacancy_search_profile'],profile);self.assertEqual(task['status'],'waiting_approval')
    def test_legacy_set_review_call_remains_compatible(self):
        state.set_review('review-task','"Legacy Role"');task=state.get_task('review-task')
        self.assertEqual(task['query'],'"Legacy Role"');self.assertNotIn('vacancy_search_profile',task)
    def test_failed_save_does_not_leave_false_review_state(self):
        original=state._save
        def fail_after_terminal(con,task):raise RuntimeError('injected save failure')
        with patch.object(state,'_save',side_effect=fail_after_terminal):
            with self.assertRaisesRegex(RuntimeError,'injected save failure'):state.set_review('review-task','"Role"',vacancy_search_profile=self.profile())
        task=state.get_task('review-task');self.assertEqual(task['status'],'queued');self.assertNotIn('query',task);self.assertNotIn('vacancy_search_profile',task)
    def test_set_review_reaches_terminal_save_with_profile_present(self):
        profile=self.profile();captured=[];original=state._save
        def inspect(con,task):captured.append(dict(task));return original(con,task)
        with patch.object(state,'_save',side_effect=inspect):state.set_review('review-task','"Role"',vacancy_search_profile=profile)
        self.assertEqual(len(captured),1);self.assertEqual(captured[0]['status'],'waiting_approval');self.assertEqual(captured[0]['vacancy_search_profile'],profile)


class AtomicPrepareQueryTests(unittest.TestCase):
    def test_hh_prepare_calls_set_review_once_with_profile_and_never_updates_after(self):
        plan={'profile':{'leadership_intent':{'level':'unknown','evidence':['Недостаточно данных.']}},'query':'"Role"'}
        with patch.object(service,'validate_nano_settings',return_value=[]),patch.object(service,'fetch_vacancy_details',return_value={'name':'Role'}),patch.object(service,'generate_vacancy_search_plan',return_value=plan),patch.object(service,'set_review') as review,patch.object(service,'update_task') as update:
            service.prepare_query('vacancy','task','hh')
        review.assert_called_once_with('task','"Role"',vacancy_search_profile=plan['profile']);update.assert_not_called()
    def test_xray_prepare_does_not_attach_hh_profile(self):
        with patch.object(service,'validate_nano_settings',return_value=[]),patch.object(service,'fetch_vacancy_details',return_value={'name':'Role','area':{'name':'Almaty'}}),patch.object(state,'get_task',return_value={'xray_sources':[]}) as get_task,patch.object(service,'generate_xray_query',return_value=['Role']) as generate_xray,patch('xray_plan.build_plan',return_value='xray query') as build_plan,patch.object(service,'generate_vacancy_search_plan') as generate_hh_plan,patch.object(service,'set_review') as review,patch.object(service,'update_task') as update:
            service.prepare_query('vacancy','task','xray')
        get_task.assert_called_once_with('task');generate_xray.assert_called_once_with({'name':'Role','area':{'name':'Almaty'}})
        build_plan.assert_called_once();generate_hh_plan.assert_not_called()
        review.assert_called_once_with('task','xray query',vacancy_search_profile=None);update.assert_not_called()
    def test_no_post_review_transition_exists_in_prepare_query(self):
        import inspect
        source=inspect.getsource(service.prepare_query);after=source.split('set_review',1)[1]
        self.assertNotIn('update_task(',after)


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
