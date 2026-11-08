#!/usr/bin/env python3
"""Save HTML and PDF-page or LaTeX-document PNG previews; never edit Markdown."""
import argparse
from contextlib import nullcontext
import hashlib
from html import escape
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup, Comment
import pymupdf

from workflow import ROOT, DOWNLOADS, PENDING, document_markdown, html_name, next_command


def latex_sources(source):
    """Read and validate the separate LaTeX and optional comparison-PDF records."""
    match = re.search(r'^<!-- pdf2md-latex: (.+) -->$', source, flags=re.M)
    if match is None:
        return None
    records = json.loads(match[1])
    if not isinstance(records, dict) or 'tex' not in records:
        raise ValueError('Invalid LaTeX source record.')
    for kind, record in records.items():
        if kind not in ('tex', 'pdf') or not isinstance(record, dict):
            raise ValueError('Invalid LaTeX source record.')
        path = Path(record.get('path', ''))
        if not path.is_absolute() or path.suffix.lower() != '.' + kind or not path.is_file():
            raise ValueError(f'Original {kind.upper()} not found: {path}')
        if hashlib.sha256(path.read_bytes()).hexdigest() != record.get('sha256'):
            raise ValueError(f'Original {kind.upper()} changed since conversion: {path}')
        if kind == 'pdf':
            validate_source(path, record['sha256'])
    return records


def validate_source(pdf, digest):
    if pdf.suffix.lower() != '.pdf' or not pdf.is_file():
        raise ValueError(f'Choose an existing PDF file: {pdf}')
    if digest:
        with pdf.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise ValueError('This PDF does not match the source recorded in the Markdown.')
    with pymupdf.open(pdf) as document:
        if not document.is_pdf:
            raise ValueError(f'Not a PDF file: {pdf}')
    return pdf


def resolve_source(markdown, root=None):
    """Validate recorded LaTeX sources or locate the original PDF before rendering."""
    root = (root or ROOT).resolve()
    markdown = markdown.resolve()
    source = markdown.read_text(encoding='utf-8')
    records = latex_sources(source)
    if records is not None:
        return Path(records['tex']['path'])
    match = re.search(r'SHA-256: ([a-f0-9]{64})', source)
    digest = match[1] if match else None
    pending = root / PENDING
    name = markdown.parent.name

    def select(paths):
        matches = []
        for path in sorted(paths):
            if path.stem != name or path.suffix.lower() != '.pdf':
                continue
            try:
                matches.append(validate_source(path, digest))
            except (OSError, ValueError, RuntimeError):
                continue
        # Identical hashes identify the same source even if copies exist.
        if matches and (digest or len(matches) == 1):
            return matches[0]
        return None

    if markdown.parent.is_relative_to(pending) and markdown.parent != pending:
        relative = markdown.parent.relative_to(pending)
        directory = root / DOWNLOADS / relative.parent
        if directory.is_dir():
            pdf = select(directory.iterdir())
            if pdf is not None:
                return pdf
    pdf = select(pending.rglob('*'))
    if pdf is not None:
        return pdf
    if not sys.stdin.isatty():
        raise ValueError('Original PDF could not be identified automatically. '
                         'Run dfx review in an interactive terminal to enter its path or cancel.')
    value = input('Original PDF not found or could not be identified.\n'
                  'Enter its path, or press Enter to cancel: ').strip()
    if not value:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '\"\'':
        value = value[1:-1]
    return validate_source(Path(value).expanduser().resolve(), digest)


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


def render_blank_rules(soup):
    """Turn blank markers into block rules, preserving their surrounding text."""
    inline_parents = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'span', 'a',
                      'em', 'strong', 'mark', 'b', 'i', 'u', 's', 'del', 'ins',
                      'small', 'sub', 'sup'}
    for blank in soup.select('span.blank'):
        if blank.get_text() != '---':
            continue
        rule = soup.new_tag('hr', attrs={'class': 'blank'})
        blank.replace_with(rule)
        # A rule cannot be a child of a paragraph or phrasing element. Split
        # those wrappers around it, stopping at a list item, cell, or other block.
        while rule.parent.name in inline_parents:
            parent = rule.parent
            after = soup.new_tag(parent.name, attrs=dict(parent.attrs))
            for sibling in list(rule.next_siblings):
                after.append(sibling.extract())
            parent.insert_after(rule.extract())
            before_has_content = bool(parent.get_text(strip=True) or parent.find(True))
            if after.get_text(strip=True) or after.find(True):
                if before_has_content:
                    after.attrs.pop('id', None)
                rule.insert_after(after)
            if not before_has_content:
                parent.decompose()


def render_inline_headings(soup):
    """Promote marked paragraphs' leading bold text without nesting h4 in p."""
    block_tags = {'p', 'div', 'ul', 'ol', 'li', 'table', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'img'}
    for comment in list(soup.find_all(string=lambda s: isinstance(s, Comment))):
        match = re.fullmatch(r'\s*pdf2md-inline-heading: (\d+)\s*', str(comment))
        if not match:
            continue
        container = comment.parent
        comment.extract()
        if container.name not in ('p', 'li'):
            continue
        text_nodes, has_block = [], False
        for node in container.descendants:
            if isinstance(node, Comment):
                continue
            if getattr(node, 'name', None) in block_tags:
                has_block = True
                break
            if isinstance(node, str):
                text_nodes.append(node)
        prefix, last_bold = [], None
        for node in text_nodes:
            if node.strip() and node.find_parent('strong') is None:
                break
            prefix.append(str(node))
            if node.strip():
                last_bold = node
        if last_bold is None:
            continue
        all_bold = not has_block and all(not node.strip() or node.find_parent('strong')
                                         for node in text_nodes)
        if all_bold:
            qualifies = int(match[1]) == 1
        else:
            qualifies = 1 <= len(''.join(prefix).split()) <= 10
        if not qualifies:
            continue
        boundary = soup.new_tag('span')
        last_bold.insert_after(boundary)
        # Split any inline wrappers at the end of the heading, keeping their
        # formatting and links on both sides of the split.
        while boundary.parent is not container:
            parent = boundary.parent
            after = soup.new_tag(parent.name, attrs=dict(parent.attrs))
            for sibling in list(boundary.next_siblings):
                after.append(sibling.extract())
            parent.insert_after(boundary.extract())
            if after.contents:
                after.attrs.pop('id', None)
                boundary.insert_after(after)
        heading = soup.new_tag('h4', attrs={'class': 'inline-heading'})
        for sibling in reversed(list(boundary.previous_siblings)):
            heading.append(sibling.extract())
        boundary.replace_with(heading)
        if container.name == 'p':
            container.name = 'div'
            container['class'] = container.get('class', []) + ['inline-heading-paragraph']


def render_body(markdown, folder, description_ids, continuation_indent=0):
    # Source paths are review bookkeeping, not part of the published HTML.
    markdown = re.sub(r'^<!-- pdf2md-latex: .+ -->\s*', '', markdown, flags=re.M)
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
    result = subprocess.run(['pandoc', '--from=markdown+raw_html+fancy_lists+startnum-smart-implicit_figures', '--to=html5', '--mathml'],
                            input=markdown, capture_output=True, text=True, check=True)
    if 'Could not convert TeX math' in result.stderr:
        raise ValueError('Pandoc could not render the Markdown cleanly: ' + result.stderr.strip())
    soup = BeautifulSoup(result.stdout, 'html.parser')
    for heading in list(soup.select('h4.inline-heading')):
        if 'inline-heading-paragraph' in heading.parent.get('class', []):
            continue
        following = heading.find_next_sibling()
        wrapper = soup.new_tag('div', attrs={'class': 'inline-heading-paragraph'})
        heading.wrap(wrapper)
        if ('inline-heading-prefix' in heading.get('class', [])
                and following is not None and following.name == 'p'):
            wrapper.append(' ')
            wrapper.append(following.extract())
    render_inline_headings(soup)
    render_blank_rules(soup)
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


def document_title(name):
    """Make filename-based titles readable while preserving uppercase acronyms."""
    return ' '.join(word if word.isupper() else word[:1].upper() + word[1:]
                    for word in name.replace('_', ' ').split())


def ensure_document_heading(bodies, title):
    """Add one fallback h1 only when no page already contains one."""
    if bodies and not any(BeautifulSoup(body, 'html.parser').find('h1') for body in bodies):
        return [f'<h1>{escape(title)}</h1>\n' + bodies[0], *bodies[1:]]
    return bodies


def preview(pdf, markdown, chrome, inspect=False):
    source = markdown.read_text(encoding='utf-8')
    records = latex_sources(source)
    latex = records is not None
    if latex and Path(records['tex']['path']) != pdf.resolve():
        raise ValueError('LaTeX source does not match the Markdown source record.')
    digest = re.search(r'SHA-256: ([a-f0-9]{64})', source)
    if not latex and digest and digest[1] != hashlib.sha256(pdf.read_bytes()).hexdigest():
        raise ValueError('This PDF does not match the source recorded in the Markdown.')
    parts = re.split(r'<!-- Source page (\d+) -->', source)
    page_indents = {int(match[2]): len(match[1]) for match in
                    re.finditer(r'(?m)^( *)<!-- Source page (\d+) -->', source)}
    css = document_css(source)
    review = markdown.parent / 'review'
    inspection = markdown.parent / html_name(markdown.parent)
    external_css = inspection.name == 'index.html'
    title = document_title(markdown.parent.name)
    comparison = Path(records['pdf']['path']) if latex and 'pdf' in records else None
    document_context = (pymupdf.open(comparison or pdf) if not latex or comparison else nullcontext(None))
    with document_context as document, tempfile.TemporaryDirectory(prefix='access-preview-') as tmp:
        if not latex and [int(n) for n in parts[1::2]] != list(range(1, len(document) + 1)):
            raise ValueError('Keep one <!-- Source page N --> marker per PDF page, in order.')
        work = Path(tmp)
        (work / 'main.css').write_text(css, encoding='utf-8')
        description_ids = set()
        page_bodies = ([render_body(source, markdown.parent, description_ids)] if latex else
                      [render_body(parts[2 * number], markdown.parent, description_ids,
                                  continuation_indent=page_indents.get(number, 0))
                       for number in range(1, len(document) + 1)])
        page_bodies = ensure_document_heading(page_bodies, title)
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

        for index, page in enumerate([None] if latex else document):
            number = index + 1
            width, height = ((918, 1188) if latex else
                             (math.ceil(page.rect.width * 1.5), math.ceil(page.rect.height * 1.5)))
            body = page_bodies[index]
            label = 'Converted document' if latex else f'Source page {number}'
            bodies.append(f'<section aria-label="{label}">{body}</section>')
            html = work / f'p{number:03d}.html'
            html.write_text(f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src file: data:; style-src file: 'unsafe-inline'; script-src 'nonce-preview-measure'">
<title>{label} — revised</title><link rel="stylesheet" href="main.css"></head>
<body style="width: {width}px"><main style="min-height: {height}px">{body}</main>
<script nonce="preview-measure">window.addEventListener('load', () => {{
 document.documentElement.dataset.previewHeight = Math.ceil(document.querySelector('main').getBoundingClientRect().height);
 document.documentElement.dataset.brokenImages = [...document.images].filter(i => !i.naturalWidth).length;
}});</script></body></html>''', encoding='utf-8')
            measured = run_browser(f'--window-size={width},{height}', '--dump-dom', html.as_uri())
            dom = BeautifulSoup(measured, 'html.parser')
            if not dom.html or dom.html.get('data-broken-images') != '0':
                raise RuntimeError(f'Page {number}: image loading or layout measurement failed.')
            full_height = max(height, int(dom.html['data-preview-height']))
            if latex and full_height > 30000:
                raise ValueError('Document exceeds the 30,000-pixel PNG preview limit; split the LaTeX document before importing.')
            revised = work / ('document-revised.png' if latex else f'p{number:03d}-revised.png')
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
            if not latex:
                page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(work / f'p{number:03d}-original.png')
            print('Document rendered.' if latex else f'Page {number}/{len(document)} rendered.', flush=True)
        if latex and document is not None:
            for number, page in enumerate(document, 1):
                page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(work / f'p{number:03d}-original.png')
        if latex:
            latex_sources(source)
        if markdown.read_text(encoding='utf-8') != source:
            raise ValueError('Markdown changed during rendering; rerun preview.')
        review.mkdir(exist_ok=True)
        for png in work.glob('*.png'):
            shutil.copyfile(png, review / png.name)
        if external_css:
            (markdown.parent / 'assets').mkdir(exist_ok=True)
            shutil.copyfile(work / 'main.css', markdown.parent / 'assets/main.css')
        stylesheet = '<link rel="stylesheet" href="assets/main.css">' if external_css else f'<style>{css}</style>'
        content = BeautifulSoup(''.join(bodies), 'html.parser')
        for img in content.find_all('img'):
            asset = Path(unquote(urlsplit(img['src']).path))
            img['src'] = quote(asset.relative_to(markdown.parent).as_posix(), safe='/')
        inspection.write_text('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                              '<meta name="viewport" content="width=device-width, initial-scale=1">'
                              f'<title>{escape(title)}</title>{stylesheet}</head>'
                              '<body><main>'
                              + str(content) + '</main></body></html>', encoding='utf-8')
        if inspect:
            webbrowser.open(inspection.as_uri())
    print(f'{"Document and comparison PNGs" if latex else "Paired PNGs"}: {review}\nSaved HTML: {inspection}\nMarkdown unchanged.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('markdown', type=Path, help='Document folder (or Markdown file); original PDF or LaTeX sources are located automatically')
    parser.add_argument('--inspect', action='store_true', help='Open the saved HTML in your browser')
    parser.add_argument('--chrome', help='Chrome executable used to generate the preview')
    args = parser.parse_args()
    markdown = document_markdown(args.markdown).resolve()
    try:
        try:
            pdf = resolve_source(markdown)
        except (KeyboardInterrupt, EOFError):
            pdf = None
        if pdf is None:
            print('Review cancelled. No files changed.')
            return
        preview(pdf, markdown, find_chrome(args.chrome), args.inspect)
        if markdown.parent.is_relative_to(ROOT / PENDING):
            print(f'Next: {next_command("stage", markdown.parent)}')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'Review failed: {exc}\n')


if __name__ == '__main__':
    main()
