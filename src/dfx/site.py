#!/usr/bin/env python3
"""Discover PDFs, audit HTML accessibility, and display document and score trees."""
import argparse
from collections import Counter, deque
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
from pathlib import Path
import posixpath
import re
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from .workflow import ROOT, DOWNLOADS, PENDING, READY, document_present

INVENTORY = ROOT / 'data' / 'pdfs.json'
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


def load_exclusions():
    path = ROOT / '.pdfignore'
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return ()
    folders = []
    for number, line in enumerate(lines, 1):
        value = line.strip()
        if not value or value.startswith('#'):
            continue
        try:
            if '*' in value or urlsplit(value).query or urlsplit(value).fragment:
                raise ValueError('Use literal folder URLs without wildcards, queries, or fragments.')
            folders.append(scope_url(value))
        except ValueError as exc:
            raise ValueError(f'{path.name}:{number}: {exc}') from exc
    return tuple(folders)


def excluded(url, folders):
    return any(within(url, folder) for folder in folders)


def load():
    legacy = INVENTORY.with_name('inventory.json')
    if legacy.exists():
        if INVENTORY.exists():
            raise ValueError('Both inventory.json and pdfs.json exist; resolve the duplicate before continuing. Neither file was changed.')
        # Link first so a concurrent writer cannot be overwritten; preserve bytes.
        INVENTORY.hardlink_to(legacy)
        legacy.unlink()
    try:
        contents = INVENTORY.read_text()
    except FileNotFoundError:
        contents = ''
    if not contents.strip():
        return {'version': 1, 'scans': {}, 'documents': {}}
    data = json.loads(contents)
    if data.get('version') != 1 or not isinstance(data.get('documents'), dict) or not isinstance(data.get('scans'), dict):
        raise ValueError('Expected inventory version 1 with documents and scans objects.')
    return data


def save(data):
    save_json(INVENTORY, data)


def save_json(path, data):
    # Adapted from the archived catalog approach; replace atomically.
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False, encoding='utf-8') as stream:
        temporary = Path(stream.name)
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write('\n')
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Client:
    def __init__(self, scope, exclusions=()):
        self.scope = scope
        self.exclusions = exclusions
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
                    raise ValueError(f'Redirect outside scope, excluded by .pdfignore, or disallowed by robots.txt: {target}')
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
        return url == self.robots_url or (within(url, self.scope)
                                         and not excluded(url, self.exclusions)
                                         and self.robots.can_fetch(AGENT, url))

    def open(self, url, method='GET'):
        if not self.allowed(url):
            raise ValueError(f'Outside scope, excluded by .pdfignore, or disallowed by robots.txt: {url}')
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        return self.opener.open(Request(url, method=method, headers={
            'User-Agent': AGENT, 'Accept-Encoding': 'identity'}), timeout=20)


def higher_limit(limit, interactive):
    print(f'Scan limit reached ({limit} URLs); discovered links remain. Increase --max-urls above {limit} to continue discovery.')
    if not interactive:
        return None
    while True:
        try:
            answer = input(f'New --max-urls greater than {limit}, or Enter to stop: ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\nStopped at the current limit.')
            return None
        if not answer:
            return None
        try:
            value = int(answer)
            if value > limit:
                return value
        except ValueError:
            pass
        print(f'Enter a whole number greater than {limit}.')


def scan(data, scope, limit, *, folders=False, on_page=None, interactive=False):
    pdf_inventory = not folders and on_page is None
    # HTML audits use the same crawler but PDF exclusions do not apply to them.
    exclusions = load_exclusions() if on_page is None else ()
    if excluded(scope, exclusions):
        raise ValueError('The scan root is excluded by .pdfignore.')
    client = Client(scope, exclusions)
    queue, queued, seen = deque([scope]), {scope}, set()
    errors, outside, found = [], set(), set()
    references, pdf_targets = {}, {}
    stamp = now()
    while queue:
        # Redirect destinations may already be queued. They do not imply more work.
        while queue and queue[0] in seen:
            queue.popleft()
        if not queue:
            break
        if len(seen) >= limit:
            increased = higher_limit(limit, interactive)
            if increased is None:
                break
            limit = increased
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
                    if pdf_inventory:
                        entry = data['documents'].setdefault(final, {'discovered_at': stamp})
                        length = response.headers.get('Content-Length', '')
                        entry.update({'last_seen': stamp, 'size_bytes': int(length) if length.isdigit() else None})
                        entry.setdefault('referring_pages', [])
                        pdf_targets[url] = pdf_targets[final] = final
                    found.add(final)
                    if pdf_inventory:
                        print(f'Found PDF: {final}', flush=True)
                    continue
                if mime not in ('text/html', 'application/xhtml+xml'):
                    continue
                body = response.read(5_000_001)
                if len(body) > 5_000_000:
                    raise ValueError('HTML exceeds 5 MB; page skipped')
                soup = BeautifulSoup(body, 'html.parser')
                if on_page is not None:
                    on_page(final, client)
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
                    if excluded(target, exclusions):
                        continue
                    if pdf_inventory:
                        text = ' '.join(link.get_text().split()) if link.name == 'a' else ''
                        reference = {'text': text, 'url': final}
                        target_references = references.setdefault(target, [])
                        if reference not in target_references:
                            target_references.append(reference)
                    if target not in queued and target not in seen:
                        queue.append(target)
                        queued.add(target)
        except (OSError, ValueError, HTTPException) as exc:
            errors.append({'url': url, 'error': str(exc)})
            if not folders:
                print(f'Skipped {url}: {exc}')
    summary = {'scanned_at': stamp, 'visited': len(seen), 'pdfs_found': len(found),
               'limited': bool(queue), 'max_urls': limit, 'errors': errors,
               'outside_pdfs': sorted(outside)}
    if folders:
        counts = Counter()
        for url in found:
            parts = urlsplit(url)
            folder = parts._replace(path=parts.path.rsplit('/', 1)[0] + '/', query='', fragment='').geturl()
            counts[folder] += 1
        width = max([len('PDF folder'), *map(len, counts)])
        print(f'{"PDF folder":<{width}}  PDFs')
        for folder, count in sorted(counts.items()):
            print(f'{folder:<{width}}  {count:>4}')
    elif pdf_inventory:
        # Collect after crawling: a referring page may be visited after its PDF,
        # and an extensionless link may only reveal its PDF target on redirect.
        for target, target_references in references.items():
            final = pdf_targets.get(target)
            if final is None:
                continue
            stored = data['documents'][final]['referring_pages']
            for reference in target_references:
                if reference not in stored:
                    stored.append(reference)
        data['scans'][scope] = summary
        save(data)
    if on_page is None:
        print(f'Found {len(found)} unique PDFs; {len(errors)} skipped URLs; {len(outside)} PDF links outside scope.')
    else:
        print(f'Discovery visited {len(seen)} URLs; {len(errors)} skipped URLs.')
    if not queue:
        if errors:
            print('Discovery finished with skipped URLs; increasing --max-urls alone will not resolve these errors.')
        else:
            print('Discovery complete: all discovered links within the allowed scope were processed.')
    if folders and (queue or errors):
        print('Counts may be incomplete because the scan limit was reached or URLs were skipped.')
    print('Scope: linked content only; unlinked files and JavaScript-generated links may be missed.')
    return summary


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
    exclusions = load_exclusions()
    pending = [(url, entry) for url, entry in documents(data, scope)
               if not excluded(url, exclusions) and document_stage(url) is None]
    known = sum(entry['size_bytes'] for _, entry in pending if entry.get('size_bytes') is not None)
    unknown = sum(entry.get('size_bytes') is None for _, entry in pending)
    print(f'{len(pending)} PDFs to download; reported size {known / 1_000_000:.2f} MB'
          + (f' plus {unknown} with unknown size.' if unknown else '.'))
    if not pending:
        return
    print('Documents present in any workflow folder or excluded by .pdfignore are skipped. Reported sizes may change.')
    if input(f'Download these PDFs into {DOWNLOADS}/? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Cancelled. No PDFs downloaded; scan results remain in data/pdfs.json.')
        return
    client = Client(scope, exclusions)
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
    parser = argparse.ArgumentParser(description=__doc__, epilog='Tree status comes from files in the three workflow folders. Use dfx stage to prepare a reviewed document for publication.')
    sub = parser.add_subparsers(dest='command', required=True)
    scanner = sub.add_parser('scan', help='Discover PDFs, show tree and ask before downloading')
    scanner.add_argument('url', help='URL folder, e.g. https://example.edu/department/')
    scanner.add_argument('--max-urls', '--max-pages', dest='max_urls', type=int, default=9999, help='Maximum visited URLs (default 9999)')
    scanner.add_argument('--scan-only', action='store_true', help='Save inventory without asking to download')
    scanner.add_argument('--folders', action='store_true', help='Only summarize PDF folders and counts; no tree, downloads, or inventory changes')
    for name, help_text in (('tree', 'Display PDF status tree'),
                            ('download', 'Confirm downloads from a previous scan'),
                            ('audit', 'Crawl HTML pages and save Lighthouse accessibility scores'),
                            ('scores', 'Display saved HTML accessibility scores as a tree')):
        command = sub.add_parser(name, help=help_text)
        command.add_argument('url', nargs='?', help='Scope URL; defaults to most recent scan')
        if name == 'audit':
            command.add_argument('--max-urls', type=int, default=9999, help='Maximum visited URLs (default 9999)')
            command.add_argument('--chrome', help='Chrome or Chromium executable path')
        if name == 'scores':
            command.add_argument('--below', type=int, help='Show only scores strictly below this integer threshold (0–100)')
    args = parser.parse_args()
    try:
        if args.command in ('scan', 'audit') and args.max_urls < 1:
            raise ValueError('--max-urls must be positive.')
        if args.command == 'scores' and args.below is not None and not 0 <= args.below <= 100:
            raise ValueError('--below must be an integer from 0 to 100.')
        if args.command == 'scan' and args.folders:
            scan({}, scope_url(args.url), args.max_urls, folders=True, interactive=sys.stdin.isatty())
            return
        data = load()
        latest = max(data['scans'], key=lambda s: data['scans'][s]['scanned_at'], default=None)
        if not args.url and not latest:
            raise ValueError('No scan recorded. Run dfx site scan URL first.')
        scope = scope_url(args.url or latest)
        if args.command in ('audit', 'scores'):
            from . import site_audit
            if args.command == 'audit':
                site_audit.audit(scope, args.max_urls, chrome=args.chrome, interactive=sys.stdin.isatty())
            else:
                site_audit.scores(scope, below=args.below)
            return
        if args.command == 'scan':
            scan(data, scope, args.max_urls, interactive=sys.stdin.isatty())
        if args.command in ('tree', 'scan'):
            tree(data, scope)
        if args.command == 'download' or (args.command == 'scan' and not args.scan_only):
            download(data, scope)
    except (OSError, ValueError, HTTPException, EOFError, KeyboardInterrupt) as exc:
        parser.exit(1, f'Stopped: {exc or "cancelled"}\n')


if __name__ == '__main__':
    main()
