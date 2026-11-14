"""Run the behavior suite through the official libSQL adapter, without cloud credentials."""
from contextlib import contextmanager
from unittest.mock import patch
import test_workflows as workflows
from core import Store
from storage import Connection


class LibsqlWorkflowTests(workflows.WorkflowTests):
    def setUp(self):
        @contextmanager
        def driver_db(store):
            c=Connection(str(store.path))
            try:
                c.execute('PRAGMA busy_timeout=30000')
                c.execute('PRAGMA foreign_keys=ON')
                c.execute('BEGIN IMMEDIATE')
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise
            finally:
                c.close()
        patcher=patch.object(Store,'db',driver_db)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
