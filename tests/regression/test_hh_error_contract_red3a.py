"""RED-3A: backwards-compatible structured HH error contract."""
import copy, inspect, pickle, unittest
from app.core.errors import HHUnavailableError, UserFacingError
FIELDS=('error_kind','request_scope','endpoint','status_code','retry_after')
def field(exc,name): return getattr(exc,name,None)
def make_structured(test,*args,**kwargs):
 try:return HHUnavailableError(*args,**kwargs)
 except TypeError as exc:test.fail('HHUnavailableError does not support the required structured keyword-only contract: '+str(exc))
class HHErrorContractRed3A(unittest.TestCase):
 def test_01_is_user_facing(self): self.assertTrue(issubclass(HHUnavailableError,UserFacingError))
 def test_02_message_args_and_str(self):
  e=HHUnavailableError('message'); self.assertEqual(e.args,('message',)); self.assertEqual(str(e),'message')
 def test_03_empty_constructor(self): self.assertEqual(HHUnavailableError().args,())
 def test_04_metadata_defaults_none(self):
  exc=HHUnavailableError('x')
  for name in FIELDS:
   with self.subTest(name=name):
    self.assertTrue(hasattr(exc,name),f'HHUnavailableError instance is missing {name}')
    self.assertIsNone(getattr(exc,name))
 def test_05_metadata_keyword_only(self):
  try:p=inspect.signature(HHUnavailableError).parameters
  except (TypeError,ValueError) as exc:self.fail('HHUnavailableError does not expose the required structured signature: '+str(exc))
  for n in FIELDS:
   self.assertIn(n,p); self.assertEqual(p[n].kind,inspect.Parameter.KEYWORD_ONLY)
 def test_06_valid_metadata_preserved(self):
  e=make_structured(self,'x',error_kind='timeout',request_scope='search',endpoint='/resumes',status_code=503,retry_after=7)
  self.assertEqual(tuple(field(e,n) for n in FIELDS),('timeout','search','/resumes',503,7))
 def test_07_unknown_error_kind_none(self): self.assertIsNone(field(make_structured(self,'x',error_kind='other'),'error_kind'))
 def test_08_unknown_scope_none(self): self.assertIsNone(field(make_structured(self,'x',request_scope='other'),'request_scope'))
 def test_09_unknown_endpoint_none(self): self.assertIsNone(field(make_structured(self,'x',endpoint='/resumes/secret'),'endpoint'))
 def test_10_status_accepts_range(self):
  for v in (100,200,599): self.assertEqual(field(make_structured(self,'x',status_code=v),'status_code'),v)
 def test_11_status_rejects_bool(self):
  for v in (True,False): self.assertIsNone(field(make_structured(self,'x',status_code=v),'status_code'))
 def test_12_status_rejects_invalid(self):
  for v in (99,600,-1,200.0,'200',None): self.assertIsNone(field(make_structured(self,'x',status_code=v),'status_code'))
 def test_13_retry_after_accepts_nonnegative_int(self):
  for v in (0,7,30): self.assertEqual(field(make_structured(self,'x',retry_after=v),'retry_after'),v)
 def test_14_retry_after_rejects_invalid(self):
  for v in (True,False,-1,1.5,'7'): self.assertIsNone(field(make_structured(self,'x',retry_after=v),'retry_after'))
 def test_15_copy_and_deepcopy(self):
  e=make_structured(self,'x',error_kind='timeout',request_scope='search',endpoint='/resumes',status_code=503,retry_after=7)
  for c in (copy.copy(e),copy.deepcopy(e)): self.assertEqual(tuple(field(c,n) for n in FIELDS),tuple(field(e,n) for n in FIELDS))
 def test_16_pickle_round_trip(self):
  e=make_structured(self,'x',error_kind='network_error',request_scope='details',endpoint='/resumes/{id}',status_code=502,retry_after=0); c=pickle.loads(pickle.dumps(e))
  self.assertEqual((c.args,tuple(field(c,n) for n in FIELDS)),(e.args,tuple(field(e,n) for n in FIELDS)))
