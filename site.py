#!/usr/bin/env python3
"""Scan a URL subtree, confirm PDF downloads, and show folder-based review status."""
import argparse
from collections import deque
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
from pathlib import Path
import posixpath
import re
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from workflow import DOWNLOADS, PENDING, READY, document_present

ROOT = Path(__file__).resolve().parent
INVENTORY = ROOT / 'inventory.json'
AGENT = 'pdf2md/1.0'
STAGES = {'1': 'Downloaded', '2': 'Pending Review', '3': 'Ready to Publish'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def canonical(url):
    parts = urlsplit(url)
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Use an http(s) URL without embedded credentials.')
    path = posixpath.normpath('/' + unquote(parts.path).lstrip('/'))
    if parts.path.endswith('/') and path != '/':
        path += '/'
    path = quote(path, safe="/:@-._~!$&'()*+,;=")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ''))


def scope_url(url):
    parts = urlsplit(canonical(url))
    if parts.query:
        raise ValueError('The scan root must be a URL folder, without a query string.')
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/') + '/', '', ''))


def within(url, scope):
    candidate, root = urlsplit(canonical(url)), urlsplit(scope)
    return ((candidate.scheme, candidate.netloc) == (root.scheme, root.netloc)
            and (candidate.path.startswith(root.path) or candidate.path == root.path.rstrip('/')))


def load():
    data = json.loads(INVENTORY.read_text())
    if data.get('version') != 1 or not isinstance(data.get('documents'), dict) or not isinstance(data.get('scans'), dict):
        raise ValueError('Expected inventory version 1 with documents and scans objects.')
    return data


def save(data):
    # Adapted from the archived catalog approach; replace atomically.
    with tempfile.NamedTemporaryFile(mode='w', dir=INVENTORY.parent, delete=False, encoding='utf-8') as stream:
        temporary = Path(stream.name)
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write('\n')
    try:
        temporary.replace(INVENTORY)
    finally:
        temporary.unlink(missing_ok=True)


class Client:
    def __init__(self, scope):
        self.scope = scope
        self.last_request = 0
        self.delay = .2
        origin = urlsplit(scope)
        self.robots_url = urlunsplit((origin.scheme, origin.netloc, '/robots.txt', '', ''))
        self.robots = RobotFileParser()
        client = self

        class Redirects(HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, newurl):
                target = canonical(newurl)
                if not client.allowed(target):
                    raise ValueError(f'Redirect outside scope or disallowed by robots.txt: {target}')
                return super().redirect_request(request, fp, code, msg, headers, target)

        self.opener = build_opener(Redirects())
        try:
            with self.open(self.robots_url) as response:
                body = response.read(1_000_001)
                if len(body) > 1_000_000:
                    raise ValueError('robots.txt too large to read safely; scan stopped.')
                self.robots.parse(body.decode('utf-8', errors='replace').splitlines())
        except HTTPError as exc:
            if exc.code in (404, 410):
                self.robots.parse([])
            elif exc.code in (401, 403):
                self.robots.parse(['User-agent: *', 'Disallow: /'])
            else:
                raise ValueError(f'Cannot check robots.txt (HTTP {exc.code}); scan stopped.') from exc
        delay = self.robots.crawl_delay(AGENT)
        if delay is not None:
            self.delay = max(self.delay, delay)

    def allowed(self, url):
        return url == self.robots_url or (within(url, self.scope) and self.robots.can_fetch(AGENT, url))

    def open(self, url, method='GET'):
        if not self.allowed(url):
            raise ValueError(f'Outside scope or disallowed by robots.txt: {url}')
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        return self.opener.open(Request(url, method=method, headers={
            'User-Agent': AGENT, 'Accept-Encoding': 'identity'}), timeout=20)


def scan(data, scope, limit):
    client = Client(scope)
    queue, queued, seen = deque([scope]), {scope}, set()
    errors, outside, found = [], set(), set()
    stamp = now()
    while queue and len(seen) < limit:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        try:
            is_pdf_url = urlsplit(url).path.lower().endswith('.pdf')
            try:
                response = client.open(url, 'HEAD' if is_pdf_url else 'GET')
            except HTTPError as exc:
                if not is_pdf_url or exc.code not in (405, 501):
                    raise
                response = client.open(url)
            with response:
                final = canonical(response.url)
                seen.add(final)
                mime = response.headers.get_content_type()
                pdf = (mime == 'application/pdf' or urlsplit(final).path.lower().endswith('.pdf')
                       or '.pdf' in response.headers.get('Content-Disposition', '').lower())
                if pdf:
                    entry = data['documents'].setdefault(final, {'discovered_at': stamp})
                    length = response.headers.get('Content-Length', '')
                    entry.update({'last_seen': stamp, 'size_bytes': int(length) if length.isdigit() else None})
                    found.add(final)
                    print(f'Found PDF: {final}', flush=True)
                    continue
                if mime not in ('text/html', 'application/xhtml+xml'):
                    continue
                body = response.read(5_000_001)
                if len(body) > 5_000_000:
                    raise ValueError('HTML exceeds 5 MB; page skipped')
                soup = BeautifulSoup(body, 'html.parser')
                base = urljoin(final, soup.base.get('href', '')) if soup.base else final
                for link in soup.select('a[href], iframe[src], embed[src], object[data]'):
                    href = link.get('href') or link.get('src') or link.get('data')
                    try:
                        target = canonical(urljoin(base, href))
                    except ValueError:
                        continue
                    if not within(target, scope):
                        if urlsplit(target).path.lower().endswith('.pdf'):
                            outside.add(target)
                        continue
                    if target not in queued and target not in seen:
                        queue.append(target)
                        queued.add(target)
        except (OSError, ValueError, HTTPException) as exc:
            errors.append({'url': url, 'error': str(exc)})
            print(f'Skipped {url}: {exc}')
    data['scans'][scope] = {'scanned_at': stamp, 'visited': len(seen), 'pdfs_found': len(found),
                           'limited': bool(queue), 'errors': errors, 'outside_pdfs': sorted(outside)}
    save(data)
    print(f'Found {len(found)} unique PDFs; {len(errors)} skipped URLs; {len(outside)} PDF links outside scope.')
    if queue:
        print(f'Scan limit reached ({limit} URLs). Increase --max-urls to continue discovery.')
    print('Scope: linked content only; unlinked files and JavaScript-generated links may be missed.')


def documents(data, scope):
    return [(url, entry) for url, entry in sorted(data['documents'].items()) if within(url, scope)]


def tree(data, scope):
    entries = documents(data, scope)
    print(f'{scope}    {len(entries)} PDFs')
    nodes = {}
    for url, entry in entries:
        parts = urlsplit(url)
        relative = parts.path[len(urlsplit(scope).path):] or 'index'
        segments = [unquote(s) for s in relative.split('/') if s]
        label = segments[-1] + ('?' + parts.query if parts.query else '')
        branch = nodes
        for segment in segments[:-1]:
            branch = branch.setdefault(segment + '/', {})
        stage = document_stage(url)
        branch[f'[{stage}] {label}' if stage else f'{label} — not downloaded'] = None

    def display(branch, prefix=''):
        items = sorted(branch.items())
        for index, (label, children) in enumerate(items):
            last = index == len(items) - 1
            print(prefix + ('└── ' if last else '├── ') + label)
            if children is not None:
                display(children, prefix + ('    ' if last else '│   '))
    display(nodes)
    for stage, meaning in STAGES.items():
        print(f'[{stage}] {meaning}')


def local_path(url):
    # Reuse the archived catalog's confinement checks; never overwrite inputs.
    parts = urlsplit(url)
    components = [re.sub(r'[^\w.\-]', '_', unquote(p)) or '_' for p in parts.path.split('/') if p]
    if not components:
        components = ['document.pdf']
    if not components[-1].lower().endswith('.pdf'):
        components[-1] += '.pdf'
    if parts.query:
        name = Path(components[-1])
        components[-1] = name.stem + '-' + hashlib.sha256(parts.query.encode()).hexdigest()[:10] + '.pdf'
    host = re.sub(r'[^\w.\-]', '_', parts.netloc)
    destination = (ROOT / DOWNLOADS / host).joinpath(*components).resolve()
    boundary = (ROOT / DOWNLOADS).resolve()
    if not boundary.is_relative_to(ROOT.resolve()) or not destination.is_relative_to(boundary):
        raise ValueError(f'Download path escapes {DOWNLOADS}/.')
    return destination


def document_stage(url):
    pdf = local_path(url)
    relative = pdf.relative_to((ROOT / DOWNLOADS).resolve()).with_suffix('')
    for stage, name in (('3', READY), ('2', PENDING)):
        boundary = (ROOT / name).resolve()
        folder = (boundary / relative).resolve()
        if not boundary.is_relative_to(ROOT.resolve()) or not folder.is_relative_to(boundary):
            raise ValueError(f'Document path escapes {name}/.')
        if document_present(folder):
            return stage
    return '1' if pdf.is_file() else None


def download(data, scope):
    pending = [(url, entry) for url, entry in documents(data, scope)
               if document_stage(url) is None]
    known = sum(entry['size_bytes'] for _, entry in pending if entry.get('size_bytes') is not None)
    unknown = sum(entry.get('size_bytes') is None for _, entry in pending)
    print(f'{len(pending)} PDFs to download; reported size {known / 1_000_000:.2f} MB'
          + (f' plus {unknown} with unknown size.' if unknown else '.'))
    if not pending:
        return
    print('Documents present in any workflow folder are skipped. Reported sizes may change.')
    if input(f'Download these PDFs into {DOWNLOADS}/? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Cancelled. No PDFs downloaded; scan results remain in inventory.json.')
        return
    client = Client(scope)
    for url, entry in pending:
        destination = local_path(url)
        if destination.exists():
            print(f'Skipped existing file: {destination}')
            continue
        temporary = None
        try:
            with client.open(url) as response:
                first = response.read(1024)
                if b'%PDF-' not in first:
                    raise ValueError('Response does not contain a PDF header')
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    chunk = first
                    while chunk:
                        stream.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        chunk = response.read(65536)
                expected = response.headers.get('Content-Length', '')
                if expected.isdigit() and size != int(expected):
                    raise ValueError('Incomplete download; original file was not saved')
                # Publish the complete file atomically, without replacing an input.
                destination.hardlink_to(temporary)
            entry.update({'local_path': str(destination.relative_to(ROOT)), 'sha256': digest.hexdigest(),
                          'downloaded_at': now(), 'size_bytes': size})
            save(data)
            print(f'Downloaded: {entry["local_path"]}')
        except (OSError, ValueError, HTTPException) as exc:
            print(f'Download failed for {url}: {exc}')
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog='Tree status comes from files in the three workflow folders. Use verify.py to prepare a reviewed document for publication.')
    sub = parser.add_subparsers(dest='command', required=True)
    scanner = sub.add_parser('scan', help='Discover PDFs, show tree and ask before downloading')
    scanner.add_argument('url', help='URL folder, e.g. https://example.edu/department/')
    scanner.add_argument('--max-urls', '--max-pages', dest='max_urls', type=int, default=250, help='Maximum visited URLs (default 250)')
    scanner.add_argument('--scan-only', action='store_true', help='Save inventory without asking to download')
    for name in ('tree', 'download'):
        command = sub.add_parser(name, help='Display status tree' if name == 'tree' else 'Confirm downloads from a previous scan')
        command.add_argument('url', nargs='?', help='Scope URL; defaults to most recent scan')
    args = parser.parse_args()
    try:
        data = load()
        latest = max(data['scans'], key=lambda s: data['scans'][s]['scanned_at'], default=None)
        if not args.url and not latest:
            raise ValueError('No scan recorded. Run site.py scan URL first.')
        scope = scope_url(args.url or latest)
        if args.command == 'scan':
            if args.max_urls < 1:
                raise ValueError('--max-urls must be positive.')
            scan(data, scope, args.max_urls)
        if args.command in ('tree', 'scan'):
            tree(data, scope)
        if args.command == 'download' or (args.command == 'scan' and not args.scan_only):
            download(data, scope)
    except (OSError, ValueError, HTTPException, EOFError, KeyboardInterrupt) as exc:
        parser.exit(1, f'Stopped: {exc or "cancelled"}\n')


if __name__ == '__main__':
    main()
