"""Source lookup must finish before review files can change."""
from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review


class ReviewSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.pdf = self.make_pdf('1_downloads/reports/report.v2.PDF', 'Original source')
        self.original = self.pdf.read_bytes()
        self.markdown = self.root / '2_pending_review/reports/report.v2/content.md'
        self.markdown.parent.mkdir(parents=True)
        digest = hashlib.sha256(self.original).hexdigest()
        self.markdown.write_text(f'<!-- Source: report.v2.PDF; SHA-256: {digest} -->\n# Edited text')
        (self.markdown.parent / 'preview.html').write_text('Existing preview')
        (self.markdown.parent / 'review').mkdir()
        (self.markdown.parent / 'review/notes.txt').write_text('Keep these notes')
        self.root_patch = patch.object(review, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def make_pdf(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 90), text)
            document.save(path)
        return path

    def copy_source(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.original)
        return path

    def snapshot(self):
        return {path.relative_to(self.markdown.parent): path.read_bytes()
                for path in self.markdown.parent.rglob('*') if path.is_file()}

    def test_expected_path_takes_priority_and_handles_dots_and_uppercase_extension(self):
        self.copy_source('2_pending_review/elsewhere/report.v2.pdf')
        with patch('builtins.input') as prompt:
            self.assertEqual(review.resolve_source(self.markdown), self.pdf)
        prompt.assert_not_called()

    def test_recursive_fallback_uses_hash_to_distinguish_same_name(self):
        self.pdf.unlink()
        self.make_pdf('2_pending_review/a/report.v2.pdf', 'Wrong source')
        matching = self.copy_source('2_pending_review/b/nested/report.v2.PDF')
        with patch('builtins.input') as prompt:
            self.assertEqual(review.resolve_source(self.markdown), matching)
        prompt.assert_not_called()

    def test_mismatched_expected_pdf_does_not_override_matching_fallback(self):
        self.pdf.unlink()
        self.make_pdf('1_downloads/reports/report.v2.PDF', 'Wrong source')
        matching = self.copy_source('2_pending_review/backup/report.v2.pdf')
        self.assertEqual(review.resolve_source(self.markdown), matching)

    def test_identical_copies_are_unambiguous_with_hash(self):
        self.pdf.unlink()
        first = self.copy_source('2_pending_review/a/report.v2.pdf')
        self.copy_source('2_pending_review/b/report.v2.pdf')
        self.assertEqual(review.resolve_source(self.markdown), first)

    def test_legacy_markdown_without_hash_accepts_unique_source(self):
        self.markdown.write_text('# Legacy Markdown')
        self.assertEqual(review.resolve_source(self.markdown), self.pdf)
        self.pdf.unlink()
        matching = self.copy_source('2_pending_review/backup/report.v2.pdf')
        self.assertEqual(review.resolve_source(self.markdown), matching)

    def test_legacy_duplicates_prompt_instead_of_guessing(self):
        self.markdown.write_text('# Legacy Markdown')
        self.pdf.unlink()
        self.copy_source('2_pending_review/a/report.v2.pdf')
        chosen = self.copy_source('2_pending_review/b/report.v2.pdf')
        with patch.object(review.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', return_value=str(chosen)) as prompt:
            self.assertEqual(review.resolve_source(self.markdown), chosen)
        prompt.assert_called_once()

    def test_custom_folder_prompts_and_accepts_quoted_path_with_spaces(self):
        custom = self.root / 'custom/renamed/content.md'
        custom.parent.mkdir(parents=True)
        shutil.copy2(self.markdown, custom)
        chosen = self.copy_source('elsewhere/original report.pdf')
        with patch.object(review.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', return_value=f'"{chosen}"'):
            self.assertEqual(review.resolve_source(custom), chosen)

    def test_cancel_leaves_existing_files_untouched_and_never_starts_browser(self):
        self.pdf.unlink()
        before = self.snapshot()
        for response in ('', KeyboardInterrupt(), EOFError()):
            with self.subTest(response=type(response).__name__):
                output = io.StringIO()
                options = {'return_value': response} if isinstance(response, str) else {'side_effect': response}
                with patch.object(sys, 'argv', ['review.py', str(self.markdown)]), \
                        patch.object(review.sys.stdin, 'isatty', return_value=True), \
                        patch('builtins.input', **options), \
                        patch.object(review, 'find_chrome') as chrome, \
                        patch.object(review, 'preview') as render, redirect_stdout(output):
                    review.main()
                chrome.assert_not_called()
                render.assert_not_called()
                self.assertIn('Review cancelled. No files changed.', output.getvalue())
                self.assertEqual(self.snapshot(), before)

    def test_noninteractive_lookup_failure_stops_without_prompt_or_changes(self):
        self.pdf.unlink()
        before = self.snapshot()
        with patch.object(sys, 'argv', ['review.py', str(self.markdown)]), \
                patch.object(review.sys.stdin, 'isatty', return_value=False), \
                patch('builtins.input') as prompt, patch.object(review, 'preview') as render, \
                patch('sys.stderr', new_callable=io.StringIO) as errors:
            with self.assertRaises(SystemExit) as stopped:
                review.main()
        self.assertEqual(stopped.exception.code, 1)
        self.assertIn('interactive terminal', errors.getvalue())
        prompt.assert_not_called()
        render.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_prompted_source_is_validated_before_rendering(self):
        self.pdf.unlink()
        wrong = self.make_pdf('elsewhere/wrong.pdf', 'Different source')
        invalid = self.root / 'elsewhere/invalid.pdf'
        invalid.write_text('Not a PDF')
        for path in (wrong, invalid, self.root / 'missing.pdf'):
            with self.subTest(path=path):
                before = self.snapshot()
                with patch.object(sys, 'argv', ['review.py', str(self.markdown)]), \
                        patch.object(review.sys.stdin, 'isatty', return_value=True), \
                        patch('builtins.input', return_value=str(path)), \
                        patch.object(review, 'find_chrome') as chrome, \
                        patch.object(review, 'preview') as render, patch('sys.stderr', new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as stopped:
                        review.main()
                self.assertEqual(stopped.exception.code, 1)
                chrome.assert_not_called()
                render.assert_not_called()
                self.assertEqual(self.snapshot(), before)

    def test_legacy_invalid_pdf_is_rejected(self):
        self.markdown.write_text('# Legacy Markdown')
        self.pdf.write_text('Not a PDF')
        with patch.object(review.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', return_value=str(self.pdf)):
            with self.assertRaises((ValueError, RuntimeError)):
                review.resolve_source(self.markdown)

    def test_review_receives_inferred_source_and_existing_options(self):
        with patch.object(sys, 'argv', ['review.py', str(self.markdown), '--inspect', '--chrome', 'my-chrome']), \
                patch.object(review, 'find_chrome', return_value='my-chrome') as chrome, \
                patch.object(review, 'preview') as render:
            review.main()
        chrome.assert_called_once_with('my-chrome')
        render.assert_called_once_with(self.pdf, self.markdown, 'my-chrome', True)


if __name__ == '__main__':
    unittest.main()
