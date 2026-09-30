import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('snapshot', Path(__file__).with_name('sqlite_snapshot.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        for name in m.DATABASES:
            with sqlite3.connect(self.source / name) as db:
                db.execute('CREATE TABLE evidence(value TEXT)')
                db.execute('INSERT INTO evidence VALUES (?)', ('secret-sentinel-not-in-manifest',))
        self.target = self.root / 'backup'

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        before = [m.digest(self.source / n) for n in m.DATABASES]
        manifest = m.snapshot(self.source, self.target)
        self.assertEqual(m.verify(self.target), manifest)
        self.assertEqual(before, [m.digest(self.source / n) for n in m.DATABASES])
        self.assertNotIn('secret-sentinel', (self.target / 'snapshot.json').read_text())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o700)
        for name in m.DATABASES:
            self.assertEqual((self.target / name).stat().st_mode & 0o777, 0o600)

    def test_refuses_existing_target(self):
        self.target.mkdir()
        with self.assertRaises(FileExistsError):
            m.snapshot(self.source, self.target)

    def test_refuses_missing_source(self):
        (self.source / 'users.db').unlink()
        with self.assertRaises(ValueError):
            m.snapshot(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_refuses_nested_target(self):
        with self.assertRaises(ValueError):
            m.snapshot(self.source, self.source / 'backup')

    def test_refuses_source_symlink(self):
        (self.source / 'control.db').rename(self.source / 'real.db')
        (self.source / 'control.db').symlink_to(self.source / 'real.db')
        with self.assertRaises(ValueError):
            m.snapshot(self.source, self.target)

    def test_tamper_detected(self):
        m.snapshot(self.source, self.target)
        with sqlite3.connect(self.target / 'users.db') as db:
            db.execute("INSERT INTO evidence VALUES ('changed')")
        with self.assertRaises(ValueError):
            m.verify(self.target)

    def test_manifest_path_traversal_refused(self):
        m.snapshot(self.source, self.target)
        path = self.target / 'snapshot.json'
        path.write_text(path.read_text().replace('control.db', '../control.db'))
        with self.assertRaises(ValueError):
            m.verify(self.target)

    def test_wal_committed_data_copied(self):
        db = sqlite3.connect(self.source / 'control.db')
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute("INSERT INTO evidence VALUES ('wal-data')")
            db.commit()
            m.snapshot(self.source, self.target)
            with sqlite3.connect(self.target / 'control.db') as copied:
                self.assertEqual(copied.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 2)
        finally:
            db.close()

    def test_committed_wal_tamper_rejected(self):
        source = sqlite3.connect(self.source / 'control.db')
        try:
            source.execute('PRAGMA journal_mode=WAL')
            source.execute("INSERT INTO evidence VALUES ('source-wal')")
            source.commit()
            m.snapshot(self.source, self.target)
        finally:
            source.close()
        conn = sqlite3.connect(self.target / 'control.db')
        try:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone(), ('delete',))
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute("INSERT INTO evidence VALUES ('tampered-wal')")
            conn.commit()
            with self.assertRaises(ValueError):
                m.verify(self.target)
        finally:
            conn.close()

    def test_sidecar_rejected(self):
        m.snapshot(self.source, self.target)
        (self.target / 'control.db-wal').write_bytes(b'')
        with self.assertRaises(ValueError):
            m.verify(self.target)

    def test_dangling_sidecar_rejected(self):
        m.snapshot(self.source, self.target)
        (self.target / 'control.db-wal').symlink_to(self.target / 'missing')
        with self.assertRaises(ValueError):
            m.verify(self.target)

    def test_malformed_manifest_rejected(self):
        m.snapshot(self.source, self.target)
        (self.target / 'snapshot.json').write_text('[]')
        with self.assertRaises(ValueError):
            m.verify(self.target)


if __name__ == '__main__':
    unittest.main()
