"""Regression checks for text decorations and checkbox answer preservation."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import pymupdf
from bs4 import BeautifulSoup

from dfx.convert import bullet_details, convert_pdf
from dfx.review import render_body


class TextGraphicsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.document = pymupdf.open()
        self.addCleanup(self.document.close)
        self.page = self.document.new_page()

    def convert(self):
        source = self.root / 'source.pdf'
        destination = self.root / 'converted'
        self.document.save(source)
        convert_pdf(source, destination)
        return (destination / 'content.md').read_text(), list(destination.glob('assets/*.png'))

    def test_highlight_keeps_text_without_image(self):
        self.page.draw_rect((70, 78, 147, 94), fill=(1, 1, 0), color=None)
        self.page.insert_text((72, 90), 'Required answer: explain the result.')
        markdown, images = self.convert()
        self.assertIn('Required answer: explain the result.', markdown.replace('<mark>', '').replace('</mark>', ''))
        self.assertIn('<mark>', markdown)
        self.assertEqual(images, [])

    def test_shaded_panel_keeps_text_without_image(self):
        self.page.draw_rect((70, 70, 350, 150), fill=(.77, .85, .94), color=None)
        self.page.insert_text((80, 90), 'Assessment instructions')
        self.page.insert_text((80, 120), 'Keep this entire paragraph as text.')
        markdown, images = self.convert()
        self.assertIn('Keep this entire paragraph as text.', markdown)
        self.assertNotIn('<mark>', markdown)
        self.assertEqual(images, [])

    def test_small_highlight_is_not_mistaken_for_checkbox(self):
        self.page.draw_rect((70, 78, 90, 94), fill=(1, 1, 0), color=None)
        self.page.insert_text((70, 90), 'TBD', fontsize=9)
        markdown, images = self.convert()
        self.assertIn('TBD', markdown)
        self.assertEqual(images, [])

    def test_thick_rule_and_border_do_not_create_images(self):
        self.page.draw_rect((70, 70, 350, 115), width=2)
        self.page.insert_text((80, 90), 'Submission deadline: October 15.')
        self.page.draw_line((70, 140), (350, 140), width=3)
        markdown, images = self.convert()
        self.assertIn('October 15', markdown)
        self.assertEqual(images, [])

    def test_selected_no_stays_with_no(self):
        self.page.draw_rect((70, 80, 86, 96), width=2)
        self.page.draw_rect((200, 79.5, 216, 95.5), width=2)
        self.page.insert_text((91, 94), 'Yes')
        self.page.insert_text((225, 94), 'No')
        self.page.insert_text((202, 92), 'X')
        markdown, images = self.convert()
        self.assertIn('- [ ] Yes\n- [x] No', markdown)
        self.assertNotIn('X Yes', markdown)
        self.assertEqual(images, [])

    def test_ambiguous_label_keeps_source_representation(self):
        self.page.draw_rect((200, 80, 216, 96), width=2)
        # Equally close labels on opposite sides cannot identify the answer.
        self.page.insert_text((200 - pymupdf.get_text_length('Yes') - 5, 94), 'Yes')
        self.page.insert_text((221, 94), 'No')
        self.page.insert_text((202, 92), 'X')
        markdown, images = self.convert()
        self.assertNotIn('- [x]', markdown)
        # pdfplumber may retain the box as a one-cell table instead of a PNG.
        self.assertTrue(images or '| X |' in markdown)

    def test_real_figure_keeps_image(self):
        self.page.draw_circle((200, 180), 35, color=(0, 0, 1))
        self.page.insert_text((175, 182), 'A figure')
        markdown, images = self.convert()
        self.assertIn('A figure', markdown)
        self.assertEqual(len(images), 1)

    def test_later_opaque_rectangle_is_not_treated_as_background(self):
        self.page.insert_text((80, 90), 'Covered text')
        self.page.draw_rect((70, 70, 170, 100), fill=(1, 1, 0), color=None)
        markdown, images = self.convert()
        self.assertIn('Covered text', markdown)
        self.assertNotIn('<mark>', markdown)
        self.assertEqual(len(images), 1)

    def test_highlight_splits_a_span_at_word_boundaries(self):
        start = 72 + pymupdf.get_text_length('Before ')
        end = start + pymupdf.get_text_length('highlighted')
        self.page.draw_rect((start, 78, end, 94), fill=(1, 1, 0), color=None)
        self.page.insert_text((72, 90), 'Before highlighted after.')
        markdown, images = self.convert()
        self.assertIn('Before <mark>highlighted</mark> after.', markdown)
        self.assertEqual(images, [])

    def test_highlight_preserves_heading_and_italic(self):
        self.page.draw_rect((70, 74, 160, 96), fill=(1, 1, 0), color=None)
        self.page.insert_text((72, 90), 'Emphasis', fontname='hebi', fontsize=14)
        markdown, _ = self.convert()
        soup = BeautifulSoup(render_body(markdown, self.root, set()), 'html.parser')
        marked = soup.select_one('mark')
        self.assertIsNotNone(marked)
        self.assertIsNotNone(marked.find_parent('h4'))
        self.assertIsNotNone(marked.select_one('em'))
        self.assertIn('#### ', markdown)
        self.assertEqual(marked.get_text(), 'Emphasis')

    def test_pdf_highlight_annotation_is_text_not_png(self):
        self.page.insert_text((72, 90), 'A highlighted word.')
        self.page.add_highlight_annot(self.page.search_for('highlighted'))
        markdown, images = self.convert()
        self.assertIn('A <mark>highlighted</mark> word.', markdown)
        self.assertEqual(images, [])

    def test_symbol_font_bullet_is_recognized_contextually(self):
        spans = [
            {'text': '\uf0b7', 'font': 'SymbolMT', 'size': 12, 'bbox': (72, 70, 78, 84),
             'chars': [{'c': '\uf0b7', 'bbox': (72, 70, 78, 84)}]},
            {'text': ' Text', 'font': 'TimesNewRomanPSMT', 'size': 12, 'bbox': (78, 70, 115, 84),
             'chars': [{'c': ' ', 'bbox': (78, 70, 82, 84)},
                       {'c': 'T', 'bbox': (90, 70, 97, 84)}]},
        ]
        self.assertEqual(bullet_details(spans)['body_x'], 90)
        spans[0]['font'] = 'UnrelatedFont'
        self.assertIsNone(bullet_details(spans))

    def add_bullet(self, page, x, y, text, nested=False):
        page.insert_text((x, y), 'o' if nested else '•', fontname='cour' if nested else 'helv')
        page.insert_text((x + 18, y), text)

    def test_nested_list_and_wrapping_continue_across_pages(self):
        self.add_bullet(self.page, 72, 90, 'First parent')
        self.add_bullet(self.page, 108, 110, 'First child', nested=True)
        self.page.insert_text((126, 124), 'with a wrapped continuation.')
        self.add_bullet(self.page, 72, 145, 'Second parent')
        second = self.document.new_page()
        self.add_bullet(second, 108, 90, 'Child on the next page.', nested=True)
        self.add_bullet(second, 72, 115, 'Third parent')
        markdown, images = self.convert()
        self.assertIn('    - First child with a wrapped continuation.', markdown)
        self.assertIn('    <!-- Source page 2 -->', markdown)
        soup = BeautifulSoup(render_body(markdown, self.root, set()), 'html.parser')
        parents = [li for li in soup.select('li') if li.get_text().startswith('Second parent')]
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0].select_one('ul li').get_text(strip=True), 'Child on the next page.')
        self.assertIsNone(soup.select_one('pre'))
        self.assertEqual(images, [])

    def test_ordinary_courier_letter_o_is_not_a_bullet(self):
        self.page.insert_text((72, 90), 'o is a variable.', fontname='cour')
        markdown, _ = self.convert()
        self.assertIn('o is a variable.', markdown)
        self.assertNotIn('- is a variable.', markdown)

    def test_page_preview_preserves_continued_list_indent(self):
        fragment = ('\n\n    <a id="source-page2"></a>\n\n'
                    '    > **Review page 2:** Review\n    >\n    > - Check this.\n\n'
                    '    - Child continued from the prior page.\n- Next parent.\n')
        html = render_body(fragment, self.root, set(), continuation_indent=4)
        soup = BeautifulSoup(html, 'html.parser')
        self.assertIsNone(soup.select_one('pre, blockquote'))
        self.assertEqual(soup.select_one('ul ul li').get_text(), 'Child continued from the prior page.')
        self.assertNotIn('Review', soup.get_text())
        self.assertIsNotNone(soup.select_one('a#source-page2'))


if __name__ == '__main__':
    unittest.main()
