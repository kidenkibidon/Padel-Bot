import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import Store
from storage import Connection
from migrate_turso import migrate
from bot import App
from test_bot import FakeAPI


class StorageTests(unittest.TestCase):
    def test_driver_transactions_restart_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / 'cloud.db')
            with patch('storage.Connection', side_effect=lambda url, token: Connection(target)):
                s = Store('unused.db', turso_url='libsql://test', turso_token='test')
                s.touch_user({'id': 1, 'first_name': 'Test'}, True)
                s.session(1, {'kind': 'wizard', 'data': {'photo': 'telegram-id'}})
                with self.assertRaises(RuntimeError):
                    with s.db() as c:
                        c.execute("UPDATE users SET full_name='lost' WHERE id=1")
                        raise RuntimeError('rollback')
                self.assertEqual(s.user(1)['full_name'], '')
                s = Store('unused.db', turso_url='libsql://test', turso_token='test')
                self.assertEqual(s.session(1)['data']['photo'], 'telegram-id')
                backup = str(Path(tmp) / 'backup.db')
                s.backup(backup)
                restored = Store(backup)
                self.assertEqual(restored.session(1), s.session(1))

    def test_migration_and_nonempty_target_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = [str(Path(tmp) / n) for n in ('source.db', 'target.db')]
            s = Store(source)
            s.touch_user({'id': 42, 'first_name': 'Игрок'}, True)
            s.session(42, {'kind': 'wizard', 'data': {'photo': 'photo42'}})
            with patch('migrate_turso.Connection', side_effect=lambda url, token: Connection(target)):
                migrate(source, 'libsql://test', 'test')
                with self.assertRaises(SystemExit):
                    migrate(source, 'libsql://test', 'test')
            self.assertEqual(Store(target).session(42), s.session(42))

    def test_template_snapshot_and_photo_size_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Store(Path(tmp) / 'source.db')
            cid = s.save_court(999, dict(court='Club', location='https://maps.example/club', photo='same-photo'))
            data = s.court_data(cid)
            eid = s.save_event(999, data)
            data['court'] = 'Different'
            s.save_court(999, data)
            self.assertEqual(s.event(eid)['court'], 'Club')
            self.assertEqual(s.event(eid)['photo'], 'same-photo')
            app = App(s, FakeAPI(), owner=999, group=-123)
            metadata = {}
            result = app.parse_field('photo', {'photo': [
                dict(file_id='small', file_unique_id='unique', file_size=100000, width=1000, height=800),
                dict(file_id='large', file_size=5000000, width=3000, height=2000)]}, metadata)
            self.assertEqual(result, 'small')
            self.assertEqual(metadata['photo_meta']['file_size'], 100000)


if __name__ == '__main__':
    unittest.main()
