"""Stage complete publication files while retaining review Markdown and logs."""
import io
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dfx import stage
from dfx import review
from dfx.workflow import document_present


class PublicationLayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / '2_review/reports/sample'
        self.destination = self.root / '3_publish/reports/sample'
        self.source.mkdir(parents=True)
        (self.source / 'assets').mkdir()
        (self.source / 'assets/main.css').write_text('body { color: black; }')
        (self.source / 'assets/figure.png').write_bytes(b'image')
        (self.source / 'index.html').write_text(
            '<link rel="stylesheet" href="assets/main.css"><h1>Title</h1>'
            '<img src="assets/figure.png" alt="Description">')
        self.markdown = self.source / 'content.md'
        self.markdown.write_bytes(b'# Title\r\n\r\n![Description](assets/figure.png)\r\n'
                                  b'`![example](assets/figure.png)`\r\n')
        (self.source / 'review').mkdir()
        (self.source / 'review/log.md').write_text(
            '[MD](../content.md#source-page1) | [HTML](../index.html)\n'
            '[Figure](../assets/figure.png)\nHuman review notes.\n')
        (self.source / 'review/p001-original.png').write_bytes(b'review image')
        self.before = stage.tree_snapshot(self.source)

    def run_stage(self):
        return stage.verify_document(self.source, 'Test Reviewer', root=self.root)

    def assert_originals_unchanged(self):
        self.assertEqual(stage.tree_snapshot(self.source), self.before)

    def assert_ready(self):
        self.assertEqual({p.name for p in self.destination.iterdir()}, {'index.html', 'assets'})
        self.assertTrue(document_present(self.destination))
        self.assertEqual((self.destination / 'assets/figure.png').read_bytes(), b'image')
        self.assertEqual((self.destination / 'assets/main.css').read_text(), 'body { color: black; }')
        self.assertFalse((self.destination / 'assets/_pdf2md').exists())
        self.assertIn('Human review notes.', (self.source / 'review/log.md').read_text())

    def test_success_keeps_metadata_and_moves_only_publication_files(self):
        html = (self.source / 'index.html').read_bytes()
        (self.source / 'review/p001-revised.png').write_bytes(b'revised')
        (self.source / 'review/document-revised.png').write_bytes(b'latex preview')
        (self.source / 'preview.html').write_text('Stale preview')
        self.assertEqual(self.run_stage(), self.destination)
        self.assert_ready()
        self.assertEqual((self.destination / 'index.html').read_bytes(), html)
        self.assertEqual(self.markdown.read_bytes(),
                         b'# Title\r\n\r\n![Description](../../../3_publish/reports/sample/assets/figure.png)\r\n'
                         b'`![example](assets/figure.png)`\r\n')
        log = (self.source / 'review/log.md').read_text()
        self.assertIn('[MD](../content.md#source-page1)', log)
        self.assertIn('[HTML](../../../../3_publish/reports/sample/index.html)', log)
        self.assertIn('[Figure](../../../../3_publish/reports/sample/assets/figure.png)', log)
        self.assertIn('Reviewed by "Test Reviewer"; staged on ', log)
        self.assertEqual({p.relative_to(self.source).as_posix() for p in self.source.rglob('*') if p.is_file()},
                         {'content.md', 'review/log.md'})
        self.assertFalse((self.destination / 'review').exists())

    def test_html_copy_failure_preserves_originals(self):
        copy = shutil.copy2
        def fail(source, target, **kwargs):
            if Path(source).name == 'index.html':
                raise OSError('disk full')
            return copy(source, target, **kwargs)
        with patch('dfx.stage.shutil.copy2', side_effect=fail), self.assertRaisesRegex(RuntimeError, 'Copy failed'):
            self.run_stage()
        self.assert_originals_unchanged()
        self.assertFalse(self.destination.exists())

    def test_embedded_styles_stage_without_an_assets_directory(self):
        shutil.rmtree(self.source / 'assets')
        html = '<style>body { color: black; }</style><h1>Title</h1>'
        (self.source / 'index.html').write_text(html)
        self.markdown.write_text('# Title\n')
        (self.source / 'review/log.md').write_text('No issues.\n')
        self.assertEqual(self.run_stage(), self.destination)
        self.assertTrue(document_present(self.destination))
        self.assertEqual((self.destination / 'index.html').read_text(), html)
        self.assertFalse((self.destination / 'assets/main.css').exists())
        self.assertEqual(self.markdown.read_text(), '# Title\n')
        self.assertIn('No issues.', (self.source / 'review/log.md').read_text())
        self.assertEqual({p.name for p in self.destination.iterdir()}, {'index.html'})

    def test_corrupt_html_copy_is_detected(self):
        copy = shutil.copy2
        def corrupt(source, target, **kwargs):
            result = copy(source, target, **kwargs)
            if Path(source).name == 'index.html':
                Path(target).write_text('Lost notes')
            return result
        with patch('dfx.stage.shutil.copy2', side_effect=corrupt), self.assertRaisesRegex(RuntimeError, 'did not match'):
            self.run_stage()
        self.assert_originals_unchanged()
        self.assertFalse(self.destination.exists())

    def test_changed_log_stops_before_install(self):
        copy = shutil.copy2
        def change(source, target, **kwargs):
            result = copy(source, target, **kwargs)
            if Path(source).name == 'index.html':
                (self.source / 'review/log.md').write_text('New notes during staging')
            return result
        with patch('dfx.stage.shutil.copy2', side_effect=change), self.assertRaisesRegex(RuntimeError, 'changed during verification'):
            self.run_stage()
        self.assertEqual((self.source / 'review/log.md').read_text(), 'New notes during staging')
        self.assertTrue(self.markdown.is_file())
        self.assertFalse(self.destination.exists())

    def test_link_relocation_failure_preserves_originals(self):
        with patch('dfx.stage.relocate_links', side_effect=ValueError('invalid link')), \
                self.assertRaisesRegex(RuntimeError, 'Copy failed'):
            self.run_stage()
        self.assert_originals_unchanged()
        self.assertFalse(self.destination.exists())

    def test_index_installs_last_and_partial_install_stays_unready(self):
        link = Path.hardlink_to
        def fail(path, target):
            if path == self.destination / 'index.html':
                self.assertFalse((self.destination / 'assets/_pdf2md').exists())
                self.assertTrue((self.destination / 'assets/main.css').is_file())
                self.assertFalse(document_present(self.destination))
                raise OSError('cannot install index')
            return link(path, target)
        with patch.object(Path, 'hardlink_to', fail), self.assertRaisesRegex(RuntimeError, 'Installation failed'):
            self.run_stage()
        self.assert_originals_unchanged()
        self.assertFalse(document_present(self.destination))

    def test_metadata_failure_preserves_ready_copy_and_originals(self):
        with patch('dfx.stage.write_metadata', side_effect=OSError('permission denied')), \
                self.assertRaisesRegex(RuntimeError, 'Ready copy is complete'):
            self.run_stage()
        self.assert_originals_unchanged()
        self.assert_ready()

    def test_cleanup_failure_keeps_metadata_and_publication(self):
        remove = shutil.rmtree
        def fail(path, *args, **kwargs):
            if Path(path) == self.source / 'assets':
                raise OSError('review folder locked')
            return remove(path, *args, **kwargs)
        with patch('dfx.stage.shutil.rmtree', side_effect=fail), self.assertRaisesRegex(RuntimeError, 'Cleanup was interrupted'):
            self.run_stage()
        self.assert_ready()
        self.assertTrue((self.source / 'review/log.md').is_file())
        self.assertTrue(self.markdown.is_file())
        self.assertFalse((self.source / 'ready_to_publish.txt').exists())

    def test_missing_css_or_log_stops_before_prompt(self):
        for name in ('assets/main.css', 'review/log.md'):
            with self.subTest(name=name):
                path = self.source / name
                data = path.read_bytes()
                path.unlink()
                with patch('builtins.input') as prompt, self.assertRaises(ValueError):
                    stage.verify_document(self.source, root=self.root)
                prompt.assert_not_called()
                self.assertFalse(self.destination.exists())
                path.write_bytes(data)

    def test_reserved_archive_and_symlink_log_are_rejected(self):
        archive = self.source / 'assets/_pdf2md'
        archive.mkdir()
        with self.assertRaisesRegex(FileExistsError, 'metadata found'):
            self.run_stage()
        archive.rmdir()
        log = self.source / 'review/log.md'
        log.unlink()
        log.symlink_to(self.markdown)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            self.run_stage()
        self.assertFalse(self.destination.exists())

    def test_readiness_requires_installed_html(self):
        self.run_stage()
        for name in ('index.html',):
            with self.subTest(name=name):
                path = self.destination / name
                data = path.read_bytes()
                path.unlink()
                self.assertFalse(document_present(self.destination))
                path.write_bytes(data)
        self.assertTrue(document_present(self.destination))

    def test_relocation_preserves_code_external_urls_and_html_markup(self):
        original = ('[link](assets/figure.png "caption")\n'
                    '[ref]: assets/figure.png\n'
                    '<img src="assets/figure.png" alt="Keep this">\n'
                    '[external](https://example.org/report.pdf?q=1#page=2)\n'
                    '```md\n![example](assets/figure.png)\n```\n')
        rewritten = stage.relocate_links(original, self.markdown,
            self.markdown, self.source, self.destination, {})
        self.assertIn('[link](../../../3_publish/reports/sample/assets/figure.png "caption")', rewritten)
        self.assertIn('[ref]: ../../../3_publish/reports/sample/assets/figure.png', rewritten)
        self.assertIn('<img src="../../../3_publish/reports/sample/assets/figure.png" alt="Keep this">', rewritten)
        self.assertIn('[external](https://example.org/report.pdf?q=1#page=2)', rewritten)
        self.assertIn('```md\n![example](assets/figure.png)\n```', rewritten)

    def test_removed_preview_links_keep_their_labels(self):
        log = self.source / 'review/log.md'
        log.write_text('Compare [Original](p001-original.png) with [HTML](../index.html).\n'
                       '`[Original](p001-original.png)`\n')
        self.run_stage()
        text = log.read_text()
        self.assertIn('Compare Original with [HTML](../../../../3_publish/reports/sample/index.html).', text)
        self.assertIn('`[Original](p001-original.png)`', text)
        self.assertFalse((log.parent / 'p001-original.png').exists())

    def test_unrelated_files_and_nested_documents_survive_cleanup(self):
        (self.source / 'keep.txt').write_text('Unrelated file')
        (self.source / 'review/notes.txt').write_text('Human notes')
        child = self.source / 'nested/content.md'
        child.parent.mkdir()
        child.write_text('# Child')
        self.run_stage()
        self.assertEqual((self.source / 'keep.txt').read_text(), 'Unrelated file')
        self.assertEqual((self.source / 'review/notes.txt').read_text(), 'Human notes')
        self.assertEqual(child.read_text(), '# Child')

    def test_legacy_preview_keeps_markdown_and_creates_log(self):
        (self.source / 'index.html').rename(self.source / 'preview.html')
        (self.source / 'review/log.md').unlink()
        self.run_stage()
        self.assertTrue(document_present(self.destination))
        self.assertTrue((self.destination / 'frompdf.html').is_file())
        self.assertTrue(self.markdown.is_file())
        self.assertIn('frompdf.html', (self.source / 'review/log.md').read_text())
        self.assertFalse((self.source / 'preview.html').exists())
        self.assertFalse(list(self.destination.rglob('*.md')))

    def test_metadata_changed_during_install_is_not_overwritten(self):
        install = stage.install_copy
        def change(*args):
            install(*args)
            self.markdown.write_text('# Concurrent edit')
        with patch.object(stage, 'install_copy', side_effect=change), \
                self.assertRaisesRegex(RuntimeError, 'Source files changed'):
            self.run_stage()
        self.assertEqual(self.markdown.read_text(), '# Concurrent edit')
        self.assertTrue((self.source / 'index.html').is_file())
        self.assertTrue((self.source / 'review/p001-original.png').is_file())

    def batch(self, path=None):
        with patch.object(stage, 'ROOT', self.root), \
                patch('builtins.input', return_value='Batch Reviewer') as prompt, \
                patch('sys.stdout', new_callable=io.StringIO) as output, \
                patch('sys.stderr', new_callable=io.StringIO) as errors:
            result = stage.main([str(path or self.root / '2_review')])
        return result, prompt, output.getvalue(), errors.getvalue()

    def add_document(self, relative):
        folder = self.root / '2_review' / relative
        (folder / 'review').mkdir(parents=True)
        (folder / 'content.md').write_text('# Another document\n')
        (folder / 'index.html').write_text('<style>body { color: black; }</style><h1>Another document</h1>')
        (folder / 'review/log.md').write_text('Review notes\n')
        (folder / 'review/p001-revised.png').write_bytes(b'preview')
        return folder

    def test_batch_stages_nested_documents_once_and_skips_existing_destinations(self):
        child = self.add_document('reports/sample/nested')
        other = self.add_document('example.edu/assets/report')
        self.add_document('example.edu/review/report')
        (self.source / 'assets/content.md').write_text('Asset, not a document')
        (self.source / 'review/content.md').write_text('Review note, not a document')
        existing = self.root / '3_publish' / other.relative_to(self.root / '2_review')
        existing.mkdir(parents=True)
        (existing / 'index.html').write_text('Existing publication')
        result, prompt, output, errors = self.batch()
        self.assertEqual(result, 0, errors)
        prompt.assert_called_once()
        self.assertIn('Staged 3 documents; 1 skipped; 0 failed.', output)
        self.assertEqual((existing / 'index.html').read_text(), 'Existing publication')
        self.assertTrue((other / 'index.html').is_file())
        self.assertTrue((child / 'content.md').is_file())
        self.assertFalse((child / 'index.html').exists())
        self.assertTrue((self.destination / 'nested/index.html').is_file())
        result, prompt, output, errors = self.batch()
        self.assertEqual(result, 0, errors)
        prompt.assert_not_called()
        self.assertIn('Staged 0 documents; 4 skipped; 0 failed.', output)

    def test_batch_reports_failure_and_continues_with_other_documents(self):
        broken = self.add_document('a-broken')
        (broken / 'index.html').unlink()
        result, prompt, output, errors = self.batch()
        self.assertEqual(result, 1)
        prompt.assert_called_once()
        self.assertIn('Staged 1 documents; 0 skipped; 1 failed.', output)
        self.assertIn('a-broken', errors)
        self.assertTrue((broken / 'content.md').is_file())
        self.assertTrue((broken / 'review/p001-revised.png').is_file())
        self.assert_ready()

    def test_batch_accepts_subfolder_and_markdown_file(self):
        other = self.add_document('elsewhere/other')
        result, _, _, errors = self.batch(self.source.parent)
        self.assertEqual(result, 0, errors)
        self.assertTrue((other / 'index.html').is_file())
        result, _, _, errors = self.batch(other / 'content.md')
        self.assertEqual(result, 0, errors)
        self.assertFalse((other / 'index.html').exists())

    def test_batch_refuses_a_symlink_selection(self):
        link = self.root / '2_review/link'
        link.symlink_to(self.source, target_is_directory=True)
        with patch.object(stage, 'ROOT', self.root), patch('builtins.input') as prompt, \
                patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            stage.main([str(link)])
        prompt.assert_not_called()
        self.assert_originals_unchanged()

    def test_review_folder_argument_prints_stage_command_without_prompt(self):
        with patch.object(sys, 'argv', ['review.py', str(self.source)]), \
                patch.object(review, 'ROOT', self.root), \
                patch.object(review, 'resolve_source', return_value=self.root / 'sample.pdf') as lookup, \
                patch.object(review, 'find_chrome', return_value='chrome'), \
                patch.object(review, 'preview') as render, patch('builtins.input') as prompt, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            review.main()
        lookup.assert_called_once_with(self.markdown)
        render.assert_called_once_with(self.root / 'sample.pdf', self.markdown, 'chrome', False)
        prompt.assert_not_called()
        self.assertIn('Next: dfx stage ', output.getvalue())
        self.assertNotIn('content.md', output.getvalue())


if __name__ == '__main__':
    unittest.main()
