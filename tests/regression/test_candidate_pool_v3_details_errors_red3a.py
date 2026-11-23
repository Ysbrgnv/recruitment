"""RED-3A: V3 details continuation and propagation policy."""
import copy,unittest
from unittest.mock import patch
from app.core import settings
from app.core.errors import HHUnavailableError
from app.services import recruitment_service as service
def err(kind=None,scope=None):
 e=HHUnavailableError('x'); e.error_kind=kind; e.request_scope=scope; e.endpoint='/resumes/{id}'; return e
class Harness:
 def invoke(self,failure,items=None):
  metrics={}; cps=[]
  items=items or [{'id':'bad'},{'id':'good'}]
  def load(rid):
   if rid in {'bad','unavailable'}: raise failure
   return {'id':rid}
  with patch.object(settings,'CANDIDATE_POOL_V3_MAX_PAGES',10),patch.object(settings,'CANDIDATE_POOL_V3_SCAN_LIMIT',100),patch.object(service,'search_resumes',return_value={'items':items,'pages':1}),patch.object(service,'fetch_resume_details',side_effect=load),patch.object(service,'was_sent_to_ai',return_value=False),patch.object(service,'reset_history_cache'):
   result=service._search_candidate_pool_v3('v',{}, {},'q',20,5,metrics,lambda *a:None,pool_exclusions={},existing_pool=[],checkpoint=lambda *a:cps.append(copy.deepcopy(a)))
  return result,metrics,cps
 def continuation(self,expected):
  try:result,metrics,checkpoints=self.invoke(expected)
  except HHUnavailableError as raised:
   if raised is not expected:raise
   self.fail('V3 propagated the structured details error instead of using the generic load_failed continuation contract')
  self.assertEqual([x[0] for x in result['pool']],['good']); self.assertEqual(metrics['scanned_new_candidates'],2); self.assertEqual(metrics['load_failed'],1); self.assertTrue(checkpoints); return result,metrics,checkpoints
 def propagation(self,e,items=None):
  try:self.invoke(e,items=items)
  except HHUnavailableError as got:
   self.assertIs(got,e); return got
  self.fail('expected HHUnavailableError')
class V3DetailsRed3A(Harness,unittest.TestCase):
 def test_01_details_not_found_continues(self): self.continuation(err('not_found','details'))
 def test_02_details_malformed_continues(self): self.continuation(err('malformed_response','details'))
 def test_03_search_scope_propagates(self): self.propagation(err('not_found','search'))
 def test_04_none_scope_propagates(self): self.propagation(err('not_found',None))
 def test_05_forbidden_propagates(self): self.propagation(err('forbidden','details'))
 def test_06_timeout_propagates(self): self.propagation(err('timeout','details'))
 def test_07_structured_matches_generic_failure(self):
  structured_result,structured_metrics,structured_checkpoints=self.continuation(err('not_found','details'))
  generic_result,generic_metrics,generic_checkpoints=self.invoke(RuntimeError('x'))

  def without_guard(value):
   if isinstance(value,dict):
    return {
     key:without_guard(item)
     for key,item in value.items()
     if key!='details_failure_guard'
    }
   if isinstance(value,list):
    return [without_guard(item) for item in value]
   if isinstance(value,tuple):
    return tuple(without_guard(item) for item in value)
   return value

  self.assertEqual(
   without_guard(
    (
     structured_result,
     structured_metrics,
     structured_checkpoints,
    )
   ),
   without_guard(
    (
     generic_result,
     generic_metrics,
     generic_checkpoints,
    )
   ),
  )
 def test_08_auth_failed_propagates(self): self.propagation(err('auth_failed','details'))
 def test_09_legacy_error_propagates(self): self.propagation(HHUnavailableError('legacy'))
 def test_10_checkpoint_boundary(self):
  result,metrics,checkpoints=self.continuation(err('not_found','details'))
  self.assertEqual(metrics['load_failed'],1); self.assertEqual(metrics['scanned_new_candidates'],2); self.assertEqual([x[0] for x in result['pool']],['good']); self.assertTrue(any(x[1].get('load_failed')==1 for x in checkpoints))
  timeout=err('timeout','details'); saved_checkpoints=[]
  def save(*args): saved_checkpoints.append(copy.deepcopy(args))
  metrics2={}
  def load(rid):
   if rid=='unavailable': raise timeout
   return {'id':rid}
  with patch.object(settings,'CANDIDATE_POOL_V3_MAX_PAGES',10),patch.object(settings,'CANDIDATE_POOL_V3_SCAN_LIMIT',100),patch.object(service,'search_resumes',return_value={'items':[{'id':'saved'},{'id':'unavailable'}],'pages':1}),patch.object(service,'fetch_resume_details',side_effect=load),patch.object(service,'was_sent_to_ai',return_value=False),patch.object(service,'reset_history_cache'):
   with self.assertRaises(HHUnavailableError) as got:
    service._search_candidate_pool_v3('v',{}, {},'q',20,5,metrics2,lambda *a:None,pool_exclusions={},existing_pool=[],checkpoint=save)
  self.assertIs(got.exception,timeout); self.assertTrue(saved_checkpoints); self.assertEqual([x[0] for x in saved_checkpoints[-1][0]],['saved']); self.assertEqual(saved_checkpoints[-1][1]['scanned_new_candidates'],1)
