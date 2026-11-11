"""Short review issues with search strings taken from the Markdown draft."""
import ast
from html import escape as html_escape
import os
import re
from urllib.parse import quote

from .text import escape, format_text


def review_issues(notes, markdown, number):
    """Keep actionable uncertainties; omit routine instructions and repairs."""
    issues = []
    preserved_images = {}

    def add(reason, candidates=()):
        checks = []
        for candidate in candidates:
            for text in (candidate, format_text(candidate)):
                if text and text in markdown:
                    checks.append(text)
                    break
        issues.append((reason, tuple(dict.fromkeys(checks)) or (f'source-page{number}',)))

    def image_issue(reason, images, priority):
        if not images:
            add(reason)
        for path in images:
            if path not in preserved_images or priority > preserved_images[path][0]:
                preserved_images[path] = (priority, reason)

    for item in notes:
        note, scope = item if isinstance(item, tuple) else (item, markdown)
        headers = re.findall(r'^(\|[^\n]+\|)\n\|[- :|]+\|', scope, re.M)
        table_checks = [cell.strip() for header in headers
                        for cell in re.split(r'(?<!\\)\|', header)[1:-1] if cell.strip()][:2]
        images = re.findall(r'!\[[^\n]*?\]\(([^\n]+?)\)', scope)
        if note.startswith(('Corrected ', 'Possible text loss')):
            continue
        if note.startswith('Suspicious font/character mappings remain unverified'):
            words = re.findall(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", note.split('retained: ', 1)[1].split('. Check against', 1)[0])
            add('Spelling: Suspect characters', [ast.literal_eval(word) for word in words])
        elif note.startswith('Suspicious character mappings'):
            add('Spelling: Unverified characters')
        elif note.startswith('Unrecognized characters'):
            add('Spelling: Unrecognized characters', re.findall(r'\S*\ufffd\S*', markdown))
        elif note.startswith('Possible intentional line breaks'):
            match = re.search(r'after "(.*?)";', note)
            add('Text: Possible line break', [match[1]] if match else [])
        elif note.startswith('Possible columns'):
            add('Layout: Reading order uncertain')
        elif note.startswith('Rotated or cropped page'):
            add('Table: Position uncertain')
        elif note.startswith('Table extraction failed'):
            add('Table: Extraction failed')
        elif note.startswith('Uncertain page layout'):
            add('Layout: Uncertain')
        elif note.startswith('Likely scanned/image-only'):
            image_issue('Text: Image only', images, 1)
        elif note.startswith('Little or no extractable text'):
            if images:
                image_issue('Image: Description needed', images, 0)
            else:
                add('Text: Little or no extractable text')
        elif note.startswith(('Image preserved', 'Vector graphics preserved', 'Whole-page SVG uses')):
            image_issue('Image: Description needed', images, 0)
        elif note.startswith('Link with unsupported scheme'):
            add('Link: Unsupported address', [note.split('text: ', 1)[1]])
        elif note.startswith('Internal PDF link'):
            add('Link: Internal destination missing')
        elif note.startswith('A link was retained separately'):
            add('Link: Placement uncertain', re.findall(r'(?<!!)\[[^\n]*?\]\(([^\n]+?)\)', scope))

        # Successful normalization is not an issue. Generated labels/captions
        # remain reviewable because they introduce text absent from the source.
        if 'Empty header: positional' in note:
            add('Table: Generated column labels', table_checks)
        if 'Ambiguous cell spans' in note:
            image_issue('Table: Preserved as image', images, 2)
        if 'Caption inferred' in note:
            add('Table: Caption inferred', re.findall(r'^: (.+)$', scope, re.M))
    for reason in dict.fromkeys(reason for _, reason in preserved_images.values()):
        add(reason, [path for path, (_, current) in preserved_images.items() if current == reason])
    return list(dict.fromkeys(issues))


def code_check(text):
    if '|' in text:
        return '<code>' + html_escape(text).replace('|', '&#124;') + '</code>'
    delimiter = '`' * (max((len(run) for run in re.findall(r'`+', text)), default=0) + 1)
    return f'{delimiter} {text} {delimiter}' if '`' in text else f'{delimiter}{text}{delimiter}'


def format_review_log(pdf, markdown, page_count, page_notes, document_notes=(), page_svgs=None):
    review = markdown.parent / 'review'
    source_link = quote(os.path.relpath(pdf.resolve(), review.resolve()), safe='/')
    content_link = quote(os.path.relpath(markdown.resolve(), review.resolve()), safe='/')
    lines = [f'# {escape(pdf.stem)}', '',
             '| Original | Revision |', '| --- | --- |',
             f'| [PDF]({source_link}) | [MD]({content_link}) |', '',
             '[index.HTML](../index.html)', '']
    if page_svgs:
        lines.extend(['SVG comparisons: ' + ' · '.join(
            f'Page {number} [SVG](../{path})' for number, path in sorted(page_svgs.items())), ''])
    if document_notes:
        lines.extend(['## Document review', '', *['- ' + escape(note) for note in document_notes], ''])
    lines.extend([f'## {page_count} pages, {len(page_notes)} flagged for review:', '',
                  '| Page | Possible Issue | Check |', '| --- | --- | --- |'])
    for number, issues in sorted(page_notes.items()):
        for reason, checks in issues:
            lines.append(f'| **{number}** | {escape(reason)} | {"<br>".join(code_check(text) for text in checks)} |')
    return '\n'.join(lines) + '\n'
