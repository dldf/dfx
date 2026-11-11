"""Check that review warnings survive relocation out of the Markdown."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import pymupdf
from bs4 import BeautifulSoup

from dfx.convert import convert_pdf, format_review_log
from dfx.review import render_body
from dfx.review_log import review_issues


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

    def test_repeated_image_warnings_become_one_issue(self):
        folder, result = self.convert(flagged=True)
        markdown = (folder / 'content.md').read_text()
        log = (folder / 'review/log.md').read_text()
        self.assertEqual(result, (2, 1))
        self.assertTrue(log.startswith('# source\n'))
        self.assertIn('## 2 pages, 1 flagged for review:', log)
        self.assertEqual(log.count('| **2** |'), 1)
        self.assertIn('[PDF](../../source.pdf)', log)
        self.assertIn('[MD](../content.md)', log)
        self.assertNotIn('#page=', log)
        self.assertNotIn('content.md#source-page', log)
        self.assertIn('Image: Description needed', log)
        self.assertNotIn('Image: Description needed', markdown)
        self.assertNotIn('Text: Little or no extractable text', log)
        self.assertNotIn('**Review page', markdown)
        self.assertNotIn('Conversion draft', markdown)
        self.assertNotIn('**Review font', markdown)
        self.assertNotIn('description needs review', markdown)
        self.assertNotIn('Headings, reading order, and tables are inferred', log)
        self.assertIn('`assets/page-2-graphics.png`', log)
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
            pdf = root / '1_download' / 'A report.pdf'
            markdown = root / '2_review' / 'A report' / 'content.md'
            log = format_review_log(pdf, markdown, 4, {1: [('Spelling: Suspect characters', ('word',))],
                                                      4: [('Text: A | B <label>', ('A | B <label>', 'a`b'))]})
            self.assertIn('[PDF](../../../1_download/A%20report.pdf)', log)
            self.assertIn('[MD](../content.md)', log)
            soup = BeautifulSoup(render_body(log, markdown.parent, set()), 'html.parser')
            links, issues = soup.select('table')
            self.assertEqual(len(links.select('thead th')), 2)
            self.assertEqual(len(links.select('tbody tr')), 1)
            warning_row = issues.select('tbody tr')[1]
            self.assertEqual(len(warning_row.select('td')), 3)
            self.assertEqual(warning_row.select('td')[1].get_text(), 'Text: A | B <label>')
            self.assertEqual([code.get_text() for code in warning_row.select('code')], ['A | B <label>', 'a`b'])
            self.assertEqual(len(warning_row.select('br')), 1)
            self.assertIsNone(issues.find('a'))

    def test_spelling_flags_use_actual_search_strings_without_font_diagnostics(self):
        markdown = '<a id="source-page1"></a>\nCompuCng SubmiIed Section'
        notes = ["Corrected ':' to 'ti' for AAAAAI+Calibri-Bold glyph 38: 1 occurrences, verified by OCR. Confirm against the page image.",
                 "Suspicious font/character mappings remain unverified; extracted text retained: 'CompuCng', 'SubmiIed'. Check against the page image."]
        issues = review_issues(notes, markdown, 1)
        self.assertEqual(issues, [('Spelling: Suspect characters', ('CompuCng', 'SubmiIed'))])

    def test_generated_caption_remains_flagged_without_routine_table_notices(self):
        first = ': Table: Majors\n\n| Majors | Count |\n| --- | --- |\n| CS | 5 |'
        second = '| Program | Total |\n| --- | --- |\n| Math | 6 |'
        notes = [('Table structure and column headings inferred; check cell associations. '
                  'Caption inferred from the column headings. First column inferred as row headers; confirm or set row_header to 0.', first),
                 ('No header detected; first row assumed to be the header. Column boundaries uncertain; automatic layout used.', second)]
        issues = review_issues(notes, first + '\n\n' + second, 1)
        self.assertEqual(issues, [('Table: Caption inferred', ('Table: Majors',))])

    def test_word_counts_never_generate_token_or_page_anchor_alerts(self):
        note = ('Possible text loss compared with independent extraction: '
                '6 (1), left (1), the (1), ex (1), ams (1), absent (1), ______ (1). Check the page image.')
        for markdown in ('<a id="source-page5"></a>\nRemaining text.', 'Exams left the 6 [---]{.blank}'):
            with self.subTest(markdown=markdown):
                self.assertEqual(review_issues([note], markdown, 5), [])

    def test_routine_inference_does_not_create_flags(self):
        notes = ['Checkbox states and labels inferred from position; confirm the selected answers.',
                 'Table structure and column headings inferred; check cell associations. '
                 'First column inferred as row headers; confirm or set row_header to 0.',
                 'No header detected; first row assumed to be the header.',
                 'Possible continued table: column headings reused from the preceding page.',
                 'Grouped column headings flattened into pipe-table labels.',
                 'Different body column layouts split into separate pipe tables.',
                 'Column boundaries uncertain; automatic layout used.',
                 'Header padding and wrapped labels normalized to the body column grid.',
                 'Cell padding normalized throughout the table grid.',
                 'Page-sized single-column layout container treated as text; confirm reading order.']
        self.assertEqual(review_issues(notes, '[ ] Yes\\\n[x] No', 1), [])

    def test_preserved_table_gets_one_specific_issue_even_with_other_image_notes(self):
        table = '![Source image on page 5](assets/page-5-table-1.png)'
        figure = '![Source image on page 5](assets/page-5-graphics.png)'
        notes = [('Ambiguous cell spans: source region preserved as an image.', table),
                 'Vector graphics preserved as an image; check whether they are decorative.',
                 'Little or no extractable text. Page image preserved.',
                 'Possible text loss compared with independent extraction: 6 (1), left (1).']
        self.assertEqual(review_issues(notes, table, 5),
                         [('Table: Preserved as image', ('assets/page-5-table-1.png',))])
        self.assertEqual(review_issues(notes, table + '\n\n' + figure, 5),
                         [('Table: Preserved as image', ('assets/page-5-table-1.png',)),
                          ('Image: Description needed', ('assets/page-5-graphics.png',))])

    def test_scanned_image_has_one_flag_and_real_failures_remain(self):
        markdown = '![Scan](assets/page-1.png)\n\nftp://example.org/report'
        notes = ['Likely scanned/image-only page: a bitmap covers most of the page.',
                 'Little or no extractable text. Page image preserved.',
                 'Image preserved; add a meaningful description.',
                 'Table extraction failed (ValueError); check the page image.',
                 'Link with unsupported scheme retained as text: ftp://example.org/report']
        self.assertEqual(review_issues(notes, markdown, 1),
                         [('Table: Extraction failed', ('source-page1',)),
                          ('Link: Unsupported address', ('ftp://example.org/report',)),
                          ('Text: Image only', ('assets/page-1.png',))])

if __name__ == '__main__':
    unittest.main()
