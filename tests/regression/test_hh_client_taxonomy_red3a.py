"""RED-3A: offline HH client taxonomy, scopes, safe endpoints and Retry-After."""
import unittest
from unittest.mock import Mock, call, patch
import requests
from app.core.errors import HHUnavailableError
from app.integrations.hh import hh_client as hh
F=('error_kind','request_scope','endpoint','status_code','retry_after')
def val(e,n): return getattr(e,n,None)
def response(code,header=None,payload=None):
 r=Mock(); r.status_code=code; r.headers={} if header is None else {'Retry-After':header}; r.json.return_value=payload or {}
 if code>=400: r.raise_for_status.side_effect=requests.HTTPError('http',response=r)
 return r
def capture(test,callable):
 try: callable()
 except HHUnavailableError as e:return e
 except Exception as e:test.fail('expected HHUnavailableError, got '+type(e).__name__)
 test.fail('expected HHUnavailableError')
def invoke(test,endpoint='/resumes',scope=None,sequence=None,retries=0):
 with patch.object(hh,'reload_saved_tokens'),patch.object(hh.requests,'get',side_effect=sequence or [response(500)]) as get,patch.object(hh.time,'sleep') as sleep:
  e=capture(test,lambda:hh.hh_get(endpoint,retries=retries,request_scope=scope))
 return e,get,sleep
def assertmeta(test,e,kind,scope,endpoint,status=None,retry=None): test.assertEqual(tuple(val(e,n) for n in F),(kind,scope,endpoint,status,retry))
class HHTaxonomyRed3A(unittest.TestCase):
 def wrapper(self,name,args,scope):
  with patch.object(hh,'hh_get',side_effect=HHUnavailableError('x')) as get:
   capture(self,lambda:getattr(hh,name)(*args))
  self.assertEqual(get.call_args.kwargs.get('request_scope'),scope)
 def test_01_search_scope(self): self.wrapper('search_resumes',({},),'search')
 def test_02_resume_details_scope(self): self.wrapper('fetch_resume_details',('resume-sensitive-id',),'details')
 def test_03_vacancy_details_scope(self): self.wrapper('fetch_vacancy_details',('123',),'vacancy')
 def test_04_active_vacancies_scope(self): self.wrapper('fetch_active_vacancies',('vacancy-sensitive-id',),'vacancy')
 def endpoint(self,path,safe):
  e,_,_=invoke(self,path,'search'); self.assertEqual(val(e,'endpoint'),safe)
 def test_05_safe_resumes(self): self.endpoint('/resumes','/resumes')
 def test_06_safe_resume_id(self): self.endpoint('/resumes/resume-sensitive-id','/resumes/{id}')
 def test_07_safe_vacancies(self): self.endpoint('/vacancies','/vacancies')
 def test_08_safe_vacancy_id(self): self.endpoint('/vacancies/vacancy-sensitive-id','/vacancies/{id}')
 def test_09_unknown_endpoint_none(self): self.endpoint('/me?token=synthetic-access-token',None)
 def network(self,error,kind):
  e,_,_=invoke(self,sequence=[error]); self.assertEqual(val(e,'error_kind'),kind); self.assertIs(e.__cause__,error)
 def test_10_connect_timeout(self): self.network(requests.ConnectTimeout('x'),'timeout')
 def test_11_timeout(self): self.network(requests.Timeout('x'),'timeout')
 def test_12_connection_error(self): self.network(requests.ConnectionError('x'),'network_error')
 def test_13_network_cause_chain(self):
  for x,k in ((requests.Timeout('t'),'timeout'),(requests.ConnectionError('n'),'network_error')):
   with self.subTest(k=k): self.network(x,k)
 def http(self,code,kind):
  e,_,_=invoke(self,sequence=[response(code)]); self.assertEqual((val(e,'error_kind'),val(e,'status_code')),(kind,code)); return e
 def test_14_rate_limited(self): self.assertIsNone(self.http(429,'rate_limited').__cause__)
 def test_15_retryable_5xx_exhaustion(self):
  for c in (500,502,503,504):
   with self.subTest(c=c): self.assertIsNone(self.http(c,'server_error').__cause__)
 def test_16_other_5xx_server_error(self):
  for c in (501,505,599):
   with self.subTest(c=c): self.assertIsNotNone(self.http(c,'server_error').__cause__)
 def test_17_not_found(self):
  for c in (404,410):
   with self.subTest(c=c): self.assertIsNotNone(self.http(c,'not_found').__cause__)
 def test_18_generic_4xx(self): self.assertIsNotNone(self.http(400,'client_error').__cause__)
 def test_19_forbidden(self): self.assertIsNone(self.http(403,'forbidden').__cause__)
 def test_20_failed_refresh(self):
  with patch.object(hh,'reload_saved_tokens'),patch.object(hh,'recover_auth',return_value=False),patch.object(hh.requests,'get',return_value=response(401)),patch.object(hh.time,'sleep'):
   e=capture(self,lambda:hh.hh_get('/resumes',request_scope='search'))
  self.assertEqual(val(e,'error_kind'),'auth_failed')
 def test_21_auth_revoked(self):
  payload={'errors':[{'type':'oauth','value':'token_revoked'}]}
  with patch.object(hh,'reload_saved_tokens'),patch.object(hh.requests,'get',return_value=response(403,payload=payload)),patch.object(hh.time,'sleep'):
   e=capture(self,lambda:hh.hh_get('/resumes',request_scope='search'))
  self.assertEqual(val(e,'error_kind'),'auth_revoked')
 def test_22_auth_disabled(self):
  with patch.dict(hh.os.environ,{'HH_INTERACTIVE_AUTH':'false'}): e=capture(self,hh.authorize_from_scratch)
  self.assertEqual((val(e,'error_kind'),val(e,'request_scope'),val(e,'endpoint')),('auth_disabled','auth','/oauth/token'))
 def test_23_retry_after_matrix(self):
  cases=[('7',7,7),('120',30,30),('0',0,0),('-1',0,None),('1.5',2,None),('Wed, 21 Oct 2015 07:28:00 GMT',2,None),('junk',2,None),(None,2,None)]
  for status in (429,503):
   for raw,delay,meta in cases:
    with self.subTest(status=status,raw=raw):
     e,get,sleep=invoke(self,scope='search',sequence=[response(status,raw),response(status,raw)],retries=1)
     self.assertEqual(val(e,'retry_after'),meta); self.assertEqual(sleep.call_args_list,[call(delay)]); self.assertEqual(get.call_count,2)
     for secret in ('resume-sensitive-id','vacancy-sensitive-id','secret-query-value','synthetic-access-token'):
      self.assertNotIn(secret,str(e)); self.assertNotIn(secret,repr(e)); self.assertNotIn(secret,repr(e.args)); self.assertNotEqual(val(e,'endpoint'),secret)
