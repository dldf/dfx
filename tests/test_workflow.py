"""File-based states and safe publication transitions, using temporary documents."""
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify
from workflow import DOWNLOADS, PENDING, READY, conversion_destination

spec = importlib.util.spec_from_file_location('pdf_site', Path(__file__).resolve().parents[1] / 'site.py')
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.patch = patch.object(site, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.url = 'https://example.edu/reports/plan.pdf?version=2'

    def test_tree_uses_presence_and_precedence_not_stored_status(self):
        pdf = site.local_path(self.url)
        relative = pdf.relative_to(self.root / DOWNLOADS).with_suffix('')
        self.assertIsNone(site.document_stage(self.url))
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b'pdf')
        self.assertEqual(site.document_stage(self.url), '1')
        pending = self.root / PENDING / relative
        pending.mkdir(parents=True)
        (pending / 'ready_to_publish.txt').write_text('receipt alone')
        self.assertEqual(site.document_stage(self.url), '1')
        (pending / 'content.md').write_text('document')
        self.assertEqual(site.document_stage(self.url), '2')
        ready = self.root / READY / relative
        ready.mkdir(parents=True)
        self.assertEqual(site.document_stage(self.url), '2')
        (ready / 'content.md').write_text('document')
        self.assertEqual(site.document_stage(self.url), '3')
        output = io.StringIO()
        with redirect_stdout(output):
            site.tree({'documents': {self.url: {'status': 'U'}}}, 'https://example.edu/reports/')
        self.assertIn('[3] plan.pdf?version=2', output.getvalue())
        self.assertIn('[1] Downloaded', output.getvalue())
        self.assertNotIn('accessible', output.getvalue())
        (ready / 'content.md').unlink()
        self.assertEqual(site.document_stage(self.url), '2')

    def test_undownloaded_old_accessible_status_is_ignored(self):
        output = io.StringIO()
        with redirect_stdout(output):
            site.tree({'documents': {self.url: {'status': 'A'}}}, 'https://example.edu/reports/')
        self.assertIn('not downloaded', output.getvalue())
        self.assertNotIn('[A]', output.getvalue())
        # A stale status must not suppress a download offer.
        with patch('builtins.input', return_value='n'), redirect_stdout(output):
            site.download({'documents': {self.url: {'status': 'A'}}}, 'https://example.edu/reports/')
        self.assertIn('1 PDFs to download', output.getvalue())

    def test_single_subfolder_and_batch_conversion_share_destination(self):
        pdf = site.local_path(self.url)
        target = self.root / PENDING
        results = [conversion_destination(pdf, base, target, self.root)
                   for base in (pdf.parent, self.root / DOWNLOADS, self.root / DOWNLOADS / 'example.edu')]
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(results[0].relative_to(target), pdf.relative_to(self.root / DOWNLOADS).with_suffix(''))
        custom = self.root / 'custom'
        self.assertEqual(conversion_destination(pdf, pdf.parent, custom, self.root), custom / pdf.stem)

    def test_scan_limit_alias_and_default(self):
        for options, expected in (([], 250), (['--max-urls', '100'], 100), (['--max-pages', '100'], 100)):
            with self.subTest(options=options), patch.object(sys, 'argv', ['site.py', 'scan', 'https://example.edu/reports/', '--scan-only', *options]), patch.object(site, 'load', return_value={'scans': {}}), patch.object(site, 'scan') as scan, patch.object(site, 'tree'), patch.object(site, 'download') as download:
                site.main()
                self.assertEqual(scan.call_args.args[2], expected)
                download.assert_not_called()

    def test_scan_limit_rejects_invalid_values(self):
        for option in ('--max-urls', '--max-pages'):
            for value in ('0', '-1', 'invalid'):
                with self.subTest(option=option, value=value), patch.object(sys, 'argv', ['site.py', 'scan', 'https://example.edu/reports/', option, value]), patch.object(site, 'load', return_value={'scans': {}}), patch.object(site, 'scan') as scan, patch('sys.stderr', io.StringIO()) as errors:
                    with self.assertRaises(SystemExit) as error:
                        site.main()
                    self.assertNotEqual(error.exception.code, 0)
                    self.assertIn('invalid int value' if value == 'invalid' else '--max-urls must be positive', errors.getvalue())
                    scan.assert_not_called()

    def test_manual_status_command_is_removed(self):
        with patch.object(sys, 'argv', ['site.py', 'status', self.url, 'R']), redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                site.main()
        self.assertEqual(error.exception.code, 2)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / PENDING / 'example.edu' / 'report'
        self.source.mkdir(parents=True)
        self.markdown = self.source / 'content.md'
        self.markdown.write_text('# Reviewed content\n\n![Meaningful description](assets/figure.png)\n')
        (self.source / 'preview.html').write_text('<html><body><img src="assets/figure.png" alt="Meaningful description"></body></html>')
        (self.source / 'assets').mkdir()
        (self.source / 'assets/figure.png').write_bytes(b'example image bytes')
        (self.source / 'assets/empty').mkdir()
        (self.source / 'review').mkdir()
        (self.source / 'review/log.md').write_text('Review notes to be deleted intentionally.')
        self.destination = self.root / READY / 'example.edu' / 'report'
        self.before = verify.snapshot(self.source, 'content.md', 'preview.html')

    def unchanged(self):
        self.assertEqual(verify.snapshot(self.source, 'content.md', 'preview.html'), self.before)
        self.assertTrue((self.source / 'review/log.md').is_file())

    def test_success_prompts_moves_unchanged_files_and_leaves_two_line_receipt(self):
        with patch('builtins.input', return_value='Jane Smith') as prompt:
            result = verify.verify_document(self.markdown, root=self.root)
        prompt.assert_called_once()
        self.assertEqual(result, self.destination)
        self.assertEqual(verify.snapshot(result, 'content.md', 'frompdf.html'), self.before)
        self.assertFalse((result / 'preview.html').exists())
        self.assertFalse((result / 'review').exists())
        self.assertEqual([path.name for path in self.source.iterdir()], ['ready_to_publish.txt'])
        lines = (self.source / 'ready_to_publish.txt').read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r'^Reviewed by "Jane Smith"; on "\d{4}-\d{2}-\d{2}" at "\d{2}:\d{2}:\d{2} [+-]\d{4}"\.$')
        self.assertEqual(lines[1], self.destination.as_uri() + '/')

    def test_destination_conflict_stops_before_name_prompt(self):
        self.destination.mkdir(parents=True)
        (self.destination / 'keep.txt').write_text('existing')
        with patch('builtins.input') as prompt, self.assertRaises(FileExistsError):
            verify.verify_document(self.markdown, root=self.root)
        prompt.assert_not_called()
        self.assertEqual((self.destination / 'keep.txt').read_text(), 'existing')
        self.unchanged()

    def test_copy_failure_does_not_delete_originals(self):
        with patch('verify.shutil.copy2', side_effect=OSError('disk full')), self.assertRaisesRegex(RuntimeError, 'originals and review files are unchanged'):
            verify.verify_document(self.markdown, 'Jane', root=self.root)
        self.unchanged()
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.source / 'ready_to_publish.txt').exists())

    def test_changed_source_stops_before_install(self):
        copy = shutil.copy2
        def change(source, target, **kwargs):
            result = copy(source, target, **kwargs)
            if Path(source).name == 'preview.html':
                self.markdown.write_text('New edit while copying')
            return result
        with patch('verify.shutil.copy2', side_effect=change), self.assertRaisesRegex(RuntimeError, 'changed during verification'):
            verify.verify_document(self.markdown, 'Jane', root=self.root)
        self.assertEqual(self.markdown.read_text(), 'New edit while copying')
        self.assertFalse(self.destination.exists())
        self.assertTrue((self.source / 'review/log.md').exists())

    def test_receipt_failure_leaves_ready_copy_and_all_originals(self):
        with patch('verify.write_receipt', side_effect=OSError('permission denied')), self.assertRaisesRegex(RuntimeError, 'Ready copy is complete'):
            verify.verify_document(self.markdown, 'Jane', root=self.root)
        self.unchanged()
        self.assertEqual(verify.snapshot(self.destination, 'content.md', 'frompdf.html'), self.before)

    def test_interrupted_cleanup_preserves_complete_ready_copy_and_receipt(self):
        remove = shutil.rmtree
        def interrupt(path, *args, **kwargs):
            if Path(path) == self.source / 'review':
                raise PermissionError('review locked')
            return remove(path, *args, **kwargs)
        with patch('verify.shutil.rmtree', side_effect=interrupt), self.assertRaisesRegex(RuntimeError, 'Cleanup was interrupted'):
            verify.verify_document(self.markdown, 'Jane', root=self.root)
        self.assertEqual(verify.snapshot(self.destination, 'content.md', 'frompdf.html'), self.before)
        self.assertTrue((self.source / 'ready_to_publish.txt').is_file())
        self.assertTrue((self.source / 'review/log.md').is_file())

    def test_incomplete_install_does_not_publish_markdown(self):
        link = Path.hardlink_to
        def interrupt(path, target):
            if path == self.destination / 'frompdf.html':
                raise OSError('cannot install HTML')
            return link(path, target)
        with patch.object(Path, 'hardlink_to', interrupt), self.assertRaisesRegex(RuntimeError, 'Installation failed'):
            verify.verify_document(self.markdown, 'Jane', root=self.root)
        self.unchanged()
        self.assertFalse((self.destination / 'content.md').exists())

    def test_missing_asset_and_blank_name_stop_without_destination(self):
        with self.assertRaisesRegex(ValueError, 'nonempty reviewer'):
            verify.verify_document(self.markdown, '  ', root=self.root)
        (self.source / 'assets/figure.png').unlink()
        with patch('builtins.input') as prompt, self.assertRaisesRegex(ValueError, 'HTML image must exist'):
            verify.verify_document(self.markdown, root=self.root)
        prompt.assert_not_called()
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.markdown.exists())

    def test_symlink_assets_are_not_followed(self):
        asset = self.source / 'assets/figure.png'
        asset.unlink()
        outside = self.root / 'outside.txt'
        outside.write_text('keep me')
        asset.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'Unsupported asset'):
            verify.verify_document(self.markdown, 'Jane', root=self.root)
        self.assertEqual(outside.read_text(), 'keep me')
        self.assertTrue(self.markdown.is_file())

    def test_document_without_assets_is_supported(self):
        shutil.rmtree(self.source / 'assets')
        self.markdown.write_text('# Plain document')
        (self.source / 'preview.html').write_text('<h1>Plain document</h1>')
        destination = verify.verify_document(self.markdown, 'Jane', root=self.root)
        self.assertTrue((destination / 'content.md').is_file())
        self.assertFalse((destination / 'assets').exists())


if __name__ == '__main__':
    unittest.main()
