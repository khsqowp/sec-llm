"""Offline audit regressions. Run: python3 -m unittest discover -s tests -v.

Vault/config tests import the real modules. Service-dependent functions/classes
are compiled from their actual AST and run against explicit fakes; these tests
are not Docker, FastAPI integration, or model-inference tests.
"""
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import logging
from pathlib import Path
import re
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/pipeline'))
from src.config import Config
from src.vault import VaultWriter
from src.new_files import write_new_file, write_new_files
from src.moc import MocWriter
from src.activity import ActivityLogger
from src.mover import FileMover


def load_def(file, name, **namespace):
    """Load the complete real definition without importing network/model SDKs."""
    tree = ast.parse((ROOT / file).read_text())
    node = next(n for n in tree.body if getattr(n, 'name', None) == name)
    node.decorator_list = []
    ns = dict(Path=Path, logging=logging, logger=logging.getLogger('audit'),
              asyncio=asyncio, datetime=datetime, re=re, **namespace)
    exec(compile('from __future__ import annotations\n' + ast.unparse(node), str(file), 'exec'), ns)
    return ns[name], ns


class FilePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config(vault_path=self.root / 'vault', storage_path=self.root / 'storage')
        self.cfg.vault_path.mkdir()

    def test_same_title_keeps_original(self):
        writer = VaultWriter(self.cfg)
        first = writer.write_individual_note('Title', 'general', 'old', 'a')[0]
        original = first.read_bytes()
        second = writer.write_individual_note('Title', 'general', 'new', 'b')[0]
        self.assertEqual(second.stem, 'Title_1')
        self.assertEqual(first.read_bytes(), original)
        self.assertIn('new', second.read_text())

    def test_merged_notes_keep_original(self):
        writer = VaultWriter(self.cfg)
        first = writer.write_merged_note('Merged', 'general', 'old', ['A'])[0]
        original = first.read_bytes()
        second = writer.write_merged_note('Merged', 'general', 'new', ['B'])[0]
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), original)

    def test_part_links_use_actual_collision_names(self):
        self.cfg.settings = {'processing': {'max_words_per_note': 2}}
        writer = VaultWriter(self.cfg)
        old = writer.write_individual_note('Title', 'general', 'one two\nthree four', 'a')
        original = [p.read_bytes() for p in old]
        new = writer.write_individual_note('Title', 'general', 'five six\nseven eight', 'b')
        self.assertEqual(len(new), 2)
        self.assertIn(f'[[{new[1].stem}|Part2', new[0].read_text())
        self.assertIn(f'[[{new[0].stem}|← Part1', new[1].read_text())
        self.assertEqual([p.read_bytes() for p in old], original)

    def test_concurrent_creation_is_exclusive(self):
        path = self.root / 'same.md'
        with ThreadPoolExecutor(max_workers=8) as executor:
            paths = list(executor.map(lambda i: write_new_file(path, str(i)), range(20)))
        self.assertEqual(len(set(paths)), 20)
        self.assertEqual({p.read_text() for p in paths}, {str(i) for i in range(20)})

    def test_symlink_collision_keeps_target(self):
        target = self.root / 'original.md'
        target.write_text('original')
        alias = self.root / 'alias.md'
        alias.symlink_to(target)
        created = write_new_file(alias, 'new')
        self.assertEqual(target.read_text(), 'original')
        self.assertEqual(created.name, 'alias_1.md')
        self.assertTrue(alias.is_symlink())

    def test_group_collision_cleans_only_new_reservations(self):
        second = self.root / 'Part2.md'
        second.write_text('original')
        created = write_new_files([self.root / 'Part1.md', second], lambda paths: ['one', 'two'])
        self.assertEqual(second.read_text(), 'original')
        self.assertFalse((self.root / 'Part1.md').exists())
        self.assertEqual([p.name for p in created], ['Part1_1.md', 'Part2_1.md'])

    def test_render_failure_leaves_no_partial_output(self):
        def fail(_):
            raise ValueError('render failed')
        with self.assertRaises(ValueError):
            write_new_files([self.root / 'new.md'], fail)
        self.assertFalse((self.root / 'new.md').exists())

    def test_moc_snapshots_accumulate_without_edit(self):
        writer = MocWriter(self.cfg.vault_path)
        writer.update('general', 'A')
        first = writer.moc_dir / 'general_MOC.md'
        original = first.read_bytes()
        writer.update('general', 'B')
        second = writer.moc_dir / 'general_MOC_1.md'
        self.assertEqual(first.read_bytes(), original)
        self.assertIn('[[A]]', second.read_text())
        self.assertIn('[[B]]', second.read_text())
        writer.update('general', 'B')
        self.assertEqual(len(list(writer.moc_dir.glob('*.md'))), 2)

    def test_activity_writes_new_events(self):
        old = self.cfg.activity_log_path
        old.write_text('original')
        log = ActivityLogger(self.cfg)
        log.log('A', 'general', [], 'storage')
        log.log_health_check('report')
        self.assertEqual(old.read_text(), 'original')
        self.assertEqual(len(list(self.cfg.vault_path.glob('00_activity_log*.md'))), 3)

    def test_vault_move_is_rejected_and_inbox_move_preserves_conflict(self):
        mover = FileMover(self.cfg)
        source = self.cfg.vault_path / 'existing.md'
        source.write_text('original')
        with self.assertRaises(PermissionError):
            mover.move_to_storage(source, 'general')
        self.assertEqual(source.read_text(), 'original')
        inbox = self.cfg.storage_path / 'inbox'
        inbox.mkdir(parents=True)
        source = inbox / 'new.md'
        source.write_text('new')
        target = self.cfg.storage_folder_for_domain('general')
        target.mkdir(parents=True)
        (target / 'new.md').write_text('old')
        created = mover.move_to_storage(source, 'general')
        self.assertEqual(created.name, 'new_1.md')
        self.assertEqual((target / 'new.md').read_text(), 'old')

    def test_domain_and_moc_path_escape_rejected(self):
        with self.assertRaises(ValueError):
            self.cfg.vault_folder_for_domain('../../../../outside')
        with self.assertRaises(ValueError):
            MocWriter(self.cfg.vault_path, '../outside')

    def test_inbox_writers_do_not_overwrite_same_second(self):
        for class_name, file, args in [
            ('NoteValidator', 'validator.py', ('Title', 'supplement', 'new')),
            ('GapAnalyzer', 'gap_analyzer.py', ('Title', 'new')),
        ]:
            cls, _ = load_def('services/pipeline/src/' + file, class_name,
                              write_new_file=write_new_file)
            writer = cls.__new__(cls)
            writer.inbox = self.root / class_name
            with patch('src.new_files.write_new_files', wraps=write_new_files):
                first = writer._write_to_inbox(*args)
                second = writer._write_to_inbox(*args)
            self.assertIsNotNone(first)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_text(), 'new')


class LogicTests(unittest.TestCase):
    def test_rerank_dictionary_results_and_small_candidate_order(self):
        class Request:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
        fake = ModuleType('flashrank')
        fake.RerankRequest = Request
        ranker = SimpleNamespace(rerank=lambda req: list(reversed(req.passages)))
        fn, ns = load_def('services/chat/app.py', '_rerank_flashrank',
                          _get_ranker=lambda: ranker)
        chunks = [{'content': str(i), 'score': i / 10} for i in range(10)]
        with patch.dict(sys.modules, {'flashrank': fake}):
            self.assertEqual(fn('query', chunks, 8), list(reversed(chunks))[:8])
            self.assertEqual(fn('query', chunks[:2], 8), list(reversed(chunks[:2])))
        ns['_get_ranker'] = lambda: None
        self.assertEqual(fn('query', chunks, 8), chunks[:8])

    def test_vault_path_absolute_relative_and_escape(self):
        class HTTPError(Exception):
            def __init__(self, status, detail): self.status = status
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / 'vault'
            root.mkdir()
            fn, _ = load_def('services/chat/app.py', '_vault_note_path',
                              VAULT_PATH=str(root), HTTPException=HTTPError)
            self.assertEqual(fn(str(root / 'A.md')), root / 'A.md')
            self.assertEqual(fn('A.md'), root / 'A.md')
            with self.assertRaises(HTTPError): fn('../outside.md')
            outside = Path(tmp) / 'outside.md'
            outside.write_text('original')
            (root / 'alias.md').symlink_to(outside)
            with self.assertRaises(HTTPError): fn('alias.md')

    def test_existing_note_mutation_and_relink_are_blocked(self):
        cls, _ = load_def('services/pipeline/src/linker.py', 'Linker')
        linker = cls.__new__(cls)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'original.md'
            path.write_text('original')
            for fn, args in [(linker.insert_links_into_note, (path, ['X'])),
                             (linker.add_backlink, (path, 'X')),
                             (linker.link_note, (path, 'Title', 'general', 'text', []))]:
                with self.assertRaises(PermissionError): fn(*args)
            self.assertEqual(linker.sanitize_all_notes([{'path': str(path)}]), 0)
            self.assertEqual(linker.relink_all_notes(None), 0)
            self.assertEqual(path.read_text(), 'original')

    def test_duplicate_threshold_uses_cosine_search(self):
        cls, _ = load_def('services/pipeline/src/linker.py', 'Linker')
        linker = cls.__new__(cls)
        linker.llm = SimpleNamespace(embed=lambda text: [1.0])
        linker._dense_search = lambda *args: [{'title': 'duplicate', 'score': 0.95}]
        linker._hybrid_search = lambda *args, **kwargs: [{'score': 0.4}]
        self.assertEqual(linker.check_duplicate('text')['title'], 'duplicate')
        self.assertEqual(linker.find_similar('text', 0.85)[0]['score'], 0.95)
        self.assertEqual(linker.find_similar('text', 0.3)[0]['score'], 0.4)

    def test_collection_schema_mismatch_never_deletes(self):
        cls, _ = load_def('services/pipeline/src/linker.py', 'Linker', COLLECTION='notes')
        linker = cls.__new__(cls)
        class Client:
            def get_collections(self): return SimpleNamespace(collections=[SimpleNamespace(name='notes')])
            def get_collection(self, _): return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors='legacy')))
            def delete_collection(self, _): raise AssertionError('must not delete')
        linker.client = Client()
        with self.assertRaises(RuntimeError): linker._ensure_collection()

    def test_processing_parse_failure_is_terminal(self):
        cls, _ = load_def('services/pipeline/src/processor.py', 'Processor',
                          _md5=lambda _: 'hash', parse_file=lambda *args, **kwargs: ('', 'unknown'))
        updates = []
        processor = cls.__new__(cls)
        processor.config = SimpleNamespace(skip_extensions=[], ollama_url='', vision_model='')
        processor.db = SimpleNamespace(is_already_processed=lambda *args: False,
                                       create_job=lambda *args: 1,
                                       update_job=lambda job, **kwargs: updates.append(kwargs))
        self.assertEqual(processor._process_single_file(Path('unknown.xyz')), [])
        self.assertEqual(updates[-1]['status'], 'failed')
        self.assertIn('error_message', updates[-1])

    def test_rag_modes_order_and_dedup(self):
        tree = ast.parse((ROOT / 'services/chat/app.py').read_text())
        prompts = ast.literal_eval(next(n.value for n in tree.body if isinstance(n, ast.Assign)
                                       and any(isinstance(t, ast.Name) and t.id == 'SYSTEM_PROMPTS' for t in n.targets)))
        calls = []
        async def hyde(q): calls.append(('hyde', q)); return 'hypothetical'
        async def hybrid(q, coll, limit):
            calls.append((coll, q, limit)); return [{'title': coll, 'note_id': coll, 'content': 'text', 'score': 0.9}]
        async def kb(q, category, limit):
            calls.append(('kb', q, category, limit)); return [{'title': 'KB', 'note_id': 'kb', 'content': 'text', 'score': 0.9}]
        def rerank(q, hits, top): calls.append(('rerank', top)); return hits[:top]
        async def gap(*args): pass
        fn, _ = load_def('services/chat/app.py', '_build_rag_context', SYSTEM_PROMPTS=prompts,
                          NOTES_COLLECTION='notes', PAYLOADS_COLLECTION='ctf_payloads',
                          _hyde=hyde, _qdrant_search_hybrid=hybrid, _qdrant_search_security_kb=kb,
                          _rerank_flashrank=rerank, _log_rag_gap=gap)
        expected = {'rag': (True, None, []), 'ctf': (True, 'vuln_env', ['owasp']),
                    'pentest': (False, None, ['vulnerability', 'advisory_go', 'advisory_python']),
                    'bypass': (False, 'owasp', ['vulnerability']),
                    'threat': (False, 'threat_intel', ['compliance']),
                    'remediator': (True, None, ['vulnerability', 'owasp', 'advisory_go', 'advisory_python']),
                    'diagnostics': (True, None, ['vulnerability', 'compliance'])}
        async def run():
            system, sources = await fn('general', 'query')
            self.assertEqual(sources, [])
            self.assertEqual(calls, [])
            for mode, (notes, main, aux) in expected.items():
                calls.clear()
                system, sources = await fn(mode, 'query')
                self.assertEqual(calls[0][0], 'hyde')
                self.assertIn(('rerank', 8), calls)
                self.assertEqual(any(c[0] == 'notes' for c in calls), notes)
                self.assertEqual(any(c[0] == 'ctf_payloads' for c in calls), mode == 'ctf')
                self.assertIn(('kb', 'hypothetical', main, 12), calls)
                self.assertIn(('kb', 'query', main, 6), calls)
                for cat in aux: self.assertIn(('kb', 'hypothetical', cat, 6), calls)
                self.assertNotIn('{context}', system)
                self.assertEqual(sum(s['title'] == 'KB' for s in sources), 1)
            await asyncio.sleep(0)
        asyncio.run(run())


class ValidationFailureTests(unittest.TestCase):
    def make_llm(self, raw):
        cls, _ = load_def('services/pipeline/src/llm.py', 'LLMClient',
                          _fmt=lambda template, **kwargs: template)
        llm = cls.__new__(cls)
        llm.config = SimpleNamespace(get_prompt=lambda key: '', num_ctx_for=lambda task: 4096)
        llm._chat = lambda *args, **kwargs: raw
        return llm

    def test_invalid_evaluation_is_not_good(self):
        for raw in ['', 'invalid', '[]', '{}', '{"verdict":"unknown"}']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.make_llm(raw).evaluate_note('Title', 'general', 'text')
        self.assertEqual(self.make_llm('{"verdict":"good"}').evaluate_note('Title', 'general', 'text')['verdict'], 'good')

    def test_invalid_self_validation_is_not_success(self):
        for raw in ['', '{}', '[]', '{"still_critical":"false"}']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.make_llm(raw).self_validate('Title', 'text')
        self.assertFalse(self.make_llm('{"still_critical":false}').self_validate('Title', 'text')['still_critical'])

    def test_empty_llm_content_raises_after_retry_exhaustion(self):
        class HTTPError(Exception): pass
        response = SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                   json=lambda: {'message': {'content': ''}})
        cls, _ = load_def('services/pipeline/src/llm.py', 'LLMClient',
                          requests=SimpleNamespace(post=lambda *args, **kwargs: response, HTTPError=HTTPError))
        llm = cls.__new__(cls)
        llm.model = 'test-model'
        llm.base_url = 'unused'
        llm.timeout = 1
        llm.max_retries = 1
        llm.config = SimpleNamespace(num_ctx_default=4096)
        with self.assertRaises(RuntimeError): llm._chat('system', 'user')

    def test_missing_supplement_is_not_marked_fixed(self):
        cls, _ = load_def('services/pipeline/src/validator.py', 'NoteValidator')
        validator = cls.__new__(cls)
        validator.llm = SimpleNamespace(evaluate_note=lambda *args: {'verdict': 'insufficient_topics'})
        validator._handle_insufficient = lambda *args: None
        validator.db = SimpleNamespace(mark_note_validated=lambda *args: self.fail('must not mark complete'))
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / 'A.md'
            note.write_text('original')
            with self.assertRaises(RuntimeError): validator.validate_note(note, 'Title', 'general')
            self.assertEqual(note.read_text(), 'original')

    def test_ideas_movement_is_disabled(self):
        cls, _ = load_def('services/pipeline/src/ideas_organizer.py', 'IdeasOrganizer')
        organizer = cls.__new__(cls)
        organizer.reorganize()
        with self.assertRaises(PermissionError): organizer._merge_folders(Path('A'), Path('B'), 'Merged')

    def test_skipped_job_has_terminal_timestamp(self):
        job = SimpleNamespace(status='processing', completed_at=None)
        class Session:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get(self, *args): return job
            def commit(self): pass
        cls, _ = load_def('services/pipeline/src/db.py', 'Database', ProcessingJob=object)
        db = cls.__new__(cls)
        db.session = Session
        db.update_job(1, status='skipped')
        self.assertEqual(job.status, 'skipped')
        self.assertIsInstance(job.completed_at, datetime)


class SpecificationTests(unittest.TestCase):
    def test_explicit_route_table_is_complete(self):
        tree = ast.parse((ROOT / 'services/chat/app.py').read_text())
        routes = set()
        for node in tree.body:
            for d in getattr(node, 'decorator_list', []):
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in {'get','post','patch','delete'}:
                    routes.add((d.func.attr.upper(), ast.literal_eval(d.args[0])))
        spec = (ROOT / 'docs/dev-spec.md').read_text()
        documented = set(re.findall(r'\| (GET|POST|PATCH|DELETE) \| `([^`]+)` \|', spec))
        self.assertEqual(documented, routes)

    def test_env_table_matches_example(self):
        example = (ROOT / '.env.example').read_text()
        names = set(re.findall(r'^([A-Z_]+)=', example, re.M))
        spec = (ROOT / 'docs/dev-spec.md').read_text().split('## 7.')[1].split('### Compose')[0]
        self.assertEqual(names, set(re.findall(r'^\| `([A-Z_]+)` \|', spec, re.M)))

    def test_all_pipeline_files_and_db_columns_documented(self):
        spec = (ROOT / 'docs/dev-spec.md').read_text()
        for file in (ROOT / 'services/pipeline/src').glob('*.py'):
            self.assertIn(file.name, spec)
        tree = ast.parse((ROOT / 'services/pipeline/src/db.py').read_text())
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef) or cls.name not in {'NoteIndex','ProcessingJob'}: continue
            for node in cls.body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == 'Column':
                    self.assertIn('| ' + node.targets[0].id + ' |', spec)


if __name__ == '__main__':
    unittest.main()
