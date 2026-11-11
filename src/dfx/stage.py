#!/usr/bin/env python3
"""Stage reviewed HTML and assets; retain Markdown and logs in 2_review/."""
import argparse
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup

from .workflow import ROOT, PENDING, READY, document_markdown, html_name, review_documents


def require_local(path, root):
    """Keep moves/deletions within the project and reject symlink redirects."""
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(root):
        raise ValueError(f'Path must be inside {root}: {path}')
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink():
            raise ValueError(f'Symlinks are not supported during verification: {candidate}')
    return path


def fingerprint(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def snapshot(folder, markdown_name, html_name, log_name=None):
    result = {}
    files = [(markdown_name, 'markdown'), (html_name, 'html')]
    if log_name:
        files.append((log_name, 'log'))
    for name, key in files:
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Required regular file is missing: {path}')
        result[key] = fingerprint(path)
    assets = folder / 'assets'
    if assets.is_symlink() or (assets.exists() and not assets.is_dir()):
        raise ValueError(f'Expected a regular assets directory: {assets}')
    if assets.exists():
        result['assets/'] = None
        for path in sorted(assets.rglob('*')):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ValueError(f'Unsupported asset: {path}')
            relative = path.relative_to(folder).as_posix()
            result[relative + '/' if path.is_dir() else relative] = None if path.is_dir() else fingerprint(path)
    return result


def check_html_assets(source):
    html = BeautifulSoup((source / html_name(source)).read_text(encoding='utf-8'), 'html.parser')
    resources = [(image.get('src', ''), 'image') for image in html.find_all('img')]
    resources += [(link.get('href', ''), 'stylesheet') for link in html.select('link[rel~=stylesheet]')]
    for reference, kind in resources:
        uri = urlsplit(reference)
        if uri.scheme in ('https', 'http', 'data') or uri.netloc:
            continue
        asset = (source / unquote(uri.path)).resolve()
        if uri.scheme or not asset.is_relative_to(source / 'assets') or not asset.is_file():
            raise ValueError(f'HTML {kind} must exist under assets/: {reference}. Run dfx review first.')


def tree_snapshot(folder):
    """Fingerprint the complete new publication layout, rejecting symlinks."""
    result = {}
    for path in sorted(folder.rglob('*')):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f'Unsupported publication file: {path}')
        relative = path.relative_to(folder).as_posix()
        result[relative + '/' if path.is_dir() else relative] = None if path.is_dir() else fingerprint(path)
    return result


def relocate_links(text, old_file, new_file, source, destination, moved_files, removed_files=()):
    """Update references to moved files while retaining text and code examples."""
    def target(value):
        angle = value.startswith('<') and value.endswith('>')
        value = value[1:-1] if angle else value
        uri = urlsplit(value)
        if uri.scheme or uri.netloc or not uri.path or uri.path.startswith('/'):
            return '<' + value + '>' if angle else value
        old = (old_file.parent / unquote(uri.path)).resolve()
        if old in removed_files:
            return None
        new = moved_files.get(old, old)
        if old.is_relative_to(source / 'assets'):
            new = destination / 'assets' / old.relative_to(source / 'assets')
        if new == old and new_file == old_file:
            return '<' + value + '>' if angle else value
        relative = quote(os.path.relpath(new, new_file.parent).replace(os.sep, '/'), safe='/')
        value = uri._replace(path=relative).geturl()
        return '<' + value + '>' if angle else value

    # Leave fenced and inline code examples intact. Only link destinations change.
    code = re.compile(r'(^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\2[ \t]*$|(`+)[^`]*?\3)', re.M | re.S)
    url = r'(<[^>\n]+>|(?:\\.|[^\s()<>]|\([^()\n]*\))+)'

    def prose(value):
        def inline(match):
            updated = target(match[3])
            return match[1] + updated + match[4] if updated is not None else match[2]
        value = re.sub(r'(!?\[([^\]\n]*)\]\([ \t]*)' + url + r'([^\n)]*\))', inline, value)
        value = re.sub(r'(^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*)' + url,
                       lambda m: m[1] + (target(m[2]) or ''), value, flags=re.M)
        return re.sub(r'''(\b(?:href|src)\s*=\s*)(["'])(.*?)\2''',
                      lambda m: m[1] + m[2] + (target(m[3]) or '') + m[2], value)

    parts, start = [], 0
    for match in code.finditer(text):
        parts.extend((prose(text[start:match.start()]), match[0]))
        start = match.end()
    parts.append(prose(text[start:]))
    return ''.join(parts)


def install_copy(staging, destination, completion_name):
    # mkdir exclusively reserves the final destination. Hard links also refuse
    # overwrites. Install the completion file last so partial copies stay unready.
    destination.mkdir()
    for path in sorted(staging.rglob('*')):
        if path.is_dir():
            (destination / path.relative_to(staging)).mkdir()
    files = sorted(path for path in staging.rglob('*') if path.is_file())
    files.sort(key=lambda path: path.name == completion_name and path.parent == staging)
    for path in files:
        (destination / path.relative_to(staging)).hardlink_to(path)


def write_metadata(path, data, expected):
    """Replace one retained file atomically, refusing a concurrent edit."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or fingerprint(path) != expected:
            raise ValueError(f'Source file changed during verification: {path}')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify_document(markdown, reviewer=None, *, root=None):
    root = (root or ROOT).resolve()
    markdown = require_local(document_markdown(markdown), root)
    pending = root / PENDING
    source = markdown.parent
    if markdown.suffix.lower() != '.md' or not source.is_relative_to(pending) or source == pending:
        raise ValueError(f'Choose a Markdown file inside a document folder under {PENDING}/.')
    destination = require_local(root / READY / source.relative_to(pending), root)
    if destination.exists():
        raise FileExistsError(f'Destination already exists; nothing moved: {destination}')
    review = require_local(source / 'review', root)
    if review.exists() and not review.is_dir():
        raise ValueError(f'Expected a review directory: {review}')
    html = html_name(source)
    published_html = 'index.html' if html == 'index.html' else 'frompdf.html'
    log = review / 'log.md'
    log_name = 'review/log.md' if html == 'index.html' or log.exists() else None
    original = snapshot(source, markdown.name, html, log_name)
    if (source / 'assets/_pdf2md').exists():
        raise FileExistsError('Review metadata found in assets/_pdf2md; nothing moved.')
    check_html_assets(source)
    previews = {}
    if review.exists():
        for path in review.iterdir():
            if re.fullmatch(r'(?:p[0-9]+-(?:original|revised)|document-revised)\.png', path.name):
                require_local(path, root)
                previews[path] = fingerprint(path)
    # A previous HTML preview can remain after switching to index.html.
    if html != 'preview.html' and (source / 'preview.html').exists():
        path = require_local(source / 'preview.html', root)
        previews[path] = fingerprint(path)
    if reviewer is None:
        reviewer = input('Reviewed by (name): ')
    reviewer = reviewer.strip()
    if not reviewer or any(char in reviewer for char in '\r\n'):
        raise ValueError('A nonempty reviewer name on one line is required. Nothing moved.')
    stamp = datetime.now().astimezone()
    moved = {source / html: destination / published_html}
    if source / 'preview.html' in previews:
        moved[source / 'preview.html'] = destination / published_html
    phase = 'copying'
    try:
        retained = {}
        for path, key in ((markdown, 'markdown'), (log, 'log')):
            if key == 'log' and not log_name:
                text = ''
            else:
                with path.open(encoding='utf-8', newline='') as stream:
                    text = stream.read()
            updated = relocate_links(text, path, path, source, destination, moved,
                                     set(previews) - set(moved))
            if key == 'log':
                newline = '\r\n' if '\r\n' in text else '\n'
                link = quote(os.path.relpath(destination / published_html, review), safe='/')
                updated = (updated.rstrip('\r\n') + newline * 2
                           + f'Reviewed by "{reviewer}"; staged on {stamp:%Y-%m-%d %H:%M:%S %z}. '
                           + f'[HTML]({link})' + newline)
            retained[path] = (key, updated.encode('utf-8'))
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.verify-', dir=destination.parent) as temporary:
            staging = Path(temporary)
            if (source / 'assets').exists():
                shutil.copytree(source / 'assets', staging / 'assets', symlinks=True)
            shutil.copy2(source / html, staging / published_html, follow_symlinks=False)
            expected = {key: value for key, value in original.items() if key.startswith('assets/')}
            expected[published_html] = original['html']
            if tree_snapshot(staging) != expected:
                raise ValueError('Copied files did not match the originals.')
            if snapshot(source, markdown.name, html, log_name) != original:
                raise ValueError('Source files changed during verification.')
            phase = 'installing'
            install_copy(staging, destination, published_html)
            if tree_snapshot(destination) != expected:
                raise ValueError('Destination files did not match the originals.')
            phase = 'metadata'
            if snapshot(source, markdown.name, html, log_name) != original:
                raise ValueError('Source files changed; originals retained.')
            review.mkdir(exist_ok=True)
            for path, (key, data) in retained.items():
                if key == 'log' and not log_name:
                    # Legacy previews may have no log. Create it exclusively.
                    with path.open('xb') as stream:
                        stream.write(data)
                    log_name = 'review/log.md'
                elif hashlib.sha256(data).hexdigest() != original[key]:
                    write_metadata(path, data, original[key])
                original[key] = hashlib.sha256(data).hexdigest()
            if snapshot(source, markdown.name, html, log_name) != original:
                raise ValueError('Source files changed; originals retained.')
            phase = 'cleanup'
            for path, digest in previews.items():
                if path.is_symlink() or fingerprint(path) != digest:
                    raise ValueError(f'Preview changed during verification: {path}')
            (source / html).unlink()
            if (source / 'assets').exists():
                shutil.rmtree(source / 'assets')
            for path in previews:
                path.unlink()
        return destination
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        detail = str(exc) or 'interrupted'
        if phase == 'copying':
            message = f'Copy failed; originals and review files are unchanged: {detail}'
        elif phase == 'installing':
            message = (f'Installation failed; originals and review files are unchanged. '
                       f'Check the possibly incomplete destination {destination} before retrying: {detail}')
        elif phase == 'metadata':
            message = (f'Ready copy is complete at {destination}; cleanup has not started. '
                       f'Could not finish updating the retained Markdown and log: {detail}')
        else:
            message = (f'Ready copy is complete at {destination}; Markdown and log retained. '
                       f'Cleanup was interrupted; duplicate files may remain under {source}: {detail}')
        raise RuntimeError(message) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path,
                        help='2_review, a subfolder, one document folder, or a Markdown file')
    args = parser.parse_args(argv)
    try:
        root = ROOT.resolve()
        selected = require_local(args.folder, root)
        if selected.is_file():
            documents = [selected]
        else:
            documents = review_documents(selected, root)
        pending, skipped = [], 0
        for document in documents:
            markdown = document_markdown(document)
            source = markdown.parent
            if not source.is_relative_to(root / PENDING) or source == root / PENDING:
                raise ValueError(f'Choose documents under {PENDING}/.')
            destination = require_local(root / READY / source.relative_to(root / PENDING), root)
            if destination.exists():
                skipped += 1
                print(f'Skipped (destination exists): {source.relative_to(root)}', flush=True)
            else:
                pending.append(markdown)
        reviewer = input('Reviewed by (name): ').strip() if pending else ''
        if pending and (not reviewer or any(char in reviewer for char in '\r\n')):
            raise ValueError('A nonempty reviewer name on one line is required. Nothing moved.')
    except (OSError, ValueError, EOFError, KeyboardInterrupt) as exc:
        parser.exit(1, f'Staging stopped: {exc or "cancelled; nothing moved"}\n')
    failed = 0
    for markdown in pending:
        try:
            destination = verify_document(markdown, reviewer, root=root)
            print(f'Staged: {destination.relative_to(root)}', flush=True)
        except (OSError, ValueError, RuntimeError) as exc:
            failed += 1
            print(f'Stage failed for {markdown.parent.relative_to(root)}: {exc}', file=sys.stderr, flush=True)
    print(f'Staged {len(pending) - failed} documents; {skipped} skipped; {failed} failed.')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
