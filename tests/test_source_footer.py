"""Source URLs come from verified download records and remain unlinked."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
import pymupdf

from dfx import convert
from dfx.review import render_body


class SourceFooterTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / '1_download/example.org/report.pdf'
        self.source.parent.mkdir(parents=True)
        with pymupdf.open() as document:
            for text in ('First page with enough ordinary text for conversion.',
                         'Last page with enough ordinary text for conversion.'):
                page = document.new_page()
                page.insert_text((72, 90), text)
                page.insert_text((520, 800), str(page.number + 1))
            document.save(self.source)
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.url = 'https://example.org/original/report.pdf?download=1&lang=en'
        self.record = {'local_path': str(self.source.relative_to(self.root)), 'sha256': self.digest}
        self.root_patch = patch.object(convert, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def inventory(self, documents):
        (self.root / 'data').mkdir(exist_ok=True)
        (self.root / 'data/pdfs.json').write_text(json.dumps({'version': 1, 'documents': documents}))

    def test_notice_is_once_at_end_and_url_is_unlinked_in_html(self):
        self.inventory({self.url: self.record})
        folder = self.root / 'converted'
        convert.convert_pdf(self.source, folder)
        markdown = (folder / 'content.md').read_text()
        notice = f'This page was converted from `{self.url}`'
        self.assertTrue(markdown.endswith(notice + '\n'))
        self.assertEqual(markdown.count('This page was converted'), 1)
        self.assertGreater(markdown.index(notice), markdown.index('2\n---:'))
        soup = BeautifulSoup(render_body(markdown, folder, set()), 'html.parser')
        self.assertEqual(soup.find_all('p')[-1].code.get_text(), self.url)
        self.assertIsNone(soup.find('a', href=self.url))
        self.assertNotIn('Original URL unavailable', (folder / 'review/log.md').read_text())

    def test_missing_inventory_flags_provenance_without_inventing_a_footer(self):
        folder = self.root / 'converted'
        convert.convert_pdf(self.source, folder)
        self.assertNotIn('This page was converted', (folder / 'content.md').read_text())
        self.assertIn('Source: Original URL unavailable', (folder / 'review/log.md').read_text())

    def test_mismatched_hash_or_path_and_ambiguous_urls_are_not_used(self):
        cases = [
            {self.url: dict(self.record, sha256='0' * 64)},
            {self.url: dict(self.record, local_path='1_download/elsewhere/report.pdf')},
            {self.url: self.record, 'https://elsewhere.example/report.pdf': self.record},
            {'file:///tmp/report.pdf': self.record},
            {'https://example.org/report.pdf\nInjected': self.record},
            {'https://[invalid/report.pdf': self.record},
        ]
        for records in cases:
            with self.subTest(records=records):
                self.inventory(records)
                self.assertIsNone(convert.original_source_url(self.source, self.digest))

    def test_backtick_in_recorded_url_remains_literal(self):
        url = 'https://example.org/a`b.pdf'
        self.inventory({url: self.record})
        folder = self.root / 'converted'
        convert.convert_pdf(self.source, folder)
        markdown = (folder / 'content.md').read_text()
        soup = BeautifulSoup(render_body(markdown, folder, set()), 'html.parser')
        self.assertEqual(soup.find_all('p')[-1].code.get_text(), url)
        self.assertIsNone(soup.find_all('p')[-1].find('a'))


if __name__ == '__main__':
    unittest.main()
