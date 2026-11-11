"""Exercise discovery and exclusion rules without contacting a website."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from email.message import Message
import io
import json
from pathlib import Path
import sys
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dfx import site, site_audit


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
        self.inventory = self.root / 'data' / 'pdfs.json'
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
        self.inventory.parent.mkdir()
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
        self.assertFalse(self.inventory.parent.exists())
        self.assertEqual(site.load(), {'version': 1, 'documents': {}, 'scans': {}})
        ignored = self.scope + 'archive/secret.pdf'
        data = {'version': 1, 'documents': {ignored: {'old': True}}, 'scans': {}}
        output = io.StringIO()
        with redirect_stdout(output):
            site.scan(data, self.scope, 100)
        saved = json.loads(self.inventory.read_text())
        self.assertEqual(site.load(), saved)
        self.assertEqual(saved['documents'][ignored], {'old': True})
        self.assertEqual(saved['scans'][self.scope]['pdfs_found'], 4)
        self.assertIn('Found PDF:', output.getvalue())
        self.assertNotIn(ignored, self.requests)
        self.assertEqual(saved['documents'][self.scope + 'reports/one.pdf']['referring_pages'], [
            {'text': 'one', 'url': self.scope},
            {'text': 'duplicate', 'url': self.scope},
            {'text': 'alias', 'url': self.scope},
        ])

    def test_referring_pages_keep_distinct_pairs_and_preserve_rescan_metadata(self):
        pdf = self.scope + 'reports/one.pdf'
        page = self.scope + 'advising/'
        self.routes[self.scope] = (
            '<a href="reports/one.pdf"> Course <strong>Plan</strong> </a>'
            '<a href="reports/one.pdf#page=1">Course\nPlan</a>'
            '<a href="advising/">Advising</a>', 'text/html')
        self.routes[page] = (
            '<a href="../reports/one.pdf">Course Plan</a>'
            '<a href="../reports/one.pdf">Another title</a>'
            '<a href="../reports/one.pdf"><img alt="Not textContent"></a>'
            '<embed src="../reports/one.pdf">', 'text/html')
        prior = {'text': 'Earlier reference', 'url': self.scope + 'old/'}
        data = {'version': 1, 'documents': {pdf: {
            'discovered_at': 'original timestamp', 'sha256': 'original hash',
            'referring_pages': [prior],
        }}, 'scans': {}}
        for _ in range(2):
            with redirect_stdout(io.StringIO()):
                site.scan(data, self.scope, 100)
        saved = json.loads(self.inventory.read_text())['documents'][pdf]
        self.assertEqual(saved['discovered_at'], 'original timestamp')
        self.assertEqual(saved['sha256'], 'original hash')
        self.assertEqual(saved['referring_pages'], [prior,
            {'text': 'Course Plan', 'url': self.scope},
            {'text': 'Course Plan', 'url': page},
            {'text': 'Another title', 'url': page},
            {'text': '', 'url': page},
        ])

    def test_redirects_and_base_urls_keep_the_actual_referring_page(self):
        pdf = self.scope + 'reports/one.pdf'
        page = self.scope + 'advising/'
        self.routes[self.scope] = (
            '<a href="alias">Plan</a><a href="page-alias">Advising</a>', 'text/html')
        self.redirects[self.scope + 'page-alias'] = page
        self.routes[page] = (
            '<base href="../reports/">'
            '<a href="one.pdf">Direct link</a>'
            '<a href="../alias">Alias link</a>'
            '<a href="../second-alias">Second alias</a>', 'text/html')
        self.redirects[self.scope + 'second-alias'] = pdf
        data = {'version': 1, 'documents': {}, 'scans': {}}
        with redirect_stdout(io.StringIO()):
            site.scan(data, self.scope, 100)
        saved = json.loads(self.inventory.read_text())['documents']
        self.assertEqual(list(saved), [pdf])
        self.assertCountEqual(saved[pdf]['referring_pages'], [
            {'text': 'Plan', 'url': self.scope},
            {'text': 'Direct link', 'url': page},
            {'text': 'Alias link', 'url': page},
            {'text': 'Second alias', 'url': page},
        ])

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

    def test_inventory_migration_preserves_bytes_and_refuses_conflicts(self):
        self.inventory.parent.mkdir()
        legacy = self.inventory.with_name('inventory.json')
        contents = b'{"version":1,"scans":{},"documents":{}}\n'
        legacy.write_bytes(contents)
        self.assertEqual(site.load()['documents'], {})
        self.assertEqual(self.inventory.read_bytes(), contents)
        self.assertFalse(legacy.exists())
        legacy.write_text('different inventory')
        with self.assertRaisesRegex(ValueError, 'Both inventory.json and pdfs.json'):
            site.load()
        self.assertEqual(self.inventory.read_bytes(), contents)
        self.assertEqual(legacy.read_text(), 'different inventory')

    def test_limit_can_continue_without_revisiting_pages(self):
        with patch('builtins.input', side_effect=['1', 'invalid', '100']) as prompt, redirect_stdout(io.StringIO()) as output:
            summary = site.scan({}, self.scope, 1, folders=True, interactive=True)
        self.assertEqual(prompt.call_count, 3)
        self.assertFalse(summary['limited'])
        self.assertEqual(summary['max_urls'], 100)
        self.assertEqual(self.requests.count(self.scope), 1)
        self.assertIn('Discovery complete', output.getvalue())

    def test_limit_decline_saves_partial_scan(self):
        data = {'version': 1, 'documents': {}, 'scans': {}}
        with patch('builtins.input', return_value=''), redirect_stdout(io.StringIO()):
            site.scan(data, self.scope, 1, interactive=True)
        saved = site.load()['scans'][self.scope]
        self.assertTrue(saved['limited'])
        self.assertEqual(saved['visited'], 1)

    def test_redirect_already_in_queue_does_not_report_a_limit(self):
        self.routes[self.scope] = ('<a href="alias">a</a><a href="reports/one.pdf">b</a>', 'text/html')
        with patch('builtins.input') as prompt, redirect_stdout(io.StringIO()):
            summary = site.scan({}, self.scope, 3, folders=True, interactive=True)
        self.assertFalse(summary['limited'])
        prompt.assert_not_called()

    def test_audit_scores_unique_html_and_preserves_pdf_inventory(self):
        self.routes[self.scope] = (
            '<a href="about.html">about</a><a href="about-alias">alias</a>'
            '<a href="reports/one.pdf">PDF</a><a href="archive/secret.pdf">excluded</a>', 'text/html')
        self.routes[self.scope + 'about.html'] = ('<h1>About</h1>', 'text/html')
        self.redirects[self.scope + 'about-alias'] = self.scope + 'about.html'
        site.save({'version': 1, 'documents': {}, 'scans': {}})
        original = self.inventory.read_bytes()
        site.save_json(site_audit.score_path(), {'version': 1, 'scans': {'https://other.edu/': {'pages': {}}}})
        with patch.object(site_audit, 'lighthouse_command', return_value=['lighthouse']), patch.object(
                site_audit, 'page_score', side_effect=[{'score': 87}, ValueError('Browser failed')]) as scorer, redirect_stdout(io.StringIO()):
            site_audit.audit(self.scope, 9999)
        saved = site_audit.load_scores()['scans']
        self.assertIn('https://other.edu/', saved)
        run = saved[self.scope]
        self.assertEqual(len(run['pages']), 2)
        self.assertEqual(scorer.call_count, 2)
        self.assertEqual(run['pages'][self.scope]['score'], 87)
        self.assertIsNone(run['pages'][self.scope + 'about.html']['score'])
        self.assertEqual(run['pages'][self.scope + 'about.html']['error'], 'Browser failed')
        self.assertFalse(run['limited'])
        self.assertIsNotNone(run['finished_at'])
        self.assertEqual(self.inventory.read_bytes(), original)

    def test_audit_disregards_pdfignore_but_keeps_scope_and_robots_rules(self):
        direct = self.scope + 'archive/direct.html'
        redirected = self.scope + 'archive/redirected.html'
        blocked = self.scope + 'blocked.html'
        outside = 'https://other.edu/page.html'
        self.routes[self.scope] = (
            '<a href="archive/direct.html">Direct</a><a href="alias">Redirect</a>'
            '<a href="blocked.html">Robots blocked</a><a href="outside-alias">Outside</a>', 'text/html')
        self.routes[direct] = self.routes[redirected] = ('<h1>Page</h1>', 'text/html')
        self.routes['https://example.edu/robots.txt'] = (
            'User-agent: *\nDisallow: /department/blocked.html\n', 'text/plain')
        self.redirects[self.scope + 'alias'] = redirected
        self.redirects[self.scope + 'outside-alias'] = outside

        def score(command, url, client, chrome):
            self.assertTrue(client.allowed(url))
            self.assertFalse(client.allowed(blocked))
            self.assertFalse(client.allowed(outside))
            return {'score': 87}

        for rules in (self.scope + 'archive/', self.scope, 'invalid rule'):
            with self.subTest(pdfignore=rules):
                (self.root / '.pdfignore').write_text(rules)
                self.requests.clear()
                with patch.object(site_audit, 'lighthouse_command', return_value=['lighthouse']), patch.object(
                        site_audit, 'page_score', side_effect=score), redirect_stdout(io.StringIO()):
                    site_audit.audit(self.scope, 9999)
                run = site_audit.load_scores()['scans'][self.scope]
                self.assertEqual(set(run['pages']), {self.scope, direct, redirected})
                self.assertTrue(all(page['score'] == 87 for page in run['pages'].values()))
                self.assertEqual(len(run['errors']), 2)
                self.assertNotIn(blocked, self.requests)
                self.assertNotIn(outside, self.requests)

    def test_interrupted_audit_preserves_completed_scores(self):
        self.routes[self.scope] = ('<a href="about.html">About</a>', 'text/html')
        self.routes[self.scope + 'about.html'] = ('<h1>About</h1>', 'text/html')
        with patch.object(site_audit, 'lighthouse_command', return_value=['lighthouse']), patch.object(
                site_audit, 'page_score', side_effect=[{'score': 100}, KeyboardInterrupt]), redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                site_audit.audit(self.scope, 9999)
        run = site_audit.load_scores()['scans'][self.scope]
        self.assertEqual(run['pages'][self.scope]['score'], 100)
        self.assertIsNone(run['finished_at'])

    def test_scores_tree_handles_directories_queries_and_failures_offline(self):
        site.save_json(site_audit.score_path(), {'version': 1, 'scans': {self.scope: {
            'audited_at': 'saved timestamp', 'finished_at': 'saved timestamp', 'pages': {
                self.scope + 'about.html': {'score': 87},
                self.scope + 'about': {'score': 100},
                self.scope + 'about/team.html?lang=en': {'score': 92},
                self.scope + 'contact.html': {'score': None, 'error': 'Timed out'},
            }}}})
        with patch.object(site, 'build_opener') as network, redirect_stdout(io.StringIO()) as output:
            site_audit.scores(self.scope)
        text = output.getvalue()
        self.assertIn('├── (87) about.html', text)
        self.assertIn('│   └── (92) team.html?lang=en', text)
        self.assertIn('(100) about', text)
        self.assertIn('(error) contact.html', text)
        network.assert_not_called()

    def test_scores_below_filters_pages_and_preserves_parent_folders(self):
        site.save_json(site_audit.score_path(), {'version': 1, 'scans': {self.scope: {
            'audited_at': 'saved timestamp', 'finished_at': 'saved timestamp', 'pages': {
                self.scope + 'about/team.html?lang=en': {'score': 69},
                self.scope + 'zero.html': {'score': 0},
                self.scope + 'boundary.html': {'score': 70},
                self.scope + 'hidden/perfect.html': {'score': 100},
                self.scope + 'contact.html': {'score': None, 'error': 'Timed out'},
            }}}})
        original = site_audit.score_path().read_bytes()
        with patch.object(site, 'build_opener') as network, redirect_stdout(io.StringIO()) as output:
            site_audit.scores(self.scope, below=70)
        text = output.getvalue()
        self.assertIn('2 HTML pages below 70 (of 5 total)', text)
        self.assertIn('├── about/\n│   └── (69) team.html?lang=en', text)
        self.assertIn('└── (0) zero.html', text)
        for excluded in ('boundary.html', 'hidden/', 'perfect.html', 'contact.html', 'Timed out'):
            self.assertNotIn(excluded, text)
        network.assert_not_called()
        self.assertEqual(site_audit.score_path().read_bytes(), original)
        for threshold, count in ((0, 0), (100, 3)):
            with self.subTest(threshold=threshold), redirect_stdout(io.StringIO()) as output:
                site_audit.scores(self.scope, below=threshold)
                self.assertIn(f'{count} HTML pages below {threshold}', output.getvalue())
                self.assertNotIn('(100)', output.getvalue())
                if threshold == 0:
                    self.assertNotIn('──', output.getvalue())

    def test_scores_cli_below_validation_and_default(self):
        data = {'scans': {self.scope: {'scanned_at': 'saved timestamp'}}}
        for threshold in (None, 0, 70, 100):
            argv = ['site.py', 'scores']
            if threshold is not None:
                argv.extend(['--below', str(threshold)])
            with self.subTest(threshold=threshold), patch.object(site, 'load', return_value=data), patch.object(
                    site_audit, 'scores') as scores, patch.object(sys, 'argv', argv):
                site.main()
                scores.assert_called_once_with(self.scope, below=threshold)
        for threshold in ('-1', '101', '69.5', 'invalid'):
            with self.subTest(threshold=threshold), patch.object(site_audit, 'scores') as scores, patch.object(
                    sys, 'argv', ['site.py', 'scores', '--below', threshold]), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    site.main()
                self.assertNotEqual(stopped.exception.code, 0)
                scores.assert_not_called()

    def test_cli_defaults_and_latest_scope(self):
        latest = self.scope + 'latest/'
        data = {'version': 1, 'documents': {}, 'scans': {
            self.scope: {'scanned_at': '2026-01-01'}, latest: {'scanned_at': '2026-02-01'}}}
        with patch.object(site, 'load', return_value=data), patch.object(site_audit, 'audit') as audit, patch.object(
                sys, 'argv', ['site.py', 'audit']), patch.object(sys.stdin, 'isatty', return_value=False):
            site.main()
        audit.assert_called_once_with(latest, 9999, chrome=None, interactive=False)
        with patch.object(site, 'load', return_value=data), patch.object(site, 'scan') as scan, patch.object(
                site, 'tree'), patch.object(sys, 'argv', ['site.py', 'scan', self.scope, '--scan-only']), patch.object(
                sys.stdin, 'isatty', return_value=False):
            site.main()
        scan.assert_called_once_with(data, self.scope, 9999, interactive=False)

    def test_lighthouse_reports_missing_scores_and_redirects_as_errors(self):
        client = site.Client(self.scope)
        def runner(command, **kwargs):
            report = Path(next(arg.split('=', 1)[1] for arg in command if arg.startswith('--output-path=')))
            report.write_text(json.dumps(payload))
            self.assertIn('--only-categories=accessibility', command)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(site_audit.subprocess, 'run', side_effect=runner):
            payload = {'categories': {'accessibility': {'score': .87}}, 'lighthouseVersion': 'test'}
            self.assertEqual(site_audit.page_score(['lighthouse'], self.scope, client)['score'], 87)
            for bad in (None, True, -1, 2):
                payload['categories']['accessibility']['score'] = bad
                with self.assertRaisesRegex(ValueError, 'did not return an accessibility score'):
                    site_audit.page_score(['lighthouse'], self.scope, client)
            payload['categories']['accessibility']['score'] = 1
            payload['finalDisplayedUrl'] = 'https://outside.edu/'
            with self.assertRaisesRegex(ValueError, 'redirected outside'):
                site_audit.page_score(['lighthouse'], self.scope, client)


if __name__ == '__main__':
    unittest.main()
