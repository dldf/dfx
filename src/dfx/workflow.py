"""Folder names and document paths shared by the local review workflow."""
from pathlib import Path
import os
import shlex
import subprocess

def project_root():
    """Use the checkout for editable installs, or the working directory otherwise."""
    source = Path(__file__).resolve().parent.parent
    if source.name == 'src' and (source.parent / 'pyproject.toml').is_file():
        return source.parent
    return Path.cwd().resolve()


ROOT = project_root()
DOWNLOADS = '1_download'
PENDING = '2_review'
READY = '3_publish'
DIAGNOSTIC = 'diagnostic.md'


def diagnostic_only(folder):
    """Recognize the exact pre-conversion layout created by dfx diagnose."""
    review = folder / 'review'
    report = review / DIAGNOSTIC
    return (not folder.is_symlink() and folder.is_dir()
            and set(folder.iterdir()) == {review}
            and not review.is_symlink() and review.is_dir()
            and set(review.iterdir()) == {report}
            and not report.is_symlink() and report.is_file())


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
    args = ['dfx', action, str(folder)]
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
    return any((folder / name).is_file() for name in ('index.html', 'frompdf.html'))


def review_documents(folder, root=ROOT):
    pending = (root / PENDING).resolve()
    folder = folder.resolve()
    if not folder.is_dir() or not folder.is_relative_to(pending):
        raise ValueError(f'Choose an existing folder under {pending}.')
    documents = []
    for directory, children, files in os.walk(folder, followlinks=False):
        path = Path(directory)
        is_document = path != pending and 'content.md' in files and not (path / 'content.md').is_symlink()
        children[:] = sorted(name for name in children
                             if not (is_document and name in ('assets', 'review'))
                             and not name.startswith('.dfx-rebuild-')
                             and not (path / name).is_symlink())
        if is_document:
            documents.append(path)
    if not documents:
        raise ValueError('No review documents with content.md found in this folder.')
    return documents
