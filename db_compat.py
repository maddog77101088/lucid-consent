"""
db_compat.py — psycopg3 wrapper that mimics sqlite3's API.
Usage in app.py: import db_compat as sqlite3
"""
import os
import re
import psycopg
from psycopg.rows import dict_row

IntegrityError = psycopg.errors.IntegrityError
OperationalError = psycopg.errors.OperationalError

_PRAGMA_TABLE_INFO_SQL = """
    SELECT ordinal_position - 1 AS cid,
           column_name          AS name,
           data_type            AS type,
           CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull,
           column_default       AS dflt_value,
           0                    AS pk
    FROM information_schema.columns
    WHERE table_name = %s AND table_schema = 'public'
    ORDER BY ordinal_position
"""

def _to_pg_schema(sql):
    """Convert SQLite DDL to PostgreSQL-compatible DDL."""
    sql = re.sub(r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT',
                 'SERIAL PRIMARY KEY', sql, flags=re.IGNORECASE)
    sql = re.sub(r"datetime\('now'[^)]*\)", "current_timestamp::text", sql, flags=re.IGNORECASE)
    return sql

def _to_pg_params(sql):
    """Replace ? placeholders with %s for psycopg."""
    return sql.replace('?', '%s')

class Row:
    """Dict-like row supporting both row['col'] and row[0] access."""
    def __init__(self, data):
        self._data = dict(data)
        self._keys = list(self._data.keys())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[self._keys[key]]
        return self._data[key]

    def __iter__(self):
        return iter(self._data.values())

    def keys(self):
        return self._keys

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __contains__(self, key):
        return key in self._data

    def __len__(self):
        return len(self._data)

class CursorWrapper:
    def __init__(self, raw_cur):
        self._cur = raw_cur
        self.lastrowid = None

    def execute(self, sql, params=()):
        # Handle PRAGMA table_info(X) -> PostgreSQL information_schema query
        pragma_m = re.match(r'\s*PRAGMA\s+table_info\((\w+)\)\s*$', sql, re.IGNORECASE)
        if pragma_m:
            table_name = pragma_m.group(1)
            self._cur.execute(_PRAGMA_TABLE_INFO_SQL, (table_name,))
            return self

        sql = _to_pg_schema(sql)
        sql = _to_pg_params(sql)
        self.lastrowid = None
        is_insert = bool(re.match(r'\s*INSERT\s+INTO\s+', sql, re.IGNORECASE))
        if is_insert and 'RETURNING' not in sql.upper():
            sql = sql.rstrip().rstrip(';') + ' RETURNING id'
            self._cur.execute(sql, params if params else None)
            row = self._cur.fetchone()
            if row:
                vals = list(row.values())
                self.lastrowid = vals[0] if vals else None
        else:
            self._cur.execute(sql, params if params else None)
        return self

    def executemany(self, sql, params_list):
        self._cur.executemany(_to_pg_params(sql), params_list)

    def executescript(self, sql):
        """Execute multiple SQL statements (SQLite compatibility)."""
        sql = _to_pg_schema(sql)
        for stmt in sql.split(';'):
            stmt = stmt.strip()
            if stmt:
                self._cur.execute(stmt)

    def fetchone(self):
        row = self._cur.fetchone()
        return Row(row) if row else None

    def fetchall(self):
        return [Row(r) for r in (self._cur.fetchall() or [])]

    @property
    def rowcount(self):
        return self._cur.rowcount

    def __iter__(self):
        for row in self._cur:
            yield Row(row)

class ConnectionWrapper:
    def __init__(self, conn):
        self._conn = conn
        self.row_factory = None

    def cursor(self):
        raw = self._conn.cursor(row_factory=dict_row)
        return CursorWrapper(raw)

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *args):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        self._conn.close()

def connect(path=None):
    """Connect to PostgreSQL using DATABASE_URL. Ignores path (SQLite compat)."""
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL environment variable is not set')
    conn = psycopg.connect(url)
    return ConnectionWrapper(conn)
