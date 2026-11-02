"""Bold body headings preserve paragraph text, lists, and inline formatting."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from bs4 import BeautifulSoup
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from convert import convert_pdf
from review import render_body


class InlineHeadingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.doc = pymupdf.open()
        self.addCleanup(self.doc.close)
        self.page = self.doc.new_page()

    def line(self, y, *parts, x=50, size=10):
        for text, font in parts:
            self.page.insert_text((x, y), text, fontname=font, fontsize=size)
            x += pymupdf.get_text_length(text, fontname=font, fontsize=size)

    def convert(self):
        source, destination = self.root / 'source.pdf', self.root / 'out'
        self.doc.save(source)
        convert_pdf(source, destination)
        markdown = (destination / 'content.md').read_text()
        return markdown, BeautifulSoup(render_body(markdown, destination, set()), 'html.parser')

    def test_standalone_and_short_prefix_rules_preserve_other_emphasis(self):
        self.line(60, ('Ordinary body text establishes the paragraph size.', 'helv'))
        standalone = 'This standalone bold heading has more than ten words in one line'
        self.line(110, (standalone, 'hebo'))
        ten = 'one two three four five six seven eight nine ten'
        self.line(160, (ten, 'hebo'), (' is followed by body text.', 'helv'))
        self.line(210, (ten + ' eleven', 'hebo'), (' stays emphasis.', 'helv'))
        self.line(260, ('Ordinary text with ', 'helv'), ('middle emphasis', 'hebo'), (' remains a paragraph.', 'helv'))
        self.line(310, ('Multiline bold', 'hebo'))
        self.line(322, ('paragraph stays bold.', 'hebo'))
        self.line(370, ('Eligibility:', 'hebo'), (' Students may apply.', 'helv'))
        markdown, soup = self.convert()
        self.assertEqual([' '.join(h.get_text().split()) for h in soup.select('h4')],
                         [standalone, ten, 'Eligibility:'])
        self.assertIn('middle emphasis', [s.get_text() for s in soup.select('p strong')])
        self.assertIn(ten + ' eleven', [' '.join(s.get_text().split()) for s in soup.select('p strong')])
        self.assertIsNone(soup.select_one('p h4, strong h4'))
        self.assertIn('Students may apply.', soup.get_text())
        self.assertIn('pdf2md-inline-heading:', markdown)
        self.assertNotIn('pdf2md-inline-heading:', str(soup))

    def test_large_headings_keep_priority_and_lists_remain_lists(self):
        self.line(50, ('Document title', 'hebo'), size=20)
        self.line(90, ('Section heading', 'hebo'), size=16)
        self.line(130, ('Ordinary body text that keeps the body font size at ten points.', 'helv'))
        self.line(170, ('1. ', 'helv'), ('Requirements:', 'hebo'), (' Complete the form.', 'helv'))
        self.line(200, ('2. Return the form to your advisor.', 'helv'))
        _, soup = self.convert()
        self.assertEqual(soup.h1.get_text(), 'Document title')
        self.assertEqual(soup.h2.get_text(), 'Section heading')
        self.assertEqual(len(soup.select('ol > li')), 2)
        self.assertEqual(soup.select_one('ol h4').get_text(strip=True), 'Requirements:')
        self.assertIn('Complete the form.', soup.select_one('ol li').get_text())

    def test_heading_can_wrap_before_the_regular_paragraph_text(self):
        self.line(60, ('Important eligibility', 'hebo'))
        self.line(72, ('requirements:', 'hebo'), (' Students must complete the course.', 'helv'))
        _, soup = self.convert()
        self.assertEqual(soup.h4.get_text(' ', strip=True), 'Important eligibility requirements:')
        self.assertIn('Students must complete the course.', soup.get_text())

    def test_nested_formatting_and_links_are_split_without_invalid_headings(self):
        markdown = ('<mark>[***Read this*** and keep this link](https://example.edu/)</mark>'
                    ' afterwards. <!-- pdf2md-inline-heading: 1 -->')
        soup = BeautifulSoup(render_body(markdown, self.root, set()), 'html.parser')
        self.assertEqual(' '.join(soup.h4.get_text().split()), 'Read this')
        self.assertIsNotNone(soup.select_one('h4 mark a strong em'))
        self.assertEqual([a['href'] for a in soup.select('a')], ['https://example.edu/'] * 2)
        self.assertIn('and keep this link', soup.select('a')[1].get_text())
        self.assertIsNone(soup.select_one('p h4, mark h4, a h4'))

    def test_unmarked_existing_markdown_retains_bold(self):
        soup = BeautifulSoup(render_body('**Existing bold** stays as authored.', self.root, set()), 'html.parser')
        self.assertIsNone(soup.h4)
        self.assertEqual(soup.strong.get_text(), 'Existing bold')


if __name__ == '__main__':
    unittest.main()
