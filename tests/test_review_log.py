"""Check that review warnings survive relocation out of the Markdown."""
from pathlib import Path
import json
import re
import shlex
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pymupdf
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from convert import convert_pdf, format_review_log, review_commands
from preview import render_body


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
        return destination, result

    def test_multiple_warnings_count_as_one_flagged_page(self):
        folder, result = self.convert(flagged=True)
        markdown = (folder / 'content.md').read_text()
        log = (folder / 'review/log.md').read_text()
        self.assertEqual(result, (2, 1))
        self.assertTrue(log.startswith('# source\n'))
        self.assertIn('## 2 pages, 1 flagged for review:', log)
        self.assertEqual(log.count('| **2** |'), 1)
        self.assertIn('[PDF](../../source.pdf#page=2)', log)
        self.assertIn('[MD](../content.md#source-page2)', log)
        for reason in ('Vector graphics preserved as an image', 'Little or no extractable text'):
            self.assertIn(reason, log)
            self.assertNotIn(reason, markdown)
        self.assertNotIn('**Review page', markdown)
        self.assertNotIn('Conversion draft', markdown)
        self.assertNotIn('**Review font', markdown)
        self.assertNotIn('description needs review', markdown)
        self.assertIn('Headings, reading order, and tables are inferred', log)
        self.assertIn('[preview.HTML](../preview.html)', log)
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
        for platform in ('mac', 'win'):
            commands = (folder / f'review/commands_{platform}.md').read_text()
            self.assertIn('## Generate Markdown ✏️', commands)
            self.assertIn('No unique source URL', commands)
            self.assertIn('custom output location', commands)
            self.assertNotIn('python verify.py \"', commands)
            self.assertNotIn('python site.py status', commands)
        self.assertNotIn('**Review page', (folder / 'content.md').read_text())

    def test_links_table_escaping_and_document_specific_commands(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / '1_downloads' / 'A report.pdf'
            markdown = root / '2_pending_review' / 'A report' / 'content.md'
            url = 'https://example.edu/reports/A%20report.pdf?download=1'
            (root / 'inventory.json').write_text(json.dumps({'documents': {
                url: {'local_path': '1_downloads/A report.pdf'}}}))
            log = format_review_log(pdf, markdown, 4, {4: ['Check A | B <label>.', 'Check A | B <label>.']})
            self.assertIn('[original.PDF](../../../1_downloads/A%20report.pdf)', log)
            self.assertIn('[content.MD](../content.md)', log)
            soup = BeautifulSoup(render_body(log, markdown.parent, set()), 'html.parser')
            warning_row = soup.select('table')[0].select('tbody tr')[0]
            self.assertEqual(len(warning_row.select('td')), 4)
            self.assertEqual(warning_row.select('td')[1].get_text(), 'Check A | B <label>.')
            self.assertEqual(len(soup.select('table')), 1)
            with patch('convert.__file__', str(root / 'convert.py')):
                guides = {platform: review_commands(pdf, markdown, root / '1_downloads', root / '2_pending_review', platform)
                          for platform in ('mac', 'win')}
            expected_headings = ['Generate Markdown ✏️', 'Rebuild Preview 🔍', 'Stage for Publication 🚀', 'Workflow',
                                 'Optional: discover PDFs on a website',
                                 'Tracking multiple conversions in a project', 'Command Usage Instructions 📓',
                                 'Environment Setup (a one-time process) ⚙️']
            for platform, guide in guides.items():
                self.assertEqual(re.findall(r'^## (.+)$', guide, re.M), expected_headings)
                soup = BeautifulSoup(render_body(guide, markdown.parent, set()), 'html.parser')
                tables = soup.select('table')
                self.assertEqual([len(table.select('tbody tr')) for table in tables], [9, 3])
                self.assertTrue(all(len(row.select('td')) == 3 for row in soup.select('tbody tr')))
                commands = [line for code in soup.select('code') for line in code.get_text().splitlines()]
                self.assertIn('--max-urls 100', guide)
                self.assertIn('250 URLs by default', guide)
                self.assertIn('This step is unnecessary if you already downloaded', guide)
                self.assertIn('python site.py scan "https://example.edu/reports/"', commands)
                self.assertNotIn('python site.py status', guide)
                self.assertTrue(any(command.startswith('python verify.py ') and command.endswith('content.md"') for command in commands))
            self.assertIn('python preview.py "1_downloads/A report.pdf" "2_pending_review/A report/content.md" --inspect', guides['mac'])
            self.assertIn('python preview.py "1_downloads\\A report.pdf" "2_pending_review\\A report\\content.md" --inspect', guides['win'])
            self.assertIn('source .venv/bin/activate', guides['mac'])
            self.assertIn('.\\.venv\\Scripts\\Activate.ps1', guides['win'])
            self.assertIn('.venv\\Scripts\\activate.bat', guides['win'])

    def test_batch_paths_and_mac_shell_quoting(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            pdf = root / '1_downloads' / 'nested' / 'A $report `draft`.pdf'
            markdown = root / '2_pending_review' / 'nested' / pdf.stem / 'content.md'
            with patch('convert.__file__', str(root / 'convert.py')):
                guide = review_commands(pdf, markdown, root / '1_downloads', root / '2_pending_review')
            self.assertIn('python convert.py "1_downloads" "2_pending_review"', guide)
            command = next(line for line in guide.splitlines() if line.startswith('python preview.py '))
            # Double-quoted metacharacters are escaped rather than expanded.
            self.assertIn('\\$report', command)
            self.assertIn('\\`draft\\`', command)
            self.assertEqual(shlex.split(command)[1], 'preview.py')


if __name__ == '__main__':
    unittest.main()
