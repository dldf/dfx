"""Rebuild only existing review documents and validate sources before replacement."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pymupdf

from dfx import rebuild
from dfx.workflow import DOWNLOADS, PENDING


class RebuildTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.folder = self.make_document('reports/report.v2')
        self.source = self.make_pdf('reports/report.v2.PDF', 'Fresh source content')

    def make_document(self, relative):
        folder = self.root / PENDING / relative
        (folder / 'review').mkdir(parents=True)
        (folder / 'assets').mkdir()
        (folder / 'content.md').write_text('# Edited old Markdown')
        (folder / 'review/log.md').write_text('Old review log')
        (folder / 'review/old.png').write_bytes(b'old preview')
        (folder / 'assets/old.png').write_bytes(b'old asset')
        (folder / 'preview.html').write_text('Old HTML')
        return folder

    def make_pdf(self, relative, text):
        source = self.root / DOWNLOADS / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 60), text)
            document.save(source)
        return source

    def snapshot(self, folder=None):
        folder = folder or self.folder
        return {str(path.relative_to(folder)): path.read_bytes()
                for path in folder.rglob('*') if path.is_file()}

    def test_rebuild_replaces_generated_files_and_preserves_source_and_other_files(self):
        original = self.source.read_bytes()
        (self.folder / 'keep.txt').write_text('Unrelated file')
        # A stale hash is expected when intentionally rebuilding an updated source.
        (self.folder / 'content.md').write_text('<!-- SHA-256: ' + '0' * 64 + ' -->\nOld edits')
        def render(source, markdown, chrome):
            self.assertEqual(source, self.source)
            self.assertIn('Fresh source content', markdown.read_text())
            (markdown.parent / 'index.html').write_text('New HTML')
        with patch.object(rebuild, 'preview', side_effect=render):
            rebuild.rebuild_document(self.folder, 'test-chrome', self.root)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual((self.folder / 'keep.txt').read_text(), 'Unrelated file')
        self.assertEqual((self.folder / 'index.html').read_text(), 'New HTML')
        self.assertFalse((self.folder / 'preview.html').exists())
        self.assertFalse((self.folder / 'assets/old.png').exists())
        self.assertFalse((self.folder / 'review/old.png').exists())
        self.assertIn(f'../../../../{DOWNLOADS}/reports/report.v2.PDF',
                      (self.folder / 'review/log.md').read_text())
        self.assertEqual(list(self.folder.parent.glob('.dfx-rebuild-*')), [])

    def test_discovery_selects_review_documents_not_unconverted_downloads_or_assets(self):
        self.make_pdf('reports/unconverted.pdf', 'Not yet converted')
        nested = self.make_document('reports/report.v2/nested')
        other = self.make_document('elsewhere/other')
        (self.folder / 'assets/content.md').write_text('Asset, not a document')
        (self.folder / 'review/content.md').write_text('Review note, not a document')
        self.assertEqual(set(rebuild.review_documents(self.root / PENDING, self.root)),
                         {self.folder, nested, other})
        self.assertEqual(rebuild.review_documents(self.root / PENDING / 'reports', self.root),
                         [self.folder, nested])

    def test_parent_rebuild_does_not_delete_nested_review_documents(self):
        child = self.make_document('reports/report.v2/nested')
        before = self.snapshot(child)
        with patch.object(rebuild, 'preview'):
            rebuild.rebuild_document(self.folder, 'test-chrome', self.root)
        self.assertEqual(self.snapshot(child), before)

    def test_source_paths_named_assets_or_review_are_still_discovered(self):
        assets = self.make_document('example.edu/assets/handbook')
        review = self.make_document('example.edu/review/report')
        stale = self.make_document('example.edu/.dfx-rebuild-abandoned')
        documents = rebuild.review_documents(self.root / PENDING, self.root)
        self.assertIn(assets, documents)
        self.assertIn(review, documents)
        self.assertNotIn(stale, documents)

    def test_missing_ambiguous_and_invalid_sources_leave_review_untouched(self):
        original = self.source.read_bytes()
        before = self.snapshot()
        for failure in ('missing', 'invalid', 'ambiguous'):
            with self.subTest(failure=failure):
                self.source.write_bytes(original)
                duplicate = self.source.with_suffix('.pdf')
                if failure == 'missing':
                    self.source.unlink()
                elif failure == 'invalid':
                    self.source.write_text('Not a PDF')
                else:
                    if duplicate.exists():
                        self.skipTest('Case-insensitive filesystem cannot store both .pdf and .PDF variants.')
                    duplicate.write_bytes(original)
                with patch.object(rebuild, 'convert_pdf') as convert, patch.object(rebuild, 'preview') as preview:
                    with self.assertRaises((ValueError, RuntimeError)):
                        rebuild.rebuild_document(self.folder, 'test-chrome', self.root)
                convert.assert_not_called()
                preview.assert_not_called()
                self.assertEqual(self.snapshot(), before)
                duplicate.unlink(missing_ok=True)

    def test_failed_conversion_cleans_temporary_output_and_keeps_old_document(self):
        before = self.snapshot()
        def fail(source, destination):
            destination.mkdir()
            (destination / 'content.md').write_text('Partial conversion')
            raise ValueError('Conversion failed')
        with patch.object(rebuild, 'convert_pdf', side_effect=fail), patch.object(rebuild, 'preview') as preview:
            with self.assertRaisesRegex(ValueError, 'Conversion failed'):
                rebuild.rebuild_document(self.folder, 'test-chrome', self.root)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.folder.parent.glob('.dfx-rebuild-*')), [])
        preview.assert_not_called()

    def test_preview_failure_keeps_new_markdown_and_explains_retry(self):
        with patch.object(rebuild, 'preview', side_effect=RuntimeError('Chrome failed')):
            with self.assertRaisesRegex(RuntimeError, 'Markdown rebuilt.*Retry dfx review'):
                rebuild.rebuild_document(self.folder, 'test-chrome', self.root)
        self.assertIn('Fresh source content', (self.folder / 'content.md').read_text())
        self.assertFalse((self.folder / 'preview.html').exists())

    def test_batch_continues_after_missing_source_and_reports_failure(self):
        missing = self.make_document('reports/aaa-missing')
        original = self.snapshot(missing)
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(rebuild, 'ROOT', self.root), patch.object(rebuild, 'find_chrome', return_value='chrome'), \
                patch.object(rebuild, 'preview'), redirect_stdout(output), redirect_stderr(errors):
            result = rebuild.main([str(self.root / PENDING)])
        self.assertEqual(result, 1)
        self.assertIn('Rebuilt 1 documents; 1 failed.', output.getvalue())
        self.assertIn('aaa-missing', errors.getvalue())
        self.assertEqual(self.snapshot(missing), original)
        self.assertIn('Fresh source content', (self.folder / 'content.md').read_text())

    def test_rejects_outside_scope_and_dry_run_option(self):
        with self.assertRaises(ValueError):
            rebuild.review_documents(self.root / DOWNLOADS, self.root)
        with patch.object(rebuild, 'ROOT', self.root), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                rebuild.main([str(self.root / PENDING), '--dry-run'])
        self.assertEqual(error.exception.code, 2)

    def test_symlinked_documents_and_external_sources_are_not_followed(self):
        elsewhere = self.root / 'outside'
        elsewhere.mkdir()
        (elsewhere / 'content.md').write_text('Outside review tree')
        (self.root / PENDING / 'linked').symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(rebuild.review_documents(self.root / PENDING, self.root), [self.folder])
        self.source.rename(elsewhere / 'source.pdf')
        self.source.symlink_to(elsewhere / 'source.pdf')
        with self.assertRaises(ValueError):
            rebuild.downloaded_source(self.folder, self.root)

    def test_latex_document_keeps_its_source_type_when_companion_pdf_exists(self):
        source = self.source.with_suffix('.tex')
        source.write_text('New LaTeX source')
        (self.folder / 'content.md').write_text('<!-- pdf2md-latex: {} -->\nOld conversion')
        def convert(tex, destination):
            self.assertEqual(tex, source)
            destination.mkdir()
            (destination / 'content.md').write_text('New LaTeX conversion')
        with patch.object(rebuild, 'convert_latex', side_effect=convert) as latex, \
                patch.object(rebuild, 'convert_pdf') as pdf, patch.object(rebuild, 'preview'):
            rebuild.rebuild_document(self.folder, 'test-chrome', self.root)
        latex.assert_called_once()
        pdf.assert_not_called()
        self.assertEqual((self.folder / 'content.md').read_text(), 'New LaTeX conversion')


if __name__ == '__main__':
    unittest.main()
