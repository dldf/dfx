#!/usr/bin/env python3
"""Move a human-reviewed Markdown document into 3_ready_to_publish/; do not rebuild HTML."""
import argparse
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup

from workflow import ROOT, PENDING, READY, document_markdown, html_name


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
            raise ValueError(f'HTML {kind} must exist under assets/: {reference}. Run pdf review first.')


def tree_snapshot(folder):
    """Fingerprint the complete new publication layout, rejecting symlinks."""
    result = {}
    for path in sorted(folder.rglob('*')):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f'Unsupported publication file: {path}')
        relative = path.relative_to(folder).as_posix()
        result[relative + '/' if path.is_dir() else relative] = None if path.is_dir() else fingerprint(path)
    return result


def relocate_links(text, old_file, new_file, source, destination, moved_files):
    """Rebase Markdown/HTML links without reformatting the archived document."""
    def target(value):
        angle = value.startswith('<') and value.endswith('>')
        value = value[1:-1] if angle else value
        uri = urlsplit(value)
        if uri.scheme or uri.netloc or not uri.path or uri.path.startswith('/'):
            return '<' + value + '>' if angle else value
        old = (old_file.parent / unquote(uri.path)).resolve()
        new = moved_files.get(old, old)
        if old.is_relative_to(source / 'assets'):
            new = destination / 'assets' / old.relative_to(source / 'assets')
        relative = quote(os.path.relpath(new, new_file.parent).replace(os.sep, '/'), safe='/')
        value = uri._replace(path=relative).geturl()
        return '<' + value + '>' if angle else value

    # Leave fenced and inline code examples intact. Only link destinations change.
    code = re.compile(r'(^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\2[ \t]*$|(`+)[^`]*?\3)', re.M | re.S)
    url = r'(<[^>\n]+>|(?:\\.|[^\s()<>]|\([^()\n]*\))+)'

    def prose(value):
        value = re.sub(r'(\]\([ \t]*)' + url, lambda m: m[1] + target(m[2]), value)
        value = re.sub(r'(^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*)' + url,
                       lambda m: m[1] + target(m[2]), value, flags=re.M)
        return re.sub(r'''(\b(?:href|src)\s*=\s*)(["'])(.*?)\2''',
                      lambda m: m[1] + m[2] + target(m[3]) + m[2], value)

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


def write_receipt(source, text):
    # Install a complete receipt exclusively; never truncate an existing one.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=source, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        (source / 'ready_to_publish.txt').hardlink_to(temporary)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify_document(markdown, reviewer=None, *, root=None):
    root = (root or ROOT).resolve()
    markdown = document_markdown(markdown)
    markdown = require_local(markdown, root)
    pending = root / PENDING
    source = markdown.parent
    if markdown.suffix.lower() != '.md' or not source.is_relative_to(pending) or source == pending:
        raise ValueError(f'Choose a Markdown file inside a document folder under {PENDING}/.')
    destination = require_local(root / READY / source.relative_to(pending), root)
    if destination.exists():
        raise FileExistsError(f'Destination already exists; nothing moved: {destination}')
    receipt = source / 'ready_to_publish.txt'
    if receipt.exists() or receipt.is_symlink():
        raise FileExistsError(f'Review receipt already exists; nothing moved: {receipt}')
    review = require_local(source / 'review', root)
    if review.exists() and not review.is_dir():
        raise ValueError(f'Expected a review directory: {review}')
    html = html_name(source)
    modern = html == 'index.html'
    log_name = 'review/log.md' if modern else None
    original = snapshot(source, markdown.name, html, log_name)
    if modern:
        if 'assets/main.css' not in original:
            raise ValueError('Required assets/main.css is missing. Run pdf review first.')
        if (source / 'assets/_pdf2md').exists():
            raise FileExistsError('assets/_pdf2md is reserved for staged Markdown and the review log; nothing moved.')
    check_html_assets(source)
    if reviewer is None:
        reviewer = input('Reviewed by (name): ')
    reviewer = reviewer.strip()
    if not reviewer or any(char in reviewer for char in '\r\n'):
        raise ValueError('A nonempty reviewer name on one line is required. Nothing moved.')
    stamp = datetime.now().astimezone()
    receipt_text = (f'Reviewed by "{reviewer}"; on "{stamp:%Y-%m-%d}" at "{stamp:%H:%M:%S %z}".\n'
                    f'{destination.as_uri()}/\n')
    phase = 'copying'
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.verify-', dir=destination.parent) as temporary:
            staging = Path(temporary)
            if (source / 'assets').exists():
                shutil.copytree(source / 'assets', staging / 'assets', symlinks=True)
            if modern:
                archive = Path('assets/_pdf2md')
                (staging / archive).mkdir()
                shutil.copy2(markdown, staging / archive / 'content.md', follow_symlinks=False)
                shutil.copy2(source / log_name, staging / archive / 'log.md', follow_symlinks=False)
                shutil.copy2(source / html, staging / html, follow_symlinks=False)
                expected = {key: value for key, value in original.items() if key.startswith('assets/')}
                expected.update({'index.html': original['html'], 'assets/_pdf2md/': None,
                                 'assets/_pdf2md/content.md': original['markdown'],
                                 'assets/_pdf2md/log.md': original['log']})
                if tree_snapshot(staging) != expected:
                    raise ValueError('Copied files did not match the originals.')
                moved_files = {markdown: destination / archive / 'content.md',
                               source / log_name: destination / archive / 'log.md',
                               source / html: destination / html}
                for old_file, name in ((markdown, 'content.md'), (source / log_name, 'log.md')):
                    copied = staging / archive / name
                    with copied.open(encoding='utf-8', newline='') as stream:
                        updated = relocate_links(stream.read(), old_file, destination / archive / name,
                                                 source, destination, moved_files)
                    with copied.open('w', encoding='utf-8', newline='') as stream:
                        stream.write(updated)
                    expected[(archive / name).as_posix()] = hashlib.sha256(updated.encode('utf-8')).hexdigest()
                if tree_snapshot(staging) != expected:
                    raise ValueError('Archived links could not be saved correctly.')
            else:
                shutil.copy2(markdown, staging / markdown.name, follow_symlinks=False)
                shutil.copy2(source / html, staging / 'frompdf.html', follow_symlinks=False)
                if snapshot(staging, markdown.name, 'frompdf.html') != original:
                    raise ValueError('Copied files did not match the originals.')
            if snapshot(source, markdown.name, html, log_name) != original:
                raise ValueError('Source files changed during verification.')
            phase = 'installing'
            install_copy(staging, destination, 'index.html' if modern else markdown.name)
            installed = tree_snapshot(destination) if modern else snapshot(destination, markdown.name, 'frompdf.html')
            if installed != (expected if modern else original):
                raise ValueError('Destination files did not match the originals.')
            phase = 'receipt'
            write_receipt(source, receipt_text)
            # Stop rather than deleting a newer edit made while publishing.
            if snapshot(source, markdown.name, html, log_name) != original:
                raise ValueError('Source files changed; originals retained.')
            phase = 'cleanup'
            markdown.unlink()
            (source / html).unlink()
            if (source / 'assets').exists():
                shutil.rmtree(source / 'assets')
            if review.exists():
                shutil.rmtree(review)
        return destination
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        detail = str(exc) or 'interrupted'
        if phase == 'copying':
            message = f'Copy failed; originals and review files are unchanged: {detail}'
        elif phase == 'installing':
            message = (f'Installation failed; originals and review files are unchanged. '
                       f'Check the possibly incomplete destination {destination} before retrying: {detail}')
        elif phase == 'receipt':
            message = (f'Ready copy is complete at {destination}; originals and review files are retained. '
                       f'Check the receipt at {receipt} before completing cleanup: {detail}')
        else:
            message = (f'Ready copy is complete at {destination}, and the receipt is at {receipt}. '
                       f'Cleanup was interrupted; duplicate files may remain under {source}: {detail}')
        raise RuntimeError(message) from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('markdown', type=Path, help='Reviewed document folder (or Markdown file) under 2_pending_review/')
    args = parser.parse_args()
    try:
        destination = verify_document(args.markdown)
    except (OSError, ValueError, RuntimeError, EOFError, KeyboardInterrupt) as exc:
        parser.exit(1, f'Verification stopped: {exc or "cancelled; nothing moved"}\n')
    print(f'Ready to publish: {destination}\nReview folder removed; receipt left in the original document folder.')


if __name__ == '__main__':
    main()
