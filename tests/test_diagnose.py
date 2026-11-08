"""Diagnostic reports use conversion evidence and never replace document edits."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pdfplumber
import pymupdf

from dfx.convert import convert_pdf, extract_page_inputs
from dfx.diagnose import diagnose_pdf, diagnostic_report, main, page_report
from dfx.page_classification import classify_page
from dfx.stage import verify_document
from test_page_classification import diagram
from dfx.workflow import diagnostic_only, document_present


class DiagnoseTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.pdf = self.root / '1_downloads/reports/sample.pdf'
        self.pdf.parent.mkdir(parents=True)
        self.destination = self.root / '2_review/reports/sample'
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((50, 60), 'A plain document for diagnostic tests.')
            doc.save(self.pdf)

    def test_diagnose_then_convert_and_stage(self):
        original = self.pdf.read_bytes()
        report = diagnose_pdf(self.pdf, self.destination)
        evidence = report.read_bytes()
        self.assertTrue(diagnostic_only(self.destination))
        self.assertFalse(document_present(self.destination))
        self.assertFalse((self.destination / 'content.md').exists())
        convert_pdf(self.pdf, self.destination)
        self.assertEqual(report.read_bytes(), evidence)
        self.assertFalse(diagnostic_only(self.destination))
        with self.assertRaises(FileExistsError):
            convert_pdf(self.pdf, self.destination)
        (self.destination / 'assets/main.css').write_text('body { color: black; }')
        (self.destination / 'index.html').write_text(
            '<html><head><link rel="stylesheet" href="assets/main.css"></head><body>Text</body></html>')
        ready = verify_document(self.destination, reviewer='Test Reviewer', root=self.root)
        self.assertFalse((self.destination / 'review').exists())
        self.assertFalse(list(ready.rglob('diagnostic.md')))
        self.assertTrue((ready / 'assets/_pdf2md/log.md').is_file())
        self.assertEqual(self.pdf.read_bytes(), original)

    def test_rerun_updates_only_diagnostic(self):
        convert_pdf(self.pdf, self.destination)
        (self.destination / 'index.html').write_text('Hand-edited HTML')
        (self.destination / 'review/notes.md').write_text('Handwritten review notes')
        before = {p: p.read_bytes() for p in self.destination.rglob('*') if p.is_file()}
        report = diagnose_pdf(self.pdf, self.destination)
        report.write_text('Old diagnostic')
        diagnose_pdf(self.pdf, self.destination)
        self.assertIn('Automatic classification: markdown', report.read_text())
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_conversion_rejects_extra_files_and_symlinks(self):
        for extra in ('content.md', 'index.html', 'review/notes.md', 'assets/custom.css', 'ready_to_publish.txt'):
            with self.subTest(extra=extra):
                destination = self.root / extra.replace('/', '-')
                diagnose_pdf(self.pdf, destination)
                path = destination / extra
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('Keep this')
                with self.assertRaises(FileExistsError):
                    convert_pdf(self.pdf, destination)
                self.assertEqual(path.read_text(), 'Keep this')
        target = self.root / 'target.md'
        target.write_text('Keep this too')
        report = diagnose_pdf(self.pdf, self.destination)
        report.unlink()
        report.symlink_to(target)
        with self.assertRaises(FileExistsError):
            convert_pdf(self.pdf, self.destination)
        with self.assertRaises(ValueError):
            diagnose_pdf(self.pdf, self.destination)
        self.assertEqual(target.read_text(), 'Keep this too')

    def test_cli_uses_conversion_paths_and_optional_output_root(self):
        for output in (None, self.root / 'custom'):
            arguments = ['diagnose.py', str(self.pdf), *([str(output)] if output else [])]
            with patch('dfx.workflow.ROOT', self.root), patch.object(sys, 'argv', arguments), \
                    patch('dfx.diagnose.diagnose_pdf') as diagnose, \
                    patch('dfx.diagnose.PENDING', str(self.root / '2_review')), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            expected = output / 'sample' if output else self.destination
            diagnose.assert_called_once_with(self.pdf, expected)

    def test_all_classification_paths_and_structure_are_reported(self):
        source = self.root / 'modes.pdf'
        with pymupdf.open() as doc:
            doc.new_page().insert_text((50, 60), 'Ordinary text')
            diagram(doc.new_page(width=600, height=800))
            diagram(doc.new_page(width=600, height=800), connected=False)
            page = doc.new_page(width=600, height=800)
            pix = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 60, 80), False)
            pix.clear_with(230)
            page.insert_image(page.rect, stream=pix.tobytes('png'))
            doc.save(source)
        report = diagnostic_report(source)
        with pymupdf.open(source) as doc, pdfplumber.open(source) as plumb:
            for i, expected in enumerate(('markdown', 'svg', 'uncertain', 'scan')):
                page = doc[i]
                inputs = extract_page_inputs(page, plumb.pages[i])
                self.assertEqual(classify_page(page, *[inputs[key] for key in
                                 ('blocks', 'drawings', 'images', 'tables')])[0], expected)
                section = report.split(f'## Page {i + 1}\n')[1].split('## Page ')[0]
                self.assertIn(f'Automatic classification: {expected}', section)
                self.assertIn('SVG: labelled diagram', section)
                self.assertIn('Uncertain layout: text alternatives', section)
        self.assertIn('Font:', report)
        self.assertIn('Example straight segment', report)
        self.assertIn('No nonempty text span was extracted', report)
        self.assertIn('Images: 1 placements', report)

    def test_table_filtering_and_failures_are_visible(self):
        source = self.root / 'layout.pdf'
        with pymupdf.open() as doc:
            page = doc.new_page(width=612, height=792)
            page.draw_rect(page.rect)
            page.draw_line((0, 100), (612, 100))
            for i, text in enumerate(('Heading', 'Body', '1. First', '2. Second', 'More', 'End')):
                page.insert_text((50, 140 + i * 30), text)
            doc.save(source)
        report = diagnostic_report(source)
        self.assertIn('1 detected; 0 used for classification', report)
        self.assertIn('Page-sized single-column layout container', report)
        for adjustment in ('rotate', 'crop'):
            adjusted = self.root / f'{adjustment}.pdf'
            with pymupdf.open(source) as doc:
                if adjustment == 'rotate':
                    doc[0].set_rotation(90)
                else:
                    doc[0].set_cropbox(pymupdf.Rect(10, 10, 600, 780))
                doc.save(adjusted)
            self.assertIn('Rotated or cropped page', diagnostic_report(adjusted))
        with patch('pdfplumber.page.Page.find_tables', side_effect=ValueError('bad table')):
            report = diagnostic_report(source)
        self.assertIn('Table extraction failed (ValueError)', report)
        self.assertIn('0 detected; 0 used for classification', report)

    def test_invalid_pdf_does_not_replace_existing_report(self):
        report = diagnose_pdf(self.pdf, self.destination)
        original = report.read_bytes()
        bad = self.root / 'bad.pdf'
        bad.write_text('Not a PDF')
        with self.assertRaises(Exception):
            diagnose_pdf(bad, self.destination)
        self.assertEqual(report.read_bytes(), original)

    def test_candidate_thresholds_are_strict_and_do_not_select_a_mode(self):
        with pymupdf.open(self.pdf) as doc, pdfplumber.open(self.pdf) as plumb:
            page = doc[0]
            for increment in (0, 1):
                with self.subTest(increment=increment):
                    inputs = extract_page_inputs(page, plumb.pages[0])
                    # Repeated text counts as raw blocks but deduplicates to one
                    # line. Small drawing bounds and image placements aren't diagrams.
                    inputs['blocks'] = inputs['blocks'] * (100 + increment)
                    inputs['drawings'] = [dict(rect=pymupdf.Rect(0, 0, 5, 5), items=
                        [('l', pymupdf.Point(0, 0), pymupdf.Point(5, 5))] * (10 + increment)
                        + [('c',)] * (10 + increment))]
                    inputs['images'] = [dict(bbox=(0, 0, 1, 1))] * (20 + increment)
                    report = page_report(page, inputs, 1)
                    self.assertIn('**Automatic classification: markdown**', report)
                    self.assertIn('Candidate SVG signals (observational only)', report)
                    self.assertIn('They do not affect mode selection', report)
                    for label, threshold in (('Text blocks', 100), ('Straight-line commands', 10),
                                             ('Curve commands', 10), ('Image placements', 20)):
                        self.assertIn(f'| {label} | {threshold + increment} | More than {threshold} | '
                                      f'{"Yes" if increment else "No"} |', report)


if __name__ == '__main__':
    unittest.main()
