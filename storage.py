"""Small DB-API adapter for the official libsql driver (no local replica)."""
import sqlite3
from collections.abc import Mapping


class Row(Mapping):
    def __init__(self, columns, values):
        self.columns, self.values = columns, values

    def __getitem__(self, key):
        return self.values[key if isinstance(key, int) else self.columns.index(key)]

    def __iter__(self):
        return iter(self.columns)

    def __len__(self):
        return len(self.columns)


class Cursor:
    def __init__(self, cursor):
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        self.description = cursor.description
        columns = [c[0] for c in (cursor.description or [])]
        self.rows = iter([Row(columns, r) for r in (cursor.fetchall() or [])])

    def __iter__(self):
        return self.rows

    def fetchone(self):
        return next(self.rows, None)

    def fetchall(self):
        return list(self.rows)


class Connection:
    def __init__(self, url, token=None):
        import libsql
        self.raw = libsql.connect(url, isolation_level=None, **({"auth_token": token} if token else {}))

    def execute(self, sql, args=()):
        return Cursor(self.raw.execute(sql, args))

    def executemany(self, sql, rows):
        result = None
        for row in rows:
            result = self.execute(sql, row)
        return result

    def executescript(self, script):
        # Our schema and SQLite dumps contain complete SQL statements. Preserve
        # semicolons inside quoted strings instead of splitting on every ';'.
        statement = ''
        for char in script:
            statement += char
            if char == ';' and sqlite3.complete_statement(statement):
                if 'PRAGMA journal_mode' not in statement:
                    self.execute(statement)
                statement = ''
        if statement.strip():
            self.execute(statement)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()
