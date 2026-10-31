#!/usr/bin/env python3
"""Save preview.html beside Markdown and paired PNGs in review/; never edit Markdown."""
import argparse
import hashlib
from html import escape
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import webbrowser
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup, Comment
import pymupdf


def find_chrome(chrome=None):
    chrome = (chrome or shutil.which('google-chrome') or shutil.which('chromium')
              or '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if not shutil.which('pandoc') or not Path(chrome).is_file():
        raise ValueError('Install Pandoc and Google Chrome (or supply --chrome /path/to/chrome).')
    return chrome


def document_css(source):
    """Read the optional document-level font setting from Markdown front matter."""
    font = 'sans'
    front = re.match(r'\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)',
                     source, flags=re.S)
    if front:
        settings = re.findall(r'^font:[ \t]*(.*)$', front[1], flags=re.M)
        if len(settings) > 1:
            raise ValueError('Use only one font setting in the Markdown front matter.')
        if settings:
            value = re.split(r'\s+#', settings[0], maxsplit=1)[0].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '\"\'':
                value = value[1:-1]
            if value not in ('serif', 'sans'):
                raise ValueError('Markdown font must be serif or sans.')
            font = value
    family = '"Times New Roman", Times, serif' if font == 'serif' else 'system-ui, sans-serif'
    return Path(__file__).with_name('review.css').read_text() + f'\nbody {{ font-family: {family}; }}\n'


def render_body(markdown, folder, description_ids, continuation_indent=0):
    # Render lists without standalone anchors, then restore the anchors in the
    # HTML. An otherwise empty anchor paragraph can change spacing or nesting.
    anchor_pattern = r'^[ \t]*<a id="source-page(\d+)"></a>[ \t]*$'
    anchors = re.findall(anchor_pattern, markdown, flags=re.M)
    markdown = re.sub(anchor_pattern, '', markdown, flags=re.M)
    # Only remove the converter's review blocks, not ordinary quotations.
    markdown = re.sub(r'^[ \t]*> \*\*Review page \d+:\*\*[^\n]*(?:\n[ \t]*>[^\n]*)*',
                      '', markdown, flags=re.M)
    # Page previews render fragments independently. Supply invisible ancestors
    # for a list carried from the preceding page so it is not parsed as code.
    first_line = next((line for line in markdown.splitlines() if line.strip()), '')
    indentation = min(continuation_indent, len(first_line) - len(first_line.lstrip(' ')))
    if indentation:
        ancestors = [' ' * indent + '- <!-- pdf2md-list-continuation -->'
                     for indent in range(0, indentation, 4)]
        markdown = '\n'.join(ancestors) + '\n\n' + markdown
    result = subprocess.run(['pandoc', '--from=markdown+raw_html+fancy_lists+startnum-smart-implicit_figures', '--to=html5'],
                            input=markdown, capture_output=True, text=True, check=True)
    soup = BeautifulSoup(result.stdout, 'html.parser')
    for number in reversed(anchors):
        anchor = soup.new_tag('a', id=f'source-page{number}')
        marker = soup.find(string=lambda s: isinstance(s, Comment)
                           and str(s).strip() == f'Source page {number}')
        if marker is not None:
            marker.insert_after(anchor)
        else:
            soup.insert(0, anchor)
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)
                                and str(s).strip() == 'pdf2md-list-continuation'):
        item = comment.find_parent('li')
        comment.extract()
        if item:
            item['style'] = 'list-style-type: none'
    labels = {}
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        table_match = re.fullmatch(r'\s*table:\s*(\{.*\})\s*', str(comment))
        if table_match:
            settings = json.loads(table_match[1])
            table = comment.find_next('table')
            if table is None:
                raise ValueError('Table metadata must precede a table.')
            complex_table = table.find(attrs={'rowspan': True}) or table.find(attrs={'colspan': True})
            widths = settings.get('widths', [])
            if widths:
                if not all(isinstance(w, (int, float)) and math.isfinite(w) and w > 0 for w in widths):
                    raise ValueError('Table widths must be positive numbers, or [] for automatic layout.')
                if not complex_table and table.thead and len(widths) != len(table.thead.find_all('th')):
                    raise ValueError('Provide one width per table column, or [] for automatic layout.')
                for old_group in table.find_all('colgroup', recursive=False):
                    old_group.decompose()
                colgroup = soup.new_tag('colgroup')
                for width in widths:
                    colgroup.append(soup.new_tag('col', style=f'width: {100 * width / sum(widths):.3f}%'))
                if table.caption:
                    table.caption.insert_after(colgroup)
                else:
                    table.insert(0, colgroup)
                table['class'] = table.get('class', []) + ['source-widths']
            # Explicit complex-table markup is left as authored by the reviewer.
            if not complex_table:
                for header in table.select('thead th'):
                    header['scope'] = 'col'
                if settings.get('row_header') == 1:
                    for row in table.select('tbody tr'):
                        cell = row.find(['td', 'th'])
                        if cell and cell.get_text(strip=True):
                            cell.name = 'th'
                            cell['scope'] = 'row'
        match = re.fullmatch(r'\s*description:\s*(desc-\d+)\s*', str(comment))
        if match:
            img = comment.find_next('img')
            ident = match[1]
            if img is None or ident in description_ids or id(img) in labels:
                raise ValueError(f'Duplicate or misplaced description ID: {ident}')
            labels[id(img)] = ident
            description_ids.add(ident)
    for img in soup.find_all('img'):
        ident = labels.get(id(img))
        if not ident:
            raise ValueError('Add <!-- description: desc-NNN --> immediately before each image in content.md.')
        source = (folder / unquote(img.get('src', ''))).resolve()
        if not source.is_relative_to((folder / 'assets').resolve()) or not source.is_file():
            raise ValueError(f'Image must exist inside this document\'s assets/: {img.get("src")}')
        img['src'] = source.as_uri()
    return str(soup)


def preview(pdf, markdown, chrome, inspect=False):
    source = markdown.read_text(encoding='utf-8')
    digest = re.search(r'SHA-256: ([a-f0-9]{64})', source)
    if digest and digest[1] != hashlib.sha256(pdf.read_bytes()).hexdigest():
        raise ValueError('This PDF does not match the source recorded in the Markdown.')
    parts = re.split(r'<!-- Source page (\d+) -->', source)
    page_indents = {int(match[2]): len(match[1]) for match in
                    re.finditer(r'(?m)^( *)<!-- Source page (\d+) -->', source)}
    css = document_css(source)
    review = markdown.parent / 'review'
    with pymupdf.open(pdf) as document, tempfile.TemporaryDirectory(prefix='access-preview-') as tmp:
        if [int(n) for n in parts[1::2]] != list(range(1, len(document) + 1)):
            raise ValueError('Keep one <!-- Source page N --> marker per PDF page, in order.')
        work = Path(tmp)
        description_ids = set()
        bodies = []
        browser = [chrome, '--headless', '--disable-gpu', '--hide-scrollbars',
                   '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
                   '--force-device-scale-factor=1', f'--user-data-dir={work / "chrome"}',
                   '--virtual-time-budget=1000']

        def run_browser(*args):
            result = subprocess.run([*browser, *args], capture_output=True, text=True, timeout=45)
            if result.returncode:
                raise RuntimeError(f'Chrome could not render the preview: {result.stderr[-1200:]}')
            return result.stdout

        for index, page in enumerate(document):
            number = index + 1
            width, height = math.ceil(page.rect.width * 1.5), math.ceil(page.rect.height * 1.5)
            body = render_body(parts[2 * number], markdown.parent, description_ids,
                               continuation_indent=page_indents.get(number, 0))
            bodies.append(f'<section aria-label="Source page {number}">{body}</section>')
            html = work / f'p{number:03d}.html'
            html.write_text(f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src file: data:; style-src 'unsafe-inline'; script-src 'nonce-preview-measure'">
<title>Source page {number} — revised</title><style>{css}
body {{ width: {width}px; }} main {{ min-height: {height}px; }}</style></head>
<body><main>{body}</main>
<script nonce="preview-measure">window.addEventListener('load', () => {{
 document.documentElement.dataset.previewHeight = Math.ceil(document.querySelector('main').getBoundingClientRect().height);
 document.documentElement.dataset.brokenImages = [...document.images].filter(i => !i.naturalWidth).length;
}});</script></body></html>''', encoding='utf-8')
            measured = run_browser(f'--window-size={width},{height}', '--dump-dom', html.as_uri())
            dom = BeautifulSoup(measured, 'html.parser')
            if not dom.html or dom.html.get('data-broken-images') != '0':
                raise RuntimeError(f'Page {number}: image loading or layout measurement failed.')
            full_height = max(height, int(dom.html['data-preview-height']))
            revised = work / f'p{number:03d}-revised.png'
            # Chrome's window can include browser chrome; leave extra room and
            # crop only the known blank space below the measured document.
            run_browser(f'--window-size={width},{full_height + 150}',
                        f'--screenshot={revised}', html.as_uri())
            pix = pymupdf.Pixmap(str(revised))
            if pix.width != width or pix.height < full_height:
                raise RuntimeError(f'Page {number}: screenshot would clip content.')
            cropped = pymupdf.Pixmap(pix.colorspace, width, full_height,
                                     pix.samples[:pix.stride * full_height], pix.alpha)
            cropped.save(revised)
            page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(work / f'p{number:03d}-original.png')
            print(f'Page {number}/{len(document)} rendered.', flush=True)
        if markdown.read_text(encoding='utf-8') != source:
            raise ValueError('Markdown changed during rendering; rerun preview.')
        review.mkdir(exist_ok=True)
        for png in work.glob('p*-*.png'):
            shutil.copyfile(png, review / png.name)
        inspection = markdown.parent / 'preview.html'
        content = BeautifulSoup(''.join(bodies), 'html.parser')
        for img in content.find_all('img'):
            asset = Path(unquote(urlsplit(img['src']).path))
            img['src'] = quote(asset.relative_to(markdown.parent).as_posix(), safe='/')
        inspection.write_text('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                              '<meta name="viewport" content="width=device-width, initial-scale=1">'
                              f'<title>{escape(markdown.parent.name)}</title><style>{css}</style></head>'
                              '<body><main>'
                              + str(content) + '</main></body></html>', encoding='utf-8')
        if inspect:
            webbrowser.open(inspection.as_uri())
    print(f'Paired PNGs: {review}\nSaved HTML: {inspection}\nMarkdown unchanged.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('markdown', type=Path)
    parser.add_argument('--inspect', action='store_true', help='Open the saved preview.html in your browser')
    parser.add_argument('--chrome', help='Chrome executable used to generate the preview')
    args = parser.parse_args()
    try:
        preview(args.pdf.resolve(), args.markdown.resolve(), find_chrome(args.chrome), args.inspect)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'Preview failed: {exc}\n')


if __name__ == '__main__':
    main()
