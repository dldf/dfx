"""Exercise discovery and exclusion rules without contacting a website."""
from contextlib import redirect_stdout
from copy import deepcopy
from email.message import Message
import importlib.util
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('discovery_site', Path(__file__).resolve().parents[1] / 'site.py')
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


class Response(io.BytesIO):
    def __init__(self, url, body, mime):
        super().__init__(body.encode())
        self.url = url
        self.headers = Message()
        self.headers['Content-Type'] = mime
        self.headers['Content-Length'] = str(len(body.encode()))


class DiscoveryTests(unittest.TestCase):
    scope = 'https://example.edu/department/'

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inventory = self.root / 'inventory.json'
        self.requests = []
        self.routes = {
            'https://example.edu/robots.txt': ('User-agent: *\nAllow: /', 'text/plain'),
            self.scope: ('<a href="reports/one.pdf">one</a><a href="reports/one.pdf#page=2">duplicate</a>'
                         '<a href="reports/two.pdf">two</a><a href="reports/2025/new.pdf">nested</a>'
                         '<a href="archive/secret.pdf">excluded</a><a href="archive-old/keep.pdf">keep</a>'
                         '<a href="alias">alias</a>', 'text/html'),
        }
        for path in ('reports/one.pdf', 'reports/two.pdf', 'reports/2025/new.pdf', 'archive/secret.pdf', 'archive-old/keep.pdf'):
            self.routes[self.scope + path] = ('%PDF-example', 'application/pdf')
        self.redirects = {self.scope + 'alias': self.scope + 'reports/one.pdf'}
        (self.root / '.pdfignore').write_text('# Old content\n\n' + self.scope + 'archive/\n')
        for patcher in (patch.object(site, 'ROOT', self.root),
                        patch.object(site, 'INVENTORY', self.inventory),
                        patch.object(site.time, 'sleep'),
                        patch.object(site, 'build_opener', side_effect=self.opener)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def opener(self, redirects):
        test = self

        class Opener:
            def open(self, request, timeout):
                url = request.full_url
                test.requests.append(url)
                if url in test.redirects:
                    redirected = redirects.redirect_request(request, None, 302, 'Found', {}, test.redirects[url])
                    return self.open(redirected, timeout)
                body, mime = test.routes[url]
                return Response(url, body, mime)

        return Opener()

    def test_rules_match_folders_without_matching_similar_names(self):
        rules = site.load_exclusions()
        for suffix in ('archive', 'archive/', 'archive/a.pdf?version=1', '%61rchive/nested/a.pdf'):
            self.assertTrue(site.excluded(self.scope + suffix, rules))
        for url in (self.scope + 'archive-old/a.pdf', 'https://other.edu/department/archive/a.pdf'):
            self.assertFalse(site.excluded(url, rules))
        (self.root / '.pdfignore').write_text(self.scope + 'archive')
        self.assertEqual(site.load_exclusions(), rules)
        (self.root / '.pdfignore').unlink()
        self.assertEqual(site.load_exclusions(), ())

    def test_invalid_rules_report_the_line(self):
        for rule in ('archive/', self.scope + '*', self.scope + '?query=1', self.scope + '#fragment'):
            with self.subTest(rule=rule):
                (self.root / '.pdfignore').write_text('# comment\n' + rule)
                with self.assertRaisesRegex(ValueError, r'\.pdfignore:2:'):
                    site.load_exclusions()

    def test_summary_counts_unique_final_urls_without_mutating_inventory(self):
        data = {'version': 1, 'documents': {'old': {}}, 'scans': {}}
        original = deepcopy(data)
        self.inventory.write_text('existing inventory stays byte-for-byte intact')
        output = io.StringIO()
        with redirect_stdout(output), patch.object(site, 'save') as save:
            site.scan(data, self.scope, 100, folders=True)
        self.assertEqual(data, original)
        self.assertEqual(self.inventory.read_text(), 'existing inventory stays byte-for-byte intact')
        save.assert_not_called()
        rows = [line.split() for line in output.getvalue().splitlines() if line.startswith('https:')]
        self.assertEqual(dict(rows), {self.scope + 'archive-old/': '1', self.scope + 'reports/': '2',
                                     self.scope + 'reports/2025/': '1'})
        self.assertIn('Found 4 unique PDFs', output.getvalue())
        self.assertNotIn('Found PDF:', output.getvalue())
        self.assertNotIn('one.pdf', output.getvalue())
        self.assertNotIn(self.scope + 'archive/secret.pdf', self.requests)

    def test_summary_cli_does_not_load_inventory_print_tree_or_download(self):
        with patch.object(sys, 'argv', ['site.py', 'scan', self.scope, '--folders']), patch.object(site, 'load') as load, patch.object(site, 'tree') as tree, patch.object(site, 'download') as download, redirect_stdout(io.StringIO()):
            site.main()
        load.assert_not_called()
        tree.assert_not_called()
        download.assert_not_called()
        self.assertFalse(self.inventory.exists())

    def test_normal_scan_still_saves_and_preserves_excluded_records(self):
        ignored = self.scope + 'archive/secret.pdf'
        data = {'version': 1, 'documents': {ignored: {'old': True}}, 'scans': {}}
        output = io.StringIO()
        with redirect_stdout(output):
            site.scan(data, self.scope, 100)
        saved = json.loads(self.inventory.read_text())
        self.assertEqual(saved['documents'][ignored], {'old': True})
        self.assertEqual(saved['scans'][self.scope]['pdfs_found'], 4)
        self.assertIn('Found PDF:', output.getvalue())
        self.assertNotIn(ignored, self.requests)

    def test_redirect_to_excluded_folder_is_rejected_before_request(self):
        target = self.scope + 'archive/secret.pdf'
        self.redirects[self.scope + 'alias'] = target
        output = io.StringIO()
        with redirect_stdout(output):
            site.scan({}, self.scope, 100, folders=True)
        self.assertNotIn(target, self.requests)
        self.assertIn('1 skipped URLs', output.getvalue())
        self.assertIn('Counts may be incomplete', output.getvalue())

    def test_limit_and_excluded_root(self):
        output = io.StringIO()
        with redirect_stdout(output):
            site.scan({}, self.scope, 1, folders=True)
        self.assertIn('Scan limit reached', output.getvalue())
        self.assertIn('Found 0 unique PDFs', output.getvalue())
        self.assertIn('Counts may be incomplete', output.getvalue())
        self.requests.clear()
        (self.root / '.pdfignore').write_text(self.scope)
        with self.assertRaisesRegex(ValueError, 'scan root is excluded'):
            site.scan({}, self.scope, 100, folders=True)
        self.assertEqual(self.requests, [])

    def test_download_skips_previously_indexed_excluded_urls(self):
        data = {'documents': {self.scope + 'archive/secret.pdf': {'size_bytes': 1}}}
        before = deepcopy(data)
        with patch('builtins.input') as prompt, redirect_stdout(io.StringIO()) as output:
            site.download(data, self.scope)
        self.assertIn('0 PDFs to download', output.getvalue())
        prompt.assert_not_called()
        self.assertEqual(self.requests, [])
        self.assertEqual(data, before)


if __name__ == '__main__':
    unittest.main()
