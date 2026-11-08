"""LaTeX imports retain math, provenance, and the existing review package."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
import pymupdf

from dfx import convert
from dfx import review


class LatexImportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.tex = self.root / '1_downloads/latex/sample.tex'
        self.tex.parent.mkdir(parents=True)
        self.destination = self.root / '2_review/latex/sample'
        self.markdown = self.destination / 'content.md'

    def write_tex(self, body):
        self.tex.write_text('\\documentclass{article}\n\\begin{document}\n' + body + '\n\\end{document}')

    def import_tex(self):
        convert.convert_latex(self.tex, self.destination)
        return self.markdown.read_text()

    def test_math_and_list_structure_survive_import_and_render(self):
        self.write_tex(r'''\begin{enumerate}
\item The temperature is $T_\mathrm{c}$.
\begin{equation}T_\mathrm{c}=AM^{-\alpha}\end{equation}
\item Uranium U$^{238}$ and CuO$_2$.
\end{enumerate}''')
        markdown = self.import_tex()
        self.assertIn('$T_\\mathrm{c}$', markdown)
        self.assertNotIn('<!-- Source page', markdown)
        soup = BeautifulSoup(review.render_body(markdown, self.destination, set()), 'html.parser')
        self.assertNotIn('pdf2md-latex', str(soup))
        self.assertNotIn(str(self.tex), str(soup))
        self.assertEqual(len(soup.select('ol > li')), 2)
        self.assertEqual(len(soup.find_all('math')), 4)
        self.assertIsNotNone(soup.select_one('math[display="block"] msup'))
        self.assertIsNotNone(soup.select_one('math msub'))
        self.assertEqual(review.resolve_source(self.markdown, self.root), self.tex)

    def test_companion_pdf_has_separate_identity_and_may_have_multiple_pages(self):
        self.write_tex('One continuous document.')
        pdf = self.tex.with_suffix('.PDF')
        with pymupdf.open() as document:
            document.new_page()
            document.new_page()
            document.save(pdf)
        records = review.latex_sources(self.import_tex())
        self.assertEqual(Path(records['pdf']['path']), pdf)
        self.assertNotEqual(records['tex']['sha256'], records['pdf']['sha256'])
        self.assertIn('#page=2', (self.destination / 'review/log.md').read_text())
        pdf.write_bytes(b'changed comparison')
        with self.assertRaisesRegex(ValueError, 'PDF changed'):
            review.resolve_source(self.markdown, self.root)

    def test_source_change_stops_review_before_rendering(self):
        self.write_tex('Original.')
        self.import_tex()
        self.tex.write_text('Changed.')
        with patch.object(review, 'render_body') as render:
            with self.assertRaisesRegex(ValueError, 'TEX changed'):
                review.preview(self.tex, self.markdown, 'unused')
        render.assert_not_called()
        self.assertFalse((self.destination / 'index.html').exists())

    def test_local_image_is_copied_and_marked_for_description(self):
        with pymupdf.open() as document:
            document.new_page(width=40, height=40).get_pixmap().save(self.tex.parent / 'diagram.png')
        self.write_tex(r'\includegraphics{diagram.png}')
        markdown = self.import_tex()
        soup = BeautifulSoup(review.render_body(markdown, self.destination, set()), 'html.parser')
        self.assertIn('description: desc-001', markdown)
        self.assertTrue((self.destination / 'assets/figure-001.png').is_file())
        self.assertTrue(soup.img['src'].endswith('/assets/figure-001.png'))

    def test_unsupported_commands_are_visible_in_review_log(self):
        self.write_tex(r'Before. \unknowncommand{Important content} After.\newpage End.')
        self.import_tex()
        log = (self.destination / 'review/log.md').read_text()
        self.assertIn('unknowncommand', log)
        self.assertIn('Unrendered LaTeX', log)
        self.assertIn('headers/footers', log)

    def test_includes_and_missing_images_fail_without_partial_output(self):
        for body in (r'\input{chapter}', r'\includegraphics{missing.png}'):
            with self.subTest(body=body):
                self.write_tex(body)
                with self.assertRaises(ValueError):
                    self.import_tex()
                self.assertFalse(self.destination.exists())

    def test_existing_output_is_never_overwritten(self):
        self.write_tex('Original.')
        self.import_tex()
        self.markdown.write_text('Human edits.')
        with self.assertRaises(FileExistsError):
            self.import_tex()
        self.assertEqual(self.markdown.read_text(), 'Human edits.')

    def test_cli_routes_explicit_tex_and_rejects_pdf_page_overrides(self):
        self.write_tex('Example.')
        with patch.object(sys, 'argv', ['convert.py', str(self.tex)]), \
                patch.object(convert, 'find_chrome', return_value='chrome'), \
                patch.object(convert, 'convert_latex') as importer, \
                patch.object(convert, 'convert_pdf') as pdf_importer, \
                patch.object(convert, 'preview') as preview:
            self.assertEqual(convert.main(), 0)
        importer.assert_called_once()
        preview.assert_called_once()
        pdf_importer.assert_not_called()
        with patch.object(sys, 'argv', ['convert.py', str(self.tex), '--page-mode', '1=markdown']):
            with self.assertRaises(SystemExit) as error:
                convert.main()
            self.assertEqual(error.exception.code, 2)

    def test_invalid_record_is_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, 'Invalid LaTeX source record'):
            review.latex_sources('<!-- pdf2md-latex: ' + json.dumps([]) + ' -->')

    def test_unsupported_math_cannot_silently_become_plain_text(self):
        with self.assertRaisesRegex(ValueError, 'Could not convert TeX math'):
            review.render_body(r'$\unknownmathcommand{x}$', self.root, set())


if __name__ == '__main__':
    unittest.main()
