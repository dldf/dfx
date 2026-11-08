"""Keep archived sources safe while installing the new publication layout."""
import io
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stage
import review
from workflow import document_present


class PublicationLayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / '2_pending_review/reports/sample'
        self.destination = self.root / '3_ready_to_publish/reports/sample'
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
        self.assertIn('Human review notes.', (self.destination / 'assets/_pdf2md/log.md').read_text())

    def test_success_archives_sources_rebases_links_and_preserves_rendered_files(self):
        html = (self.source / 'index.html').read_bytes()
        self.assertEqual(self.run_stage(), self.destination)
        self.assert_ready()
        self.assertEqual((self.destination / 'index.html').read_bytes(), html)
        archived = (self.destination / 'assets/_pdf2md/content.md').read_bytes()
        self.assertEqual(archived, b'# Title\r\n\r\n![Description](../figure.png)\r\n'
                                  b'`![example](assets/figure.png)`\r\n')
        log = (self.destination / 'assets/_pdf2md/log.md').read_text()
        self.assertIn('[MD](content.md#source-page1)', log)
        self.assertIn('[HTML](../../index.html)', log)
        self.assertIn('[Figure](../figure.png)', log)
        self.assertFalse((self.destination / 'review').exists())
        self.assertEqual({p.name for p in self.source.iterdir()}, {'ready_to_publish.txt'})

    def test_log_copy_failure_preserves_originals(self):
        copy = shutil.copy2
        def fail(source, target, **kwargs):
            if Path(source).name == 'log.md':
                raise OSError('disk full')
            return copy(source, target, **kwargs)
        with patch('stage.shutil.copy2', side_effect=fail), self.assertRaisesRegex(RuntimeError, 'Copy failed'):
            self.run_stage()
        self.assert_originals_unchanged()
        self.assertFalse(self.destination.exists())

    def test_corrupt_log_copy_is_detected(self):
        copy = shutil.copy2
        def corrupt(source, target, **kwargs):
            result = copy(source, target, **kwargs)
            if Path(source).name == 'log.md':
                Path(target).write_text('Lost notes')
            return result
        with patch('stage.shutil.copy2', side_effect=corrupt), self.assertRaisesRegex(RuntimeError, 'did not match'):
            self.run_stage()
        self.assert_originals_unchanged()
        self.assertFalse(self.destination.exists())

    def test_changed_log_stops_before_install(self):
        copy = shutil.copy2
        def change(source, target, **kwargs):
            result = copy(source, target, **kwargs)
            if Path(source).name == 'log.md':
                Path(source).write_text('New notes during staging')
            return result
        with patch('stage.shutil.copy2', side_effect=change), self.assertRaisesRegex(RuntimeError, 'changed during verification'):
            self.run_stage()
        self.assertEqual((self.source / 'review/log.md').read_text(), 'New notes during staging')
        self.assertTrue(self.markdown.is_file())
        self.assertFalse(self.destination.exists())

    def test_link_relocation_failure_preserves_originals(self):
        with patch('stage.relocate_links', side_effect=ValueError('invalid link')), \
                self.assertRaisesRegex(RuntimeError, 'Copy failed'):
            self.run_stage()
        self.assert_originals_unchanged()
        self.assertFalse(self.destination.exists())

    def test_index_installs_last_and_partial_install_stays_unready(self):
        link = Path.hardlink_to
        def fail(path, target):
            if path == self.destination / 'index.html':
                self.assertTrue((self.destination / 'assets/_pdf2md/content.md').is_file())
                self.assertTrue((self.destination / 'assets/_pdf2md/log.md').is_file())
                self.assertTrue((self.destination / 'assets/main.css').is_file())
                self.assertFalse(document_present(self.destination))
                raise OSError('cannot install index')
            return link(path, target)
        with patch.object(Path, 'hardlink_to', fail), self.assertRaisesRegex(RuntimeError, 'Installation failed'):
            self.run_stage()
        self.assert_originals_unchanged()
        self.assertFalse(document_present(self.destination))

    def test_receipt_failure_preserves_ready_copy_and_originals(self):
        with patch('stage.write_receipt', side_effect=OSError('permission denied')), \
                self.assertRaisesRegex(RuntimeError, 'Ready copy is complete'):
            self.run_stage()
        self.assert_originals_unchanged()
        self.assert_ready()

    def test_cleanup_failure_keeps_archived_log(self):
        remove = shutil.rmtree
        def fail(path, *args, **kwargs):
            if Path(path) == self.source / 'review':
                raise OSError('review folder locked')
            return remove(path, *args, **kwargs)
        with patch('stage.shutil.rmtree', side_effect=fail), self.assertRaisesRegex(RuntimeError, 'Cleanup was interrupted'):
            self.run_stage()
        self.assert_ready()
        self.assertTrue((self.source / 'review/log.md').is_file())
        self.assertTrue((self.source / 'ready_to_publish.txt').is_file())

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
        with self.assertRaisesRegex(FileExistsError, 'reserved'):
            self.run_stage()
        archive.rmdir()
        log = self.source / 'review/log.md'
        log.unlink()
        log.symlink_to(self.markdown)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            self.run_stage()
        self.assertFalse(self.destination.exists())

    def test_readiness_requires_all_new_layout_files(self):
        self.run_stage()
        for name in ('index.html', 'assets/main.css', 'assets/_pdf2md/content.md', 'assets/_pdf2md/log.md'):
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
            self.destination / 'assets/_pdf2md/content.md', self.source, self.destination, {})
        self.assertIn('[link](../figure.png "caption")', rewritten)
        self.assertIn('[ref]: ../figure.png', rewritten)
        self.assertIn('<img src="../figure.png" alt="Keep this">', rewritten)
        self.assertIn('[external](https://example.org/report.pdf?q=1#page=2)', rewritten)
        self.assertIn('```md\n![example](assets/figure.png)\n```', rewritten)

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
