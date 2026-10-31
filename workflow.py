"""Folder names and document paths shared by the local review workflow."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOWNLOADS = '1_downloads'
PENDING = '2_pending_review'
READY = '3_ready_to_publish'


def conversion_destination(pdf, base, target, root=None):
    root = (root or ROOT).resolve()
    pdf, base, target = pdf.resolve(), base.resolve(), target.resolve()
    downloads = root / DOWNLOADS
    if target == root / PENDING and pdf.is_relative_to(downloads):
        base = downloads
    return target / pdf.relative_to(base).with_suffix('')


def document_present(folder):
    """A directory or a receipt alone is not a converted document."""
    return folder.is_dir() and any(path.is_file() for path in folder.glob('*.md'))
