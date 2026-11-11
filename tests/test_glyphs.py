"""Damaged PDF character maps are repaired only with font or corroborated OCR evidence."""
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
import pdfplumber
import pymupdf

from dfx.convert import convert_pdf, extract_page_inputs, mark_highlights, format_spans
from dfx.glyphs import GlyphRepair, suspect_positions, word_changes
from dfx.review import render_body


class GlyphTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def source(self, lines=('Section Action Question',), *, embedded=False, bad='t', table=False, highlight=False, link=False):
        source = self.root / 'damaged.pdf'
        font = pymupdf.Font('helv')
        name = 'Embedded' if embedded else 'helv'
        with pymupdf.open() as document:
            page = document.new_page()
            xref = page.insert_font(fontname=name, **({'fontbuffer': font.buffer} if embedded else {}))
            if highlight:
                page.draw_rect((45, 56, 300, 74), color=None, fill=(1, 1, 0))
            for index, line in enumerate(lines):
                page.insert_text((50, 70 + index * 25), line, fontname=name, fontsize=12)
            if link:
                page.insert_link({'kind': pymupdf.LINK_URI, 'from': pymupdf.Rect(48, 54, 300, 74),
                                  'uri': 'https://example.org/report'})
            if table:
                for row, values in enumerate((('Category', 'Result'), ('Action', 'total'))):
                    for col, text in enumerate(values):
                        x, y = 50 + col * 150, 200 + row * 40
                        page.draw_rect((x, y, x + 150, y + 40))
                        page.insert_text((x + 5, y + 20), text, fontname=name)
            # Change only text extraction metadata; drawn letters stay intact.
            codes = list(range(32, 127)) + ([ord(bad)] if ord(bad) > 126 else [])
            width = 4 if embedded else 2
            entries = []
            for code in codes:
                cid = font.has_glyph(code, fallback=False) if embedded else code
                value = 58 if chr(code) == bad else code
                entries.append(f'<{cid:0{width}x}> <{value:04x}>')
            cmap = ('/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n'
                    '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
                    '/CMapName /Test def /CMapType 2 def\n'
                    f'1 begincodespacerange <{"0" * width}> <{"F" * width}> endcodespacerange\n'
                    f'{len(entries)} beginbfchar\n' + '\n'.join(entries) +
                    '\nendbfchar endcmap CMapName currentdict /CMap defineresource pop end end')
            ref = document.get_new_xref()
            document.update_object(ref, '<<>>')
            document.update_stream(ref, cmap.encode())
            document.xref_set_key(xref, 'ToUnicode', f'{ref} 0 R')
            document.save(source)
        return source

    def recognize(self, page, box, executable):
        return [(word if word == 'literal:colon' else word.replace(':', 't'), 96)
                for word in page.get_text('text', clip=pymupdf.Rect(box)).split()]

    def convert(self, source, recognize=None):
        destination = self.root / 'converted'
        with patch('dfx.glyphs.shutil.which', return_value='tesseract'), \
                patch('dfx.glyphs.ocr_line', side_effect=recognize or self.recognize):
            convert_pdf(source, destination)
        return (destination / 'content.md').read_text(), (destination / 'review/log.md').read_text()

    def test_corroborated_glyph_repairs_prose_tables_and_coverage_without_changing_pdf(self):
        source = self.source(lines=('Section Action Question', 'Note: literal:colon x:y https://example.org'), table=True)
        original = source.read_bytes()
        markdown, log = self.convert(source)
        self.assertIn('Section Action Question', markdown)
        self.assertIn('| Category | Result |', markdown)
        self.assertIn('| Action | total |', markdown)
        self.assertIn('Note: literal:colon x:y', markdown)
        self.assertIn('https://example.org', markdown)
        self.assertNotIn('targeted OCR agreement across distinct words', log)
        self.assertNotIn('Possible text loss', log)
        self.assertEqual(source.read_bytes(), original)

    def test_embedded_font_recovers_mapping_without_ocr(self):
        source = self.source(lines=('Section',), embedded=True)
        with pymupdf.open(source) as document, pdfplumber.open(source) as plumb, \
                patch('dfx.glyphs.shutil.which', return_value=None), patch('dfx.glyphs.ocr_line') as ocr:
            repair = GlyphRepair(document, plumb)
            inputs = extract_page_inputs(document[0], plumb.pages[0], glyphs=repair)
            self.assertEqual(inputs['blocks'][0]['lines'][0]['spans'][0]['text'], 'Section')
            self.assertIn('embedded font mapping', ' '.join(inputs['notes']))
            ocr.assert_not_called()

    def test_pdf_link_label_uses_the_same_verified_correction(self):
        markdown, log = self.convert(self.source(link=True))
        self.assertIn('[Section Action Question](https://example.org/report)', markdown)
        self.assertNotIn('Sec:ion', markdown)
        self.assertNotIn('A link was retained separately', log)

    def test_multiletter_glyph_keeps_highlighting_and_independent_source_chars(self):
        source = self.source(lines=('Af\ufb01nity of\ufb01ce Note:',), embedded=True, bad='\ufb01', highlight=True)
        with pymupdf.open(source) as document, pdfplumber.open(source) as plumb:
            before = plumb.pages[0].extract_text()
            repair = GlyphRepair(document, plumb)
            inputs = extract_page_inputs(document[0], plumb.pages[0], glyphs=repair)
            mark_highlights(document[0], inputs['blocks'], inputs['drawings'])
            spans = inputs['blocks'][0]['lines'][0]['spans']
            self.assertEqual(''.join(span['text'] for span in spans), 'Affinity office Note:')
            for span in spans:
                self.assertEqual(len(span['text']), len(span['highlighted']))
            rendered = BeautifulSoup(render_body(format_spans(spans), self.root, set()), 'html.parser')
            self.assertIn('Affinity office', rendered.mark.get_text())
            self.assertEqual(plumb.pages[0].extract_text(), before)
            self.assertNotEqual(inputs['plumb_page'].extract_text(), before)

    def test_one_word_repeated_is_not_independent_evidence(self):
        markdown, log = self.convert(self.source(lines=('Section', 'Section')))
        self.assertIn('Sec:ion', markdown)
        self.assertIn('Spelling: Suspect characters', log)
        self.assertNotIn('Corrected', log)

    def test_conflicting_or_low_confidence_ocr_never_changes_text(self):
        for words in ([('Section', 96), ('Ac:ion', 96), ('Question', 96)],
                      [('Section', 96), ('Action', 50), ('Question', 50)]):
            with self.subTest(words=words):
                source = self.source()
                with pymupdf.open(source) as document, pdfplumber.open(source) as plumb, \
                        patch('dfx.glyphs.shutil.which', return_value='tesseract'), \
                        patch('dfx.glyphs.ocr_line', return_value=words):
                    repair = GlyphRepair(document, plumb)
                    self.assertEqual(repair.corrections, {})
                    blocks = document[0].get_text('rawdict')['blocks']
                    _, notes = repair.apply(0, blocks, plumb.pages[0])
                    self.assertIn('remain unverified', ' '.join(notes))
                source.unlink()

    def test_missing_or_failed_ocr_retains_text_with_actionable_warning(self):
        source = self.source()
        for executable, failure, expected in ((None, None, 'Tesseract is unavailable'),
                                               ('tesseract', subprocess.TimeoutExpired('tesseract', 15), 'Targeted OCR failed')):
            with self.subTest(executable=executable), pymupdf.open(source) as document, pdfplumber.open(source) as plumb, \
                    patch('dfx.glyphs.shutil.which', return_value=executable), \
                    patch('dfx.glyphs.ocr_line', side_effect=failure) as ocr:
                repair = GlyphRepair(document, plumb)
                inputs = extract_page_inputs(document[0], plumb.pages[0], glyphs=repair)
                self.assertIn('Sec:ion', inputs['blocks'][0]['lines'][0]['spans'][0]['text'])
                self.assertIn(expected, ' '.join(inputs['notes']))
                if executable is None:
                    ocr.assert_not_called()

    def test_ocr_work_is_bounded_and_not_used_on_forced_svg_pages(self):
        source = self.source(lines=('Section', 'Action', 'Question'))
        with pymupdf.open(source) as document, pdfplumber.open(source) as plumb, \
                patch('dfx.glyphs.shutil.which', return_value='tesseract'), \
                patch('dfx.glyphs.MAX_OCR_LINES', 1), patch('dfx.glyphs.ocr_line', side_effect=self.recognize) as ocr:
            repair = GlyphRepair(document, plumb)
            self.assertEqual(ocr.call_count, 1)
            self.assertEqual(repair.corrections, {})
            self.assertIn('limit', repair.ocr_issue)
            ocr.reset_mock()
            self.assertEqual(GlyphRepair(document, plumb, {1: 'svg'}).corrections, {})
            ocr.assert_not_called()

    def test_candidate_detection_does_not_treat_ordinary_punctuation_as_a_replacement(self):
        self.assertEqual(suspect_positions('Note: 12:30 x:y https://host/path user@example.org'), set())
        self.assertEqual(word_changes('Sec:on', 'Section', {3}), {3: 'ti'})
        self.assertIsNone(word_changes('Sec:on', 'Selection', {3}))
        self.assertIsNone(word_changes('Sec:on', 'Sec1on', {3}))

    def test_mappings_are_not_shared_between_documents(self):
        source = self.source()
        with pymupdf.open(source) as document, pdfplumber.open(source) as plumb, \
                patch('dfx.glyphs.shutil.which', return_value='tesseract'), \
                patch('dfx.glyphs.ocr_line', side_effect=self.recognize):
            self.assertTrue(GlyphRepair(document, plumb).corrections)
        source.unlink()
        source = self.source(lines=('Section',))
        with pymupdf.open(source) as document, pdfplumber.open(source) as plumb, \
                patch('dfx.glyphs.shutil.which', return_value='tesseract'), \
                patch('dfx.glyphs.ocr_line', side_effect=self.recognize):
            self.assertEqual(GlyphRepair(document, plumb).corrections, {})


if __name__ == '__main__':
    unittest.main()
