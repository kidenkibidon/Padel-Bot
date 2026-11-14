"""One-time migration into an empty Turso database. Stop the bot first."""
import argparse
import os
import sqlite3
from pathlib import Path

from bot import load_env
from storage import Connection


def migrate(source, url, token):
    if not url or not token:
        raise SystemExit("Задайте TURSO_DATABASE_URL и TURSO_AUTH_TOKEN.")
    if not Path(source).is_file():
        raise SystemExit("Исходная SQLite-база не найдена.")
    src = sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)
    dst = Connection(url, token)
    try:
        src.execute('BEGIN')
        dst.execute('BEGIN IMMEDIATE')
        if dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
            raise SystemExit("Целевая база не пустая. Перенос отменён, данные не изменены.")
        # Recreate all source tables, indexes and sequence values in one transaction.
        for statement in src.iterdump():
            if statement.strip() in ('BEGIN TRANSACTION;', 'COMMIT;'):
                continue
            dst.execute(statement)
        for (name,) in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            quoted = '"' + name.replace('"', '""') + '"'
            expected = src.execute('SELECT count(*) FROM ' + quoted).fetchone()[0]
            actual = dst.execute('SELECT count(*) FROM ' + quoted).fetchone()[0]
            if actual != expected:
                raise RuntimeError('Не совпало количество строк: ' + name)
        if dst.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Обнаружены нарушенные связи данных.')
        dst.commit()
        print('Перенос завершён, количество строк проверено. Исходный файл сохранён.')
    except BaseException:
        dst.rollback()
        raise
    finally:
        src.close()
        dst.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('--env', default='.env')
    args = parser.parse_args()
    load_env(args.env)
    migrate(args.source, os.environ.get('TURSO_DATABASE_URL'), os.environ.get('TURSO_AUTH_TOKEN'))
