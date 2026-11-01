"""Check that review warnings survive relocation out of the Markdown."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import pymupdf
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from convert import convert_pdf, format_review_log
from review import render_body


class ReviewLogTests(unittest.TestCase):
    def convert(self, flagged=False):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        pdf = root / 'source.pdf'
        destination = root / 'converted'
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 90), 'A plain paragraph with enough text to need no page review.')
            if flagged:
                page = document.new_page()
                page.draw_circle((150, 150), 30)
                page.insert_text((130, 150), 'Figure')
            document.save(pdf)
        result = convert_pdf(pdf, destination)
        self.assertEqual(list((destination / 'review').glob('commands_*.md')), [])
        return destination, result

    def test_multiple_warnings_count_as_one_flagged_page(self):
        folder, result = self.convert(flagged=True)
        markdown = (folder / 'content.md').read_text()
        log = (folder / 'review/log.md').read_text()
        self.assertEqual(result, (2, 1))
        self.assertTrue(log.startswith('# source\n'))
        self.assertIn('## 2 pages, 1 flagged for review:', log)
        self.assertEqual(log.count('| **2** |'), 1)
        self.assertIn('[PDF](../../source.pdf#page=2)', log)
        self.assertIn('[MD](../content.md#source-page2)', log)
        for reason in ('Vector graphics preserved as an image', 'Little or no extractable text'):
            self.assertIn(reason, log)
            self.assertNotIn(reason, markdown)
        self.assertNotIn('**Review page', markdown)
        self.assertNotIn('Conversion draft', markdown)
        self.assertNotIn('**Review font', markdown)
        self.assertNotIn('description needs review', markdown)
        self.assertIn('Headings, reading order, and tables are inferred', log)
        self.assertIn('[index.HTML](../index.html)', log)
        self.assertTrue((folder / 'review/p002-original.png').exists())
        self.assertFalse((folder / 'review/log.txt').exists())
        for number in (1, 2):
            self.assertEqual(markdown.count(f'<a id="source-page{number}"></a>'), 1)
        soup = BeautifulSoup(render_body(markdown, folder, set()), 'html.parser')
        self.assertEqual(len(soup.select('a[id^="source-page"]')), 2)
        self.assertFalse(any(anchor.parent.name == 'p' for anchor in soup.select('a[id^="source-page"]')))

    def test_unflagged_document_also_has_a_log(self):
        folder, result = self.convert()
        self.assertEqual(result, (1, 0))
        log = (folder / 'review/log.md').read_text()
        self.assertIn('## 1 pages, 0 flagged for review:', log)
        self.assertNotIn('| **1** |', log)
        self.assertNotIn('python ', log)
        self.assertNotIn('**Review page', (folder / 'content.md').read_text())

    def test_links_and_table_escaping(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / '1_downloads' / 'A report.pdf'
            markdown = root / '2_pending_review' / 'A report' / 'content.md'
            log = format_review_log(pdf, markdown, 4, {4: ['Check A | B <label>.', 'Check A | B <label>.']})
            self.assertIn('[original.PDF](../../../1_downloads/A%20report.pdf)', log)
            self.assertIn('[content.MD](../content.md)', log)
            soup = BeautifulSoup(render_body(log, markdown.parent, set()), 'html.parser')
            warning_row = soup.select('table')[0].select('tbody tr')[0]
            self.assertEqual(len(warning_row.select('td')), 4)
            self.assertEqual(warning_row.select('td')[1].get_text(), 'Check A | B <label>.')
            self.assertEqual(len(soup.select('table')), 1)

if __name__ == '__main__':
    unittest.main()
