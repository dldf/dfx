"""Focused layout, override, and SVG integration regressions using local fixtures."""
import argparse
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import pdfplumber
import pymupdf
from bs4 import BeautifulSoup

from dfx.convert import convert_pdf, main, page_mode
from dfx.page_classification import classify_page, page_signals
from dfx.review import render_body


def diagram(page, connected=True):
    for row in range(4):
        y = 40 + row * 180
        for col in range(3):
            x = 40 + col * 200
            page.draw_rect((x, y, x + 110, y + 65), fill=(.8, .9, 1))
            page.insert_text((x + 10, y + 25), f'Course {row}-{col}')
            page.insert_text((x + 10, y + 45), 'Three credits')
            if col < 2 and (connected or row == 0 and col == 0):
                page.draw_line((x + 110, y + 32), (x + 200, y + 32))


class PageClassificationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.doc = pymupdf.open()
        self.addCleanup(self.doc.close)

    def save(self):
        pdf = self.root / 'source.pdf'
        self.doc.save(pdf)
        return pdf

    def decision(self, pdf, number=0):
        with pymupdf.open(pdf) as doc, pdfplumber.open(pdf) as plumb:
            page = doc[number]
            return classify_page(page, page.get_text('dict')['blocks'], page.get_drawings(),
                                 page.get_image_info(), plumb.pages[number].find_tables())[0]

    def test_prose_and_real_table_keep_markdown(self):
        page = self.doc.new_page(width=600, height=800)
        page.insert_text((45, 50), '\n'.join([
            'This paragraph contains ordinary document text for readers to follow.',
            'Its aligned lines explain the report and preserve the reading order.',
            'The converter should retain this paragraph as editable Markdown text.']))
        page = self.doc.new_page(width=600, height=800)
        for x in (40, 210, 380, 550):
            page.draw_line((x, 60), (x, 660))
        for row in range(7):
            y = 60 + row * 100
            page.draw_line((40, y), (550, y))
            if row < 6:
                for col in range(3):
                    page.insert_text((50 + col * 170, y + 30), f'Value {row}-{col}')
        pdf = self.save()
        self.assertEqual([self.decision(pdf, i) for i in range(2)], ['markdown', 'markdown'])
        out = self.root / 'out'
        convert_pdf(pdf, out)
        text = (out / 'content.md').read_text()
        self.assertIn('ordinary document text', text)
        self.assertIn('Value 5-2', text)
        self.assertIn('|', text)
        self.assertEqual(list(out.glob('assets/*.svg')), [])

    def test_diagram_is_one_svg_and_preserves_page_positions(self):
        for label in ('Before diagram: ordinary text remains editable.', None,
                      'After diagram: ordinary text remains editable.'):
            page = self.doc.new_page(width=600, height=800)
            if label:
                page.insert_text((40, 60), label)
            else:
                diagram(page)
        pdf = self.save()
        self.assertEqual(self.decision(pdf, 1), 'svg')
        out = self.root / 'out'
        convert_pdf(pdf, out)
        text = (out / 'content.md').read_text()
        self.assertEqual(text.count('!['), 1)
        self.assertIn('![Source page 2](assets/page-2.svg)', text)
        self.assertNotIn('Course', text)
        self.assertLess(text.index('Before diagram'), text.index('assets/page-2.svg'))
        self.assertLess(text.index('assets/page-2.svg'), text.index('After diagram'))
        self.assertEqual(list(out.glob('assets/*.png')), [])
        svg = ET.parse(out / 'assets/page-2.svg')
        self.assertEqual(svg.findall('.//{http://www.w3.org/2000/svg}text'), [])
        soup = BeautifulSoup(render_body(text, out, set()), 'html.parser')
        self.assertEqual(len(soup.select('img')), 1)
        self.assertEqual(len(soup.select('a[id^="source-page"]')), 3)
        log = (out / 'review/log.md').read_text()
        self.assertIn('meaningful text description', log)
        self.assertIn('[SVG](../assets/page-2.svg)', log)

    def test_uncertain_keeps_existing_markdown_with_comparison_link(self):
        diagram(self.doc.new_page(width=600, height=800), connected=False)
        pdf = self.save()
        self.assertEqual(self.decision(pdf), 'uncertain')
        out, forced = self.root / 'out', self.root / 'forced'
        convert_pdf(pdf, out)
        convert_pdf(pdf, forced, page_modes={1: 'markdown'})
        text = (out / 'content.md').read_text()
        self.assertEqual(text, (forced / 'content.md').read_text())
        self.assertNotIn('.svg', text)
        self.assertTrue((out / 'assets/page-1.svg').exists())
        self.assertIn('[SVG](../assets/page-1.svg)', (out / 'review/log.md').read_text())

    def test_diagonal_connections_are_not_counted_as_enclosing_boxes(self):
        page = self.doc.new_page(width=600, height=800)
        diagram(page, connected=False)
        for row in range(4):
            for col in range(2):
                x, y = 40 + col * 200, 40 + row * 180
                page.draw_line((x + 110, y + 15), (x + 200, y + 45))
        pdf = self.save()
        with pymupdf.open(pdf) as doc, pdfplumber.open(pdf) as plumb:
            page = doc[0]
            signals = page_signals(page, page.get_text('dict')['blocks'], page.get_drawings(),
                                   page.get_image_info(), plumb.pages[0].find_tables())
        self.assertEqual(signals['shapes'], 12)
        self.assertEqual(signals['connections'], 9)  # Eight diagonal, one horizontal.
        self.assertEqual(self.decision(pdf), 'svg')
        out = self.root / 'out'
        convert_pdf(pdf, out)
        self.assertIn('![Source page 1](assets/page-1.svg)', (out / 'content.md').read_text())

    def test_unconnected_diagonal_rules_do_not_create_shapes_or_overlap(self):
        page = self.doc.new_page(width=600, height=800)
        for i in range(12):
            y = 30 + i * 55
            page.draw_line((40, y), (550, y + 40))
            page.insert_text((200, y + 25), f'Ordinary label {i}')
        signals = page_signals(page, page.get_text('dict')['blocks'], page.get_drawings(), [], [])
        self.assertEqual(signals['shapes'], 0)
        self.assertEqual(signals['connections'], 0)
        self.assertEqual(signals['overlap'], 0)
        self.assertEqual(self.decision(self.save()), 'markdown')

    def test_mixed_prose_and_diagram_keeps_markdown(self):
        page = self.doc.new_page(width=600, height=800)
        diagram(page)
        page.insert_text((40, 700), '\n'.join([
            'This explanatory paragraph supplies context for the diagram above.',
            'Its aligned lines describe the process in ordinary document prose.',
            'Keep this text editable even when the drawing is large and complex.']))
        self.assertEqual(self.decision(self.save()), 'uncertain')

    def test_table_evidence_prevents_confident_diagram_classification(self):
        page = self.doc.new_page(width=600, height=800)
        diagram(page)
        for x in (20, 220, 420, 580):
            page.draw_line((x, 20), (x, 740))
        for y in (20, 140, 260, 380, 500, 620, 740):
            page.draw_line((20, y), (580, y))
        self.assertEqual(self.decision(self.save()), 'uncertain')

    def test_unlabelled_vector_artwork_can_be_preserved(self):
        page = self.doc.new_page(width=600, height=800)
        for row in range(4):
            for col in range(3):
                page.draw_circle((70 + col * 220, 70 + row * 210), 35, fill=(.3, .5, .9))
        self.assertEqual(self.decision(self.save()), 'svg')

    def test_bitmap_is_scan_not_vector_artwork(self):
        page = self.doc.new_page(width=600, height=800)
        pix = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 60, 80), False)
        pix.clear_with(230)
        page.insert_image(page.rect, stream=pix.tobytes('png'))
        pdf = self.save()
        self.assertEqual(self.decision(pdf), 'scan')
        out = self.root / 'out'
        convert_pdf(pdf, out)
        self.assertFalse((out / 'assets/page-1.svg').exists())
        self.assertIn('no OCR stage', (out / 'review/log.md').read_text())
        self.assertIn('.png', (out / 'content.md').read_text())

    def test_overrides_and_invalid_pages(self):
        page = self.doc.new_page(width=600, height=800)
        diagram(page)
        pdf = self.save()
        forced = self.root / 'markdown'
        convert_pdf(pdf, forced, page_modes={1: 'markdown'})
        self.assertIn('Course', (forced / 'content.md').read_text())
        self.assertEqual(list(forced.glob('assets/*.svg')), [])
        # Forced SVG bypasses even table detection, not just its output.
        with patch('pdfplumber.page.Page.find_tables') as find_tables:
            convert_pdf(pdf, self.root / 'svg', page_modes={1: 'svg'})
            find_tables.assert_not_called()
        auto = self.root / 'auto'
        convert_pdf(pdf, auto, page_modes={1: 'auto'})
        self.assertIn('assets/page-1.svg', (auto / 'content.md').read_text())
        for modes in ({0: 'svg'}, {2: 'svg'}, {1: 'invalid'}):
            with self.assertRaises(ValueError):
                convert_pdf(pdf, self.root / 'invalid', page_modes=modes)
            self.assertFalse((self.root / 'invalid').exists())

    def test_cli_override_validation(self):
        self.assertEqual(page_mode('2=svg'), (2, 'svg'))
        for value in ('0=svg', '1=bad', '1', 'all=svg'):
            with self.assertRaises(argparse.ArgumentTypeError):
                page_mode(value)
        self.doc.new_page()
        pdf = self.save()
        for args in ([str(self.root), '--page-mode', '1=svg'],
                     [str(pdf), '--page-mode', '1=svg', '--page-mode', '1=markdown']):
            with patch.object(sys, 'argv', ['convert.py', *args]), patch('sys.stderr'), self.assertRaises(SystemExit) as result:
                main()
            self.assertEqual(result.exception.code, 2)

    def test_cli_can_force_ordinary_prose_to_svg(self):
        page = self.doc.new_page()
        page.insert_text((40, 60), 'An ordinary sentence that would normally remain editable text.')
        pdf = self.save()
        self.assertEqual(self.decision(pdf), 'markdown')
        args = ['convert.py', str(pdf), str(self.root / 'out'), '--page-mode', '1=svg']
        with patch.object(sys, 'argv', args), patch('dfx.convert.find_chrome', return_value='chrome'), \
                patch('dfx.convert.preview') as preview, patch('sys.stdout'):
            self.assertEqual(main(), 0)
        out = self.root / 'out/source'
        preview.assert_called_once_with(pdf, out / 'content.md', 'chrome')
        self.assertIn('assets/page-1.svg', (out / 'content.md').read_text())
        self.assertNotIn('An ordinary sentence', (out / 'content.md').read_text())


if __name__ == '__main__':
    unittest.main()
