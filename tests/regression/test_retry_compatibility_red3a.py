"""RED-3A: job retry policy remains cause-chain based."""
import inspect,unittest,requests,psycopg,psycopg_pool
from app.core.errors import HHUnavailableError
from app.core.retry_policy import transient
class RetryCompatibilityRed3A(unittest.TestCase):
 def caused(self,cause,kind):
  e=HHUnavailableError('x'); e.error_kind=kind
  try: raise e from cause
  except HHUnavailableError as got:return got
 def test_01_timeout_cause_transient(self): self.assertTrue(transient(self.caused(requests.Timeout(),'timeout')))
 def test_02_network_cause_transient(self): self.assertTrue(transient(self.caused(requests.ConnectionError(),'network_error')))
 def test_03_rate_limited_without_cause_not_transient(self):
  e=HHUnavailableError('x'); e.error_kind='rate_limited'; self.assertFalse(transient(e))
 def test_04_final_server_without_cause_not_transient(self):
  e=HHUnavailableError('x'); e.error_kind='server_error'; self.assertFalse(transient(e))
 def test_05_http_error_causes_nontransient(self):
  for code in (400,404,410,501):
   r=requests.Response(); r.status_code=code
   with self.subTest(code=code): self.assertFalse(transient(self.caused(requests.HTTPError(response=r),'client_error')))
 def test_06_metadata_does_not_change_transient(self):
  e=HHUnavailableError('x'); e.error_kind='rate_limited'; e.retry_after=30; self.assertFalse(transient(e))
  source=inspect.getsource(__import__('app.core.retry_policy',fromlist=['x'])); self.assertNotIn('error_kind',source); self.assertNotIn('retryable',source)
