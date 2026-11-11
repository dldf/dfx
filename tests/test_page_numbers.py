"""Printed footer numbers retain alignment without review flags."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pymupdf
from bs4 import BeautifulSoup

from dfx.convert import convert_pdf
from dfx.review import render_body


class PageNumberTests(unittest.TestCase):
    def convert(self, footers, *, extra=None):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source, output = root / 'source.pdf', root / 'converted'
        with pymupdf.open() as document:
            for text, x, y in footers:
                page = document.new_page(width=600, height=800)
                page.insert_text((70, 90), 'A normal paragraph with enough text for a complete page.')
                page.insert_text((x, y), text)
                if extra:
                    extra(page)
            document.save(source)
        result = convert_pdf(source, output)
        markdown = (output / 'content.md').read_text()
        soup = BeautifulSoup(render_body(markdown, output, set()), 'html.parser')
        return markdown, soup, result, (output / 'review/log.md').read_text()

    def test_all_alignments_and_inclusive_limits_without_flags(self):
        markdown, soup, result, log = self.convert([('1', 70, 770), ('37', 295, 770), ('1000', 520, 770)])
        self.assertEqual(result, (3, 0))
        for text, marker in (('1', ':---'), ('37', ':---:'), ('1000', '---:')):
            self.assertIn(f'\n\n{text}\n{marker}\n', markdown)
        footers = soup.select('p[style]')
        self.assertEqual([p.get_text() for p in footers], ['1', '37', '1000'])
        self.assertEqual([p['style'] for p in footers], ['text-align:left', 'text-align:center', 'text-align:right'])
        self.assertTrue(all(p.find_next_sibling().name == 'hr' for p in footers))
        self.assertIn('0 flagged', log)

    def test_numbers_outside_range_or_bottom_margin_stay_text(self):
        markdown, soup, _, _ = self.convert([('0', 520, 770), ('1001', 520, 770),
                                             ('3', 520, 400), ('-3', 520, 770), ('3.0', 520, 770)])
        self.assertEqual(soup.select('p[style]'), [])
        self.assertNotIn('---:', markdown)

    def test_number_beside_other_text_is_not_a_footer(self):
        _, soup, _, _ = self.convert([('3', 520, 770)],
                                     extra=lambda page: page.insert_text((70, 770), 'Total'))
        self.assertEqual(soup.select('p[style]'), [])

    def test_number_above_last_text_line_is_not_a_footer(self):
        _, soup, _, _ = self.convert([('3', 520, 745)],
                                     extra=lambda page: page.insert_text((70, 780), 'Final note'))
        self.assertEqual(soup.select('p[style]'), [])

    def test_number_in_table_is_not_a_footer(self):
        def table(page):
            for box, text in (((480, 720, 570, 745), 'Count'), ((480, 745, 570, 790), '')):
                page.draw_rect(box)
                if text:
                    page.insert_text((490, 737), text)
        markdown, soup, _, _ = self.convert([('3', 520, 770)], extra=table)
        self.assertEqual(soup.select('p[style]'), [])
        self.assertIn('| 3 |', markdown)

    def test_renderer_preserves_examples_and_other_rules(self):
        markdown = '```markdown\n3\n---:\n```\n\n> 3\n> ---:\n\n0\n---:\n\n1001\n---:\n\n---\n'
        soup = BeautifulSoup(render_body(markdown, Path('.'), set()), 'html.parser')
        self.assertEqual(soup.select('p[style]'), [])
        self.assertIn('3\n---:', soup.code.get_text())
        self.assertEqual(len(soup.select('hr')), 1)


if __name__ == '__main__':
    unittest.main()
