"""Fallback first-row headers keep table data, continuations, and merged cells."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from bs4 import BeautifulSoup
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from convert import convert_pdf
from review import render_body


class TableHeaderTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.doc = pymupdf.open()
        self.addCleanup(self.doc.close)

    def cell(self, page, box, text):
        page.draw_rect(box)
        if text:
            page.insert_text((box[0] + 5, box[1] + 20), text, fontsize=10)

    def grid(self, rows):
        page = self.doc.new_page()
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                self.cell(page, (50 + c * 100, 50 + r * 40, 150 + c * 100, 90 + r * 40), text)

    def convert(self):
        source, destination = self.root / 'table.pdf', self.root / 'out'
        self.doc.save(source)
        convert_pdf(source, destination)
        return (BeautifulSoup(render_body((destination / 'content.md').read_text(), destination, set()), 'html.parser'),
                (destination / 'review/log.md').read_text())

    def test_unbold_first_row_is_header_without_synthetic_labels(self):
        self.grid([['Course', 'Credits'], ['Calculus', '4'], ['Programming', '3']])
        soup, log = self.convert()
        self.assertEqual([th.get_text() for th in soup.select('thead th')], ['Course', 'Credits'])
        self.assertEqual(len(soup.select('tbody tr')), 2)
        self.assertIn('Calculus', soup.table.get_text())
        self.assertNotIn('Column 1', soup.get_text())
        self.assertIn('first row assumed to be the header', log)

    def test_continuation_reuses_headers_and_keeps_its_first_data_row(self):
        self.grid([['Course', 'Credits'], ['Calculus', '4']])
        self.grid([['Programming', '3'], ['Algorithms', '3']])
        soup, log = self.convert()
        tables = soup.select('table')
        self.assertEqual(len(tables), 2)
        self.assertEqual([th.get_text() for th in tables[1].select('thead th')], ['Course', 'Credits'])
        self.assertEqual(len(tables[1].select('tbody tr')), 2)
        self.assertIn('Programming', tables[1].select_one('tbody tr').get_text())
        self.assertIn('Possible continued table', log)

    def test_merged_header_columns_are_preserved(self):
        page = self.doc.new_page()
        self.cell(page, (50, 50, 250, 90), 'Course details')
        self.cell(page, (250, 50, 350, 90), 'Credits')
        for c, text in enumerate(('CSCI', '101', '3')):
            self.cell(page, (50 + c * 100, 90, 150 + c * 100, 130), text)
        soup, log = self.convert()
        self.assertEqual(soup.select_one('thead th')['colspan'], '2')
        self.assertEqual([th.get_text() for th in soup.select('thead th')], ['Course details', 'Credits'])
        self.assertIn('CSCI', soup.select_one('tbody').get_text())
        self.assertIn('first row assumed to be the header', log)

    def test_header_rowspan_does_not_cross_row_groups(self):
        page = self.doc.new_page()
        self.cell(page, (50, 50, 150, 130), 'Group')
        self.cell(page, (150, 50, 250, 90), 'Value')
        self.cell(page, (150, 90, 250, 130), 'First')
        self.cell(page, (50, 130, 150, 170), 'Other')
        self.cell(page, (150, 130, 250, 170), 'Second')
        soup, _ = self.convert()
        self.assertIsNone(soup.select_one('thead'))
        self.assertEqual(soup.select_one('tbody tr th')['rowspan'], '2')
        self.assertIn('First', soup.table.get_text())
        self.assertIn('Second', soup.table.get_text())

    def test_caption_stays_separate_from_first_header_row(self):
        page = self.doc.new_page()
        self.cell(page, (50, 50, 250, 90), 'Degree requirements')
        for r, row in enumerate((('Course', 'Credits'), ('Calculus', '4')), 1):
            for c, text in enumerate(row):
                self.cell(page, (50 + c * 100, 50 + r * 40, 150 + c * 100, 90 + r * 40), text)
        soup, _ = self.convert()
        self.assertEqual(soup.caption.get_text(), 'Degree requirements')
        self.assertEqual([th.get_text() for th in soup.select('thead th')], ['Course', 'Credits'])
        self.assertEqual(len(soup.select('tbody tr')), 1)

    def test_empty_first_row_does_not_drop_following_data(self):
        self.grid([['', ''], ['Course', 'Credits'], ['Calculus', '4']])
        soup, _ = self.convert()
        self.assertEqual(len(soup.select('thead th')), 2)
        self.assertEqual(len(soup.select('tbody tr')), 2)
        self.assertIn('Calculus', soup.table.get_text())


if __name__ == '__main__':
    unittest.main()
