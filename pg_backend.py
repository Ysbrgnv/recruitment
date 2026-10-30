"""PostgreSQL adapter preserving the legacy transaction API.
BEGIN IMMEDIATE uses a transaction advisory lock, matching SQLite's single writer.
Queue claims use separate row locks and do not take this compatibility lock.
"""
import re
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row

class Row(dict):
    def __getitem__(self,key):
        return list(self.values())[key] if isinstance(key,int) else super().__getitem__(key)

class Cursor:
    def __init__(self,cursor):self.cursor=cursor
    def fetchone(self):
        row=self.cursor.fetchone();return Row(row) if row is not None else None
    def fetchall(self):return [Row(row) for row in self.cursor.fetchall()]
    def __iter__(self):return iter(self.fetchall())
    @property
    def rowcount(self):return self.cursor.rowcount

class Connection:
    def __init__(self,raw):self.raw=raw
    def execute(self,sql,params=()):
        if sql.strip().upper().startswith('PRAGMA'):
            return Cursor(self.raw.execute('SELECT 1 WHERE FALSE'))
        if sql.strip().upper()=='BEGIN IMMEDIATE':
            return Cursor(self.raw.execute('SELECT pg_advisory_xact_lock(714202601)'))
        if 'sqlite_master' in sql:
            sql="SELECT tablename AS name FROM pg_tables WHERE schemaname=current_schema()"
        # Existing queries use question-mark parameters, never literal question marks.
        sql=sql.replace('?', '%s')
        return Cursor(self.raw.execute(sql,params))
    def executescript(self,sql):
        self.raw.execute('SELECT pg_advisory_xact_lock(714202602)')
        sql=re.sub(r'\bREAL\b','DOUBLE PRECISION',sql,flags=re.I)
        sql=sql.replace('id INTEGER PRIMARY KEY','id BIGSERIAL PRIMARY KEY')
        for statement in sql.split(';'):
            if statement.strip() and not statement.strip().upper().startswith('DROP INDEX'):
                self.raw.execute(statement)

@contextmanager
def database(url):
    from db_pool import connection
    from transaction_scope import pg_connection,bind
    raw=pg_connection.get()
    if raw is not None:
        yield Connection(raw)
        return
    with connection(url) as raw:
        with bind(pg_connection,raw):yield Connection(raw)

