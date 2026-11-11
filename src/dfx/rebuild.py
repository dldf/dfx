"""Reconvert existing review documents from their matching downloaded sources."""
import argparse
from pathlib import Path
import shutil
import sys
import tempfile

from .convert import convert_latex, convert_pdf
from .review import find_chrome, preview, validate_source
from .workflow import DOWNLOADS, PENDING, ROOT, review_documents


def downloaded_source(folder, root=ROOT):
    """Match the workflow path, allowing a newer version of the downloaded file."""
    downloads = (root / DOWNLOADS).resolve()
    relative = folder.relative_to((root / PENDING).resolve())
    directory = downloads / relative.parent
    markdown = (folder / 'content.md').read_text(encoding='utf-8')
    suffix = '.tex' if '<!-- pdf2md-latex:' in markdown else '.pdf'
    candidates = sorted(path for path in directory.iterdir()
                        if path.is_file() and path.stem == folder.name and path.suffix.lower() == suffix
                        and path.resolve().is_relative_to(downloads)) if directory.is_dir() else []
    if len(candidates) != 1:
        raise ValueError(f'Expected one matching {suffix} source in {directory}; found {len(candidates)}.')
    source = candidates[0].resolve()
    if suffix == '.pdf':
        validate_source(source, None)
    return source


def remove_output(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def rebuild_document(folder, chrome, root=ROOT):
    source = downloaded_source(folder, root)
    # A sibling has the same depth as the destination, so relative source links
    # in the conversion log remain correct after the generated files are moved.
    temporary = Path(tempfile.mkdtemp(prefix='.dfx-rebuild-', dir=folder.parent))
    temporary.rmdir()  # Conversion requires a destination that does not exist.
    try:
        if source.suffix.lower() == '.tex':
            convert_latex(source, temporary)
        else:
            convert_pdf(source, temporary)
        # Replace only generated outputs, leaving nested documents and other
        # files alone. Successful rebuilding intentionally discards old edits.
        for name in ('content.md', 'assets', 'review', 'index.html', 'preview.html'):
            remove_output(folder / name)
        for generated in temporary.iterdir():
            generated.rename(folder / generated.name)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    try:
        preview(source, folder / 'content.md', chrome)
    except Exception as exc:
        raise RuntimeError(f'Markdown rebuilt, but preview failed: {exc}. '
                           f'Retry dfx review "{folder}".') from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'Replaces Markdown edits, assets, review notes, HTML, and previews without backups. '
        'Uses current conversion defaults; previous page-mode overrides are not retained.'))
    parser.add_argument('folder', type=Path, help=f'{PENDING}, a subfolder, or one document folder')
    parser.add_argument('--chrome', help='Chrome executable used to generate review images and HTML')
    args = parser.parse_args(argv)
    try:
        documents = review_documents(args.folder, ROOT)
        chrome = find_chrome(args.chrome)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    failures = 0
    for folder in documents:
        try:
            rebuild_document(folder, chrome, ROOT)
            print(f'Rebuilt: {folder.relative_to(ROOT)}', flush=True)
        except Exception as exc:
            failures += 1
            print(f'Rebuild failed for {folder}: {exc}', file=sys.stderr, flush=True)
    print(f'Rebuilt {len(documents) - failures} documents; {failures} failed.')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
