#!/usr/bin/env python3
"""Move a human-reviewed Markdown document into 3_ready_to_publish/; do not rebuild HTML."""
import argparse
from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from workflow import ROOT, PENDING, READY


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


def snapshot(folder, markdown_name, html_name):
    result = {}
    for name, key in ((markdown_name, 'markdown'), (html_name, 'html')):
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
    html = BeautifulSoup((source / 'preview.html').read_text(encoding='utf-8'), 'html.parser')
    for image in html.find_all('img'):
        uri = urlsplit(image.get('src', ''))
        if uri.scheme in ('https', 'http', 'data') or uri.netloc:
            continue
        asset = (source / unquote(uri.path)).resolve()
        if uri.scheme or not asset.is_relative_to(source / 'assets') or not asset.is_file():
            raise ValueError(f'HTML image must exist under assets/: {image.get("src")}. Refresh preview.py first.')


def install_copy(staging, destination, markdown_name):
    # mkdir exclusively reserves the final destination. Hard links also refuse
    # overwrites. Publish Markdown last so partial installs do not show state 3.
    destination.mkdir()
    for path in sorted(staging.rglob('*')):
        if path.is_dir():
            (destination / path.relative_to(staging)).mkdir()
    files = sorted(path for path in staging.rglob('*') if path.is_file())
    files.sort(key=lambda path: path.name == markdown_name and path.parent == staging)
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
    original = snapshot(source, markdown.name, 'preview.html')
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
            shutil.copy2(markdown, staging / markdown.name, follow_symlinks=False)
            shutil.copy2(source / 'preview.html', staging / 'frompdf.html', follow_symlinks=False)
            if (source / 'assets').exists():
                shutil.copytree(source / 'assets', staging / 'assets', symlinks=True)
            if snapshot(staging, markdown.name, 'frompdf.html') != original:
                raise ValueError('Copied files did not match the originals.')
            if snapshot(source, markdown.name, 'preview.html') != original:
                raise ValueError('Source files changed during verification.')
            phase = 'installing'
            install_copy(staging, destination, markdown.name)
            if snapshot(destination, markdown.name, 'frompdf.html') != original:
                raise ValueError('Destination files did not match the originals.')
            phase = 'receipt'
            write_receipt(source, receipt_text)
            # Stop rather than deleting a newer edit made while publishing.
            if snapshot(source, markdown.name, 'preview.html') != original:
                raise ValueError('Source files changed; originals retained.')
            phase = 'cleanup'
            markdown.unlink()
            (source / 'preview.html').unlink()
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
    parser.add_argument('markdown', type=Path, help='Reviewed Markdown in 2_pending_review/<document>/')
    args = parser.parse_args()
    try:
        destination = verify_document(args.markdown)
    except (OSError, ValueError, RuntimeError, EOFError, KeyboardInterrupt) as exc:
        parser.exit(1, f'Verification stopped: {exc or "cancelled; nothing moved"}\n')
    print(f'Ready to publish: {destination}\nReview files deleted; receipt left in the original document folder.')


if __name__ == '__main__':
    main()
