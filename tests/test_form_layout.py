"""Keep page layout containers out of table extraction and supply missing headings."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from bs4 import BeautifulSoup
import pdfplumber
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from convert import convert_pdf
from review import document_title, ensure_document_heading, render_body


class FormLayoutTests(unittest.TestCase):
    def test_full_page_layout_preserves_logo_heading_and_numbered_prose(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / 'request_form.pdf', root / 'output'
            with pymupdf.open() as document:
                page = document.new_page(width=612, height=792)
                page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
                page.draw_rect((0, 100, 612, 792), color=None, fill=(1, 1, 1))
                logo = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 20, 20), False)
                logo.clear_with(128)
                page.insert_image((81, 0, 171, 90), stream=logo.tobytes('png'))
                page.insert_text((220, 50), 'School of Computing', fontname='hebo', fontsize=20)
                for text, x, y in (
                    ('Request to waive a course', 72, 120),
                    ('A student may request a waiver if:', 72, 150),
                    ('1.', 90, 170), ('An advisor recommends a waiver.', 108, 170),
                    ('2.', 90, 195), ('The student passes a higher course.', 108, 195),
                    ('Submit this form before enrollment.', 72, 225),
                    ('Student name: __________________', 72, 255),
                ):
                    page.insert_text((x, y), text, fontsize=10)
                document.save(source)
            with pdfplumber.open(source) as pdf:
                self.assertEqual(len(pdf.pages[0].find_tables()), 1)
            convert_pdf(source, output)
            markdown = (output / 'content.md').read_text()
            soup = BeautifulSoup(render_body(markdown, output, set()), 'html.parser')
            self.assertIsNone(soup.find('table'))
            self.assertEqual(soup.h1.get_text(), 'School of Computing')
            self.assertEqual(len(soup.select('ol > li')), 2)
            self.assertLess(markdown.index('![Source image'), markdown.index('# School'))
            self.assertEqual(len(soup.find_all('img')), 1)
            self.assertIn('layout container treated as text', (output / 'review/log.md').read_text())

    def test_legitimate_tables_are_retained(self):
        for columns, rows, width, height in ((2, 2, 612, 792), (1, 3, 612, 792), (1, 2, 300, 150), (1, 2, 612, 792)):
            with self.subTest(columns=columns, rows=rows, width=width, height=height), TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, output = root / 'table.pdf', root / 'output'
                with pymupdf.open() as document:
                    page = document.new_page(width=612, height=792)
                    for row in range(rows):
                        for column in range(columns):
                            x, y = column * width / columns, row * height / rows
                            page.draw_rect((x, y, x + width / columns, y + height / rows))
                            page.insert_text((x + 10, y + 20), f'Cell {row + 1} {column + 1}', fontsize=10)
                    document.save(source)
                convert_pdf(source, output)
                soup = BeautifulSoup(render_body((output / 'content.md').read_text(), output, set()), 'html.parser')
                self.assertIsNotNone(soup.find('table'))
                self.assertIn('Cell 1 1', soup.get_text())
                self.assertNotIn('layout container treated as text', (output / 'review/log.md').read_text())


class DocumentHeadingTests(unittest.TestCase):
    def test_filename_title_and_acronyms(self):
        self.assertEqual(document_title('request_to_waive_a_provisional_course'),
                         'Request To Waive A Provisional Course')
        self.assertEqual(document_title('CSCI_course__request'), 'CSCI Course Request')

    def test_missing_h1_uses_escaped_document_title_at_start(self):
        bodies = ['<p>Body text</p>', '<h2>Section</h2>']
        updated = ensure_document_heading(bodies, 'A & B <Request>')
        self.assertTrue(updated[0].startswith('<h1>A &amp; B &lt;Request&gt;</h1>'))
        self.assertEqual(bodies, ['<p>Body text</p>', '<h2>Section</h2>'])
        self.assertEqual(updated[1], bodies[1])

    def test_existing_h1_on_any_page_prevents_duplicate(self):
        for bodies in (['<h1>Authored heading</h1><p>Body</p>'],
                       ['<p>Cover page</p>', '<h1>Later heading</h1>']):
            with self.subTest(bodies=bodies):
                self.assertEqual(ensure_document_heading(bodies, 'Fallback'), bodies)


if __name__ == '__main__':
    unittest.main()
