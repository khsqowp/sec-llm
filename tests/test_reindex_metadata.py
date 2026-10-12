"""Metadata preservation regressions; ORM uses an isolated in-memory database."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid

from sqlalchemy import create_engine
from test_audit import load_def
from src.db import Base, Database, NoteIndex


class ReindexMetadataTests(unittest.TestCase):
    def setUp(self):
        self.db = Database.__new__(Database)
        self.db.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.db.engine)
        self.addCleanup(self.db.engine.dispose)
        self.path = '/tmp/dummy_test_note.md'
        self.db.upsert_note(self.path, 'Original', 'security', ['테스트'], 'dummy.txt')

    def snapshot(self):
        with self.db.session() as session:
            note = session.query(NoteIndex).filter_by(note_path=self.path).one()
            return {column.name: getattr(note, column.name)
                    for column in NoteIndex.__table__.columns}

    def test_update_preserves_every_other_column(self):
        before = self.snapshot()
        self.db.update_qdrant_id(self.path, 'new-id')
        after = self.snapshot()
        self.assertEqual(after.pop('qdrant_id'), 'new-id')
        before.pop('qdrant_id')
        self.assertEqual(after, before)
        self.db.update_qdrant_id(self.path, 'second-id')
        self.assertEqual(self.snapshot()['tags'], json.dumps(['테스트'], ensure_ascii=False))

    def test_missing_note_is_not_created(self):
        with self.assertRaises(LookupError):
            self.db.update_qdrant_id('/tmp/not_registered.md', 'id')
        self.assertEqual(len(self.db.get_all_notes()), 1)

    def test_reindex_path_preserves_metadata(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'note.md'
            path.write_text('test content', encoding='utf-8')
            self.db.update_note_path(self.path, str(path))
            self.path = str(path)
            before = self.snapshot()
            cls, _ = load_def('services/pipeline/src/linker.py', 'Linker', uuid=uuid,
                              COLLECTION='notes', PointStruct=SimpleNamespace,
                              SparseVector=SimpleNamespace)
            linker = cls.__new__(cls)
            linker._reindex_log = lambda message: None
            linker._chunk_text = lambda content: [content]
            linker._sparse_encode = lambda content: ([1], [1.0])
            linker.llm = SimpleNamespace(_get_fastembed_model=lambda: SimpleNamespace(
                embed=lambda chunks, **kwargs: [[0.1] for chunk in chunks]))
            points = []
            linker.client = SimpleNamespace(upsert=lambda **kwargs: points.extend(kwargs['points']))
            self.assertEqual(linker.reindex_missing(self.db), 1)
            after = self.snapshot()
            self.assertEqual(after.pop('qdrant_id'), points[0].payload['note_id'])
            before.pop('qdrant_id')
            self.assertEqual(after, before)
            self.assertEqual(linker.reindex_missing(self.db), 0)
