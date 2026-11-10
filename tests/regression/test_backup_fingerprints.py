"""Test fingerprint algorithm in isolation; no claim of a live PostgreSQL restore."""
import ast,hashlib,uuid,unittest
from pathlib import Path
class SQL:
    def __init__(self,text):self.text=text
    def format(self,*a):return self
class SQLModule:
    SQL=SQL
    @staticmethod
    def Identifier(x):return x
class Cursor:
    def __init__(self,rows):self.rows=rows
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def execute(self,*a):pass
    def __iter__(self):return iter((v,) for v in sorted(self.rows))
class Connection:
    def __init__(self,rows):self.rows=rows
    def execute(self,*a):
        class Result:
            def fetchall(self):return [('history',)]
        return Result()
    def cursor(self,**kw):return Cursor(self.rows)
class FingerprintTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).resolve().parents[2]/'scripts'/'backup'/'verify.py'
        node=next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='content_fingerprints')
        env={'hashlib':hashlib,'uuid':uuid,'sql':SQLModule}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),env);self.f=env['content_fingerprints']
    def test_order_independent(self):self.assertEqual(self.f(Connection(['a','b'])),self.f(Connection(['b','a'])))
    def test_same_count_different_content_detected(self):self.assertNotEqual(self.f(Connection(['a','b'])),self.f(Connection(['a','c'])))
    def test_empty_table_still_fingerprinted(self):self.assertEqual(self.f(Connection([]))['history']['rows'],0)
if __name__=='__main__':unittest.main()

