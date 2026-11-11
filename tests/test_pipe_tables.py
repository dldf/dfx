"""Logical PDF grids produce pipe tables without losing span relationships."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bs4 import BeautifulSoup
import pdfplumber
import pymupdf

from dfx.review import render_body
from dfx.tables import detect_tables, make_table


class PipeTableTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.document = pymupdf.open()
        self.addCleanup(self.document.close)

    def cell(self, page, box, text='', *, bold=False, fill=None, outline=True):
        page.draw_rect(box, color=(0, 0, 0) if outline else None, fill=fill)
        if text:
            page.insert_text((box[0] + 5, box[1] + 14), text, fontsize=10,
                             fontname='hebo' if bold else 'helv')

    def output(self, *, continuation=True):
        source = self.root / 'source.pdf'
        self.document.save(source)
        results = []
        previous = None
        with pdfplumber.open(source) as pdf, pymupdf.open(source) as document:
            for number, page in enumerate(pdf.pages, 1):
                tables = detect_tables(page)
                self.assertEqual(len(tables), 1)
                markdown, note, previous = make_table(tables[0], previous if continuation else None,
                                                      number, document[number - 1])
                self.assertNotIn('<table', markdown)
                soup = BeautifulSoup(render_body(markdown, self.root, set()), 'html.parser')
                self.assertIsNone(soup.find(attrs={'rowspan': True}))
                self.assertIsNone(soup.find(attrs={'colspan': True}))
                results.append((soup, markdown, note))
        return results

    def test_nested_header_padding_joins_years_for_filled_and_outlined_rectangles(self):
        for outline in (False, True):
            page = self.document.new_page()
            for col, label in enumerate(('PLO', '', '')):
                x = 50 + col * 100
                self.cell(page, (x, 50, x + 100, 110), label, bold=True,
                          fill=(.8, .8, .8), outline=outline)
                if col:
                    for row, text in enumerate((f'{2023 + col}-', f'{2024 + col}', '')):
                        self.cell(page, (x + 8, 50 + row * 20, x + 92, 70 + row * 20),
                                  text, bold=True, fill=(.8, .8, .8), outline=outline)
            for row in range(2):
                for col, text in enumerate((str(row + 1), 'X', 'X')):
                    x, y = 50 + col * 100, 110 + row * 40
                    self.cell(page, (x, y, x + 100, y + 40), text, outline=outline,
                              fill=(1, 1, 1) if not outline else None)
        for soup, markdown, note in self.output():
            self.assertEqual([h.get_text() for h in soup.select('thead th')],
                             ['PLO', '2024-2025', '2025-2026'])
            self.assertEqual(len(soup.select('tbody tr')), 2)
            self.assertEqual([len(row.find_all(['td', 'th'])) for row in soup.select('tr')], [3, 3, 3])
            self.assertIn('Cell padding normalized throughout', note)

    def grouped_header(self, page):
        self.cell(page, (50, 50, 150, 130), 'Program', bold=True)
        self.cell(page, (150, 50, 350, 90), '2025 Results', bold=True)
        self.cell(page, (150, 90, 250, 130), 'Passed', bold=True)
        self.cell(page, (250, 90, 350, 130), 'Failed', bold=True)
        for row, values in enumerate((('CS', '10', '2'), ('Math', '8', '1'))):
            for col, text in enumerate(values):
                x, y = 50 + col * 100, 130 + row * 40
                self.cell(page, (x, y, x + 100, y + 40), text)

    def test_header_padding_preserves_body_rows_across_drawing_styles(self):
        expected = [['PLO', 'Outcome', '2024-2025', '2025-2026',
                     '2026-2027', '2027-2028', 'Source']]
        expected += [[str(row), f'Outcome {row}', 'X', 'X', 'X', 'X', f'Source {row}']
                     for row in range(1, 5)]
        for style in ('lines', 'outlines', 'fills', 'bands'):
            page = self.document.new_page()
            for row, values in enumerate(expected):
                for col, value in enumerate(values):
                    x, y = 40 + col * 72, 40 + row * 60
                    box = (x, y, x + 72, y + 60)
                    if row == 0:
                        # MS layout: large header rectangles and inset year
                        # fragments, with independently drawn body borders.
                        self.cell(page, box, fill=(.9, .9, .9), outline=False)
                    elif style in ('outlines', 'fills'):
                        self.cell(page, box, outline=style == 'outlines',
                                  fill=(1, 1, 1) if style == 'fills' else None)
                    else:
                        corners = [(x, y), (x + 72, y), (x + 72, y + 60), (x, y + 60)]
                        for start, stop in zip(corners, corners[1:] + corners[:1]):
                            if style == 'lines':
                                page.draw_line(start, stop)
                            else:
                                page.draw_rect((min(start[0], stop[0]) - .2,
                                                min(start[1], stop[1]) - .2,
                                                max(start[0], stop[0]) + .2,
                                                max(start[1], stop[1]) + .2),
                                               color=None, fill=(0, 0, 0))
                    if row == 0 and 2 <= col <= 5:
                        for line, part in enumerate((value[:5], value[5:], '')):
                            self.cell(page, (x + 6, y + line * 20, x + 66, y + (line + 1) * 20),
                                      part, bold=True, fill=(.9, .9, .9), outline=False)
                    else:
                        page.insert_text((x + 5, y + 14), value, fontsize=10,
                                         fontname='hebo' if row == 0 else 'helv')
        for style, (soup, _, _) in zip(('lines', 'outlines', 'fills', 'bands'),
                                      self.output(continuation=False)):
            with self.subTest(style=style):
                self.assertEqual([[cell.get_text() for cell in row.find_all(['th', 'td'])]
                                  for row in soup.select('tr')], expected)

    def test_padding_in_header_and_body_recovers_grid_and_values_once(self):
        expected = [
            ['', '2024-2025', '2025-2026', 'Comments'],
            ['Qualifying Exams', '5', '6', 'One student left'],
            ['Comprehensive Exams', '1', '3', ''],
            ['Doctoral Dissertation', '3', '2', ''],
            ['Doctoral Defense', '3', '2', ''],
            ['All PhD Student Publications', '24', '29', ''],
            ['PhD Student AI Publications', '14', '14', 'New subset'],
        ]
        for outline in (False, True):
            page = self.document.new_page()
            for row, values in enumerate(expected):
                for col, value in enumerate(values):
                    x, y = 40 + col * 130, 40 + row * 45
                    self.cell(page, (x, y, x + 130, y + 45), outline=outline, fill=(.9, .9, .9))
                    if row == 0 and col in (1, 2):
                        parts = [value[:5], value[5:]]
                    elif len(value) > 18:
                        words = value.split()
                        parts = [' '.join(words[:-1]), words[-1]]
                    else:
                        parts = [value, '']
                    for line, part in enumerate(parts):
                        self.cell(page, (x + 5, y + line * 22.5, x + 125, y + (line + 1) * 22.5),
                                  part, outline=outline, fill=(.9, .9, .9))
            # A trailing space painted inside another word must not split it.
            page.insert_text((72, 101), ' ', fontsize=10)
        for soup, _, note in self.output(continuation=False):
            self.assertEqual([[cell.get_text() for cell in row.find_all(['th', 'td'])]
                              for row in soup.select('tr')], expected)
            self.assertIn('Cell padding normalized throughout', note)

    def test_padding_does_not_hide_a_real_internal_rule(self):
        page = self.document.new_page()
        for row in range(2):
            for col in range(2):
                x, y = 50 + col * 150, 50 + row * 60
                self.cell(page, (x, y, x + 150, y + 60))
                if row:
                    for line, text in enumerate(('Top', 'Bottom')):
                        self.cell(page, (x + 5, y + line * 30, x + 145, y + (line + 1) * 30), text)
        page.draw_line((125, 50), (125, 110))
        page.insert_text((60, 70), 'A', fontname='hebo')
        page.insert_text((135, 70), 'B', fontname='hebo')
        page.insert_text((210, 70), 'C', fontname='hebo')
        source = self.root / 'source.pdf'
        self.document.save(source)
        with pdfplumber.open(source) as pdf:
            table, = detect_tables(pdf.pages[0])
            self.assertFalse(getattr(table, 'padding_normalized', False))
            self.assertTrue(any(abs(cell[0] - 125) < 1 for cell in table.cells))

    def test_full_width_body_subdivisions_are_not_padding(self):
        page = self.document.new_page()
        for row in range(3):
            for col in range(2):
                x, y = 50 + col * 150, 50 + row * 60
                self.cell(page, (x, y, x + 150, y + 60))
                if row == 1 and col == 0:
                    for line, text in enumerate(('Separate A', 'Separate B')):
                        self.cell(page, (x, y + line * 30, x + 150, y + (line + 1) * 30), text)
                elif row:
                    self.cell(page, (x + 5, y, x + 145, y + 30), 'Value')
        source = self.root / 'source.pdf'
        self.document.save(source)
        with pdfplumber.open(source) as pdf:
            table, = detect_tables(pdf.pages[0])
            self.assertFalse(getattr(table, 'padding_normalized', False))

    def test_padded_text_painted_out_of_order_keeps_visual_order(self):
        page = self.document.new_page()
        for row in range(2):
            for col in range(2):
                x, y = 50 + col * 150, 50 + row * 60
                self.cell(page, (x, y, x + 150, y + 60))
                for line in (1, 0):
                    self.cell(page, (x + 5, y + line * 30, x + 145, y + (line + 1) * 30),
                              'First' if line == 0 else 'Second')
        soup, _, _ = self.output()[0]
        self.assertEqual([cell.get_text() for cell in soup.select('tr > *')], ['First Second'] * 4)

    def test_grouped_headings_flatten_and_continue_without_consuming_data(self):
        self.grouped_header(self.document.new_page())
        page = self.document.new_page()
        for col, text in enumerate(('Physics', '7', '3')):
            x = 50 + col * 100
            self.cell(page, (x, 50, x + 100, 90), text)
        first, second = self.output()
        expected = ['Program', '2025 Results — Passed', '2025 Results — Failed']
        for soup, _, _ in (first, second):
            self.assertEqual([h.get_text() for h in soup.select('thead th')], expected)
        self.assertEqual(len(first[0].select('tbody tr')), 2)
        self.assertEqual([c.get_text() for c in second[0].select('tbody tr')[0].find_all(['td', 'th'])],
                         ['Physics', '7', '3'])
        self.assertIn('Possible continued table', second[2])

    def test_different_body_grids_split_in_order_with_shared_heading_context(self):
        page = self.document.new_page()
        for row, values in ((0, ('Field', 'Answer')), (1, ('Age', '20')), (3, ('Name', 'Ada'))):
            for col, text in enumerate(values):
                x, y = 50 + col * 150, 50 + row * 40
                self.cell(page, (x, y, x + 150, y + 40), text, bold=row == 0)
        self.cell(page, (50, 130, 350, 170), 'Shared <note> | keep [text]')
        soup, markdown, note = self.output()[0]
        tables = soup.select('table')
        self.assertEqual(len(tables), 3)
        self.assertEqual([h.get_text() for h in tables[1].select('thead th')], ['Field — Answer'])
        self.assertIn('Shared <note> | keep [text]', tables[1].get_text())
        self.assertIn('Age', tables[0].get_text())
        self.assertIn('Ada', tables[2].get_text())
        self.assertEqual(markdown.count('Shared'), 1)
        self.assertIn('split into separate pipe tables', note)

    def test_body_rowspan_requests_source_preservation_instead_of_fabricating_cells(self):
        page = self.document.new_page()
        self.cell(page, (50, 50, 200, 90), 'Total', bold=True)
        self.cell(page, (200, 50, 350, 90), 'Category', bold=True)
        self.cell(page, (50, 90, 200, 170), '123')
        self.cell(page, (200, 90, 350, 130), 'A')
        self.cell(page, (200, 130, 350, 170), 'B')
        source = self.root / 'source.pdf'
        self.document.save(source)
        with pdfplumber.open(source) as pdf:
            table, = detect_tables(pdf.pages[0])
            markdown, note, state = make_table(table, None, 1, page)
        self.assertIsNone(markdown)
        self.assertIsNone(state)
        self.assertIn('source region preserved as an image', note)

    def test_side_by_side_header_subcells_are_not_collapsed_as_padding(self):
        page = self.document.new_page()
        self.cell(page, (50, 50, 150, 90), 'A', bold=True)
        self.cell(page, (150, 50, 250, 90), 'B', bold=True)
        self.cell(page, (250, 50, 350, 90), 'C', bold=True)
        for row in range(2):
            y = 90 + row * 40
            self.cell(page, (50, y, 250, y + 40), f'Combined {row}')
            self.cell(page, (250, y, 350, y + 40), f'Single {row}')
        soup, _, note = self.output()[0]
        self.assertEqual([h.get_text() for h in soup.select('thead th')], ['A — B', 'C'])
        self.assertNotIn('Header padding', note)
        self.assertEqual(len(soup.select('tbody tr')), 2)


if __name__ == '__main__':
    unittest.main()
