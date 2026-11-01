"""Folder names and document paths shared by the local review workflow."""
from pathlib import Path
import os
import shlex
import subprocess

ROOT = Path(__file__).resolve().parent
DOWNLOADS = '1_downloads'
PENDING = '2_pending_review'
READY = '3_ready_to_publish'


def document_markdown(path):
    path = Path(path)
    return path / 'content.md' if path.is_dir() else path


def html_name(folder):
    # Preserve the layout of old conversions when explicitly reviewing/staging them.
    return 'preview.html' if (folder / 'preview.html').exists() and not (folder / 'index.html').exists() else 'index.html'


def next_command(action, folder, chrome=None):
    folder = Path(folder).resolve()
    try:
        folder = folder.relative_to(Path.cwd())
    except ValueError:
        pass
    args = ['pdf', action, str(folder)]
    if chrome:
        args.extend(['--chrome', chrome])
    return subprocess.list2cmdline(args) if os.name == 'nt' else shlex.join(args)


def conversion_destination(pdf, base, target, root=None):
    root = (root or ROOT).resolve()
    pdf, base, target = pdf.resolve(), base.resolve(), target.resolve()
    downloads = root / DOWNLOADS
    if target == root / PENDING and pdf.is_relative_to(downloads):
        base = downloads
    return target / pdf.relative_to(base).with_suffix('')


def document_present(folder):
    """A directory or a receipt alone is not a converted document."""
    if not folder.is_dir():
        return False
    if any(path.is_file() for path in folder.glob('*.md')):
        return True
    return all((folder / name).is_file() for name in (
        'index.html', 'assets/main.css', 'assets/_pdf2md/content.md', 'assets/_pdf2md/log.md'))
