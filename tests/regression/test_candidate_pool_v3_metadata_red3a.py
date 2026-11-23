"""RED-3A: strict malformed V3 search response contracts, offline."""
import copy,json,unittest
from unittest.mock import patch
import requests
from app.core import settings
from app.core.errors import HHUnavailableError
from app.services import recruitment_service as service
def expect_hh_error(test,call):
 try: call()
 except HHUnavailableError as e:return e
 except Exception as e:test.fail('expected HHUnavailableError, got '+type(e).__name__+': '+str(e))
 test.fail('expected HHUnavailableError')
def meta(test,e): test.assertEqual((getattr(e,'error_kind',None),getattr(e,'request_scope',None),getattr(e,'endpoint',None),getattr(e,'status_code',None)),('malformed_response','search','/resumes',None))
class Harness:
 def invoke(self,values):
  self.metrics={}; self.checkpoints=[]; self.progress_calls=[]; self.search_calls=[]; self.fetch_calls=[]
  seq=iter(values)
  def search(params):
   self.search_calls.append(copy.deepcopy(params)); v=next(seq)
   if isinstance(v,BaseException): raise v
   return copy.deepcopy(v)
  def fetch(resume_id): self.fetch_calls.append(resume_id); return {'id':resume_id}
  with patch.object(settings,'CANDIDATE_POOL_V3_MAX_PAGES',10),patch.object(settings,'CANDIDATE_POOL_V3_SCAN_LIMIT',100),patch.object(service,'search_resumes',side_effect=search),patch.object(service,'fetch_resume_details',side_effect=fetch),patch.object(service,'was_sent_to_ai',return_value=False),patch.object(service,'reset_history_cache'):
   result=service._search_candidate_pool_v3('v',{}, {},'q',20,5,self.metrics,lambda *a:self.progress_calls.append(copy.deepcopy(a)),pool_exclusions={},existing_pool=[],checkpoint=lambda *a:self.checkpoints.append(copy.deepcopy(a)))
  return result,self.metrics,self.checkpoints,self.progress_calls
class V3MetadataRed3A(Harness,unittest.TestCase):
 def malformed(self,payload):
  box={}; e=expect_hh_error(self,lambda:box.setdefault('x',self.invoke([payload])))
  meta(self,e); self.assertNotIn('x',box); return e
 def test_01_response_not_dict(self): self.malformed([])
 def test_02_items_missing(self): self.malformed({'pages':1})
 def test_03_items_none(self): self.malformed({'items':None,'pages':1})
 def test_04_items_tuple(self): self.malformed({'items':(),'pages':1})
 def test_05_items_dict(self): self.malformed({'items':{},'pages':1})
 def test_06_items_string(self): self.malformed({'items':'x','pages':1})
 def test_07_pages_missing(self): self.malformed({'items':[]})
 def test_08_pages_none(self): self.malformed({'items':[],'pages':None})
 def test_09_pages_bool(self): self.malformed({'items':[],'pages':True})
 def test_10_pages_string(self): self.malformed({'items':[],'pages':'1'})
 def test_11_pages_float(self): self.malformed({'items':[],'pages':1.0})
 def test_12_pages_negative(self): self.malformed({'items':[],'pages':-1})
 def test_13_zero_pages_empty_items_valid(self):
  r,m,c,p=self.invoke([{'items':[],'pages':0}]); self.assertEqual(m['pages_fetched'],1); self.assertEqual(len(p),1); self.assertEqual(r['stop_reason'],'search_exhausted'); self.assertIs(r['pool_complete'],True); self.assertEqual(r['pool'],[])
 def test_14_zero_pages_nonempty_items_are_processed_before_exhaustion(self):
  r,m,c,p=self.invoke([{'items':[{'id':'a'}],'pages':0}]); self.assertEqual([x[0] for x in r['pool']],['a']); self.assertEqual(self.fetch_calls,['a']); self.assertEqual(m['pages_fetched'],1); self.assertEqual(m['scanned_new_candidates'],1); self.assertEqual(len(p),1); self.assertEqual(r['stop_reason'],'search_exhausted'); self.assertEqual(r['metrics']['stop_reason'],'search_exhausted'); self.assertIs(r['pool_complete'],True)
 def test_15_search_json_decode_error(self):
  for e in (json.JSONDecodeError('x','x',0),requests.exceptions.JSONDecodeError('x','x',0)):
   with self.subTest(t=type(e).__name__): meta(self,expect_hh_error(self,lambda:self.invoke([e])))
 def test_16_second_page_preserves_checkpoint(self):
  error=json.JSONDecodeError('x','x',0); e=expect_hh_error(self,lambda:self.invoke([{'items':[{'id':'a'}],'pages':2},error])); meta(self,e)
  self.assertEqual([x['page'] for x in self.search_calls],[0,1]); self.assertEqual(len(self.progress_calls),1); self.assertEqual(self.progress_calls[0][2]['pages_fetched'],1); self.assertTrue(self.checkpoints); self.assertEqual([x[0] for x in self.checkpoints[-1][0]],['a']); self.assertEqual(self.checkpoints[-1][1]['pages_fetched'],1); self.assertNotIn('stop_reason',self.checkpoints[-1][1]); self.assertEqual(self.metrics['pages_fetched'],1); self.assertEqual(self.fetch_calls,['a']); self.assertTrue(all(snapshot[1].get('pages_fetched',0)<=1 for snapshot in self.checkpoints))
 def test_17_invalid_url_propagates(self):
  with self.assertRaises(requests.exceptions.InvalidURL): self.invoke([requests.exceptions.InvalidURL('bad')])
 def test_18_plain_value_error_propagates(self):
  with self.assertRaises(ValueError): self.invoke([ValueError('plain')])
