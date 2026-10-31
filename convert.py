#!/usr/bin/env python3
"""Create reviewable Markdown drafts from one PDF or a folder; no AI service."""
import argparse
from collections import Counter
import hashlib
from html import escape as html_escape
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from string import Template
import unicodedata
from urllib.parse import quote, urlsplit

import pdfplumber
import pymupdf
from bs4 import BeautifulSoup

from preview import find_chrome, preview
from workflow import DOWNLOADS, PENDING, conversion_destination

COMMAND_TEMPLATES = Path(__file__).resolve().parent / "templates"


def clean(text):
    return ' '.join(re.sub(r'-\s*\n\s*', '-', text or '').split())


def escape(text):
    return re.sub(r'([\\`*_\[\]<>|])', r'\\\1', clean(text))


def infer_document_font(fonts, body_size):
    """Weight body characters over headings; use known names to check font flags."""
    names = {
        'sans': ('arial', 'helvetica', 'calibri', 'aptos', 'verdana', 'tahoma', 'segoe',
                 'roboto', 'opensans', 'sourcesans', 'notosans', 'liberationsans',
                 'dejavusans', 'nimbussans'),
        'serif': ('times', 'georgia', 'cambria', 'garamond', 'palatino', 'baskerville',
                  'bookantiqua', 'liberationserif', 'dejavuserif', 'notoserif',
                  'sourceserif', 'nimbusroman'),
    }
    scores = Counter()
    unknown, conflicts = 0, 0
    for (name, flags, size), characters in fonts.items():
        name = re.sub(r'[^a-z]', '', name.split('+')[-1].lower())
        if not characters or any(token in name for token in ('symbol', 'dingbat', 'wingding')):
            continue
        weight = characters * (4 if abs(size - body_size) <= .5 else 1)
        weight *= 1 if flags & pymupdf.TEXT_FONT_BOLD else 2
        flagged = 'serif' if flags & pymupdf.TEXT_FONT_SERIFED else 'sans'
        named = next((family for family, tokens in names.items()
                      if any(token in name for token in tokens)), None)
        family = named or flagged
        scores[family] += weight
        if named is None:
            unknown += weight
        elif named != flagged:
            conflicts += weight
    total = sum(scores.values())
    if not total:
        return 'sans', 'No usable PDF font data; sans is a temporary fallback. Set font to serif or sans after review.'
    family = max(('sans', 'serif'), key=lambda name: scores[name])
    reasons = []
    if scores[family] / total < .6:
        reasons.append('mixed serif and sans body text')
    if unknown / total > .25:
        reasons.append('unrecognized font names classified using PDF flags')
    if conflicts:
        reasons.append('font names and flags disagree; recognizable names used')
    note = f'Inferred {family}: ' + '; '.join(reasons) + '. Confirm the font setting.' if reasons else ''
    return family, note


def bullet_details(spans):
    """Recognize text bullets, including font-specific Word list markers."""
    raw = ''.join(span['text'] for span in spans)
    match = re.match(r'^\s*([•●▪◦○·\uf0b7o])(?:\s+|$)', raw)
    if not match:
        return None
    first = next(span for span in spans if span['text'].strip())
    font = first['font'].lower()
    if match[1] == '\uf0b7' and 'symbol' not in font:
        return None
    if match[1] == 'o' and not (first['text'].strip() == 'o' and 'courier' in font):
        return None
    chars = [char for span in spans for char in span.get('chars', [])]
    body = next((char for char in chars[match.end():] if char['c'].strip()), None)
    # A separate marker and indented body distinguish a Courier bullet from prose.
    if match[1] == 'o' and (body is None or body['bbox'][0] - first['bbox'][2] < first['size'] * .4):
        return None
    return {'end': match.end(), 'symbol': match[1], 'x': first['bbox'][0],
            'body_x': body['bbox'][0] if body else first['bbox'][0] + first['size'] * 1.5}


def join_bullet_lines(text_blocks):
    """Join separately extracted markers to the body on the same baseline."""
    lines = [line for block in text_blocks for line in block.get('lines', [])]
    joined = set()
    for line in lines:
        spans = [span for span in line['spans'] if span['text'].strip()]
        if len(spans) != 1 or spans[0]['text'].strip() not in '•●▪◦○·\uf0b7o':
            continue
        marker = spans[0]
        if marker['text'].strip() == 'o' and 'courier' not in marker['font'].lower():
            continue
        candidates = [other for other in lines if other is not line and id(other) not in joined
                      and other['spans'] and other['spans'][0]['text'].strip()
                      and abs(other['spans'][0]['origin'][1] - marker['origin'][1]) <= 1
                      and 0 <= other['bbox'][0] - marker['bbox'][2] <= marker['size'] * 2.5]
        if len(candidates) != 1:
            continue
        body = candidates[0]
        marker['text'] += ' '
        marker['chars'].append({'c': ' ', 'origin': marker['origin'], 'bbox': marker['bbox']})
        line['spans'].extend(body['spans'])
        line['bbox'] = tuple(pymupdf.Rect(line['bbox']) | pymupdf.Rect(body['bbox']))
        joined.add(id(body))
    for block in text_blocks:
        if 'lines' in block:
            block['lines'] = [line for line in block['lines'] if id(line) not in joined]


def mark_highlights(page, text_blocks, drawings):
    """Mark characters under inline backgrounds or PDF highlight annotations."""
    traces = page.get_texttrace()
    sequence = {(chr(char[0]), tuple(round(v, 2) for v in char[2])): trace['seqno']
                for trace in traces for char in trace['chars']}
    backgrounds = [drawing for drawing in drawings if len(drawing['items']) == 1
                   and drawing['items'][0][0] == 're' and drawing.get('fill') is not None
                   and not all(channel >= .99 for channel in drawing['fill'])
                   and (drawing.get('fill_opacity') or 0) > 0]
    annotations = [(annotation, [pymupdf.Quad(annotation.vertices[i:i + 4]).rect
                                for i in range(0, len(annotation.vertices or []), 4)])
                   for annotation in page.annots() or []
                   if annotation.type[0] == pymupdf.PDF_ANNOT_HIGHLIGHT]
    for block in text_blocks:
        for line in block.get('lines', []):
            for span in line['spans']:
                flags = []
                for char in span['chars']:
                    rect = pymupdf.Rect(char['bbox'])
                    seqno = sequence.get((char['c'], tuple(round(v, 2) for v in char['origin'])), -1)
                    annotated = any(inside(rect, region) for _, regions in annotations for region in regions)
                    background = any(drawing['seqno'] < seqno
                                     and drawing['rect'].height <= span['size'] * 1.8
                                     and inside(rect, drawing['rect']) for drawing in backgrounds)
                    flags.append(annotated or background)
                span['highlighted'] = flags
    # Highlight annotations have curved ends in their appearance paths. Exclude
    # only those matching paths, not other figures underneath the annotation.
    annotation_drawings = {drawing['seqno'] for drawing in drawings
                           if drawing['type'] == 'f'
                           and [item[0] for item in drawing['items']] == ['c', 'l', 'c']
                           and any(annotation.rect.contains(drawing['rect'])
                                   and any(drawing['rect'].contains(region) for region in regions)
                                   for annotation, regions in annotations)}
    return traces, annotation_drawings


def format_spans(spans, heading=False, bullet=None):
    raw = ''.join(span['text'] for span in spans)
    # Markdown list markers must remain outside emphasis delimiters.
    marker = re.match(r'^\s*(?:\d{1,2}[.)]|[a-z][.)]|[•●▪])(?:\s+|$)', raw)
    skip = bullet['end'] if bullet else marker.end() if marker else 0
    prefix = '- ' if bullet else clean(marker[0]) + ' ' if marker else ''
    runs = []
    for span in spans:
        offset = min(skip, len(span['text']))
        text = span['text'][offset:]
        skip = max(0, skip - len(span['text']))
        if not text:
            continue
        style = span['flags'] & (2 if heading else 18)  # Italic / bold.
        highlights = span.get('highlighted', [False] * len(span['text']))
        for index, char in enumerate(text, offset):
            key = style, highlights[index]
            if runs and runs[-1][0] == key:
                runs[-1][1] += char
            else:
                runs.append([key, char])
    result = [prefix]
    for (style, highlighted), text in runs:
        if not text.strip():
            result.append(' ')
            continue
        marks = ('**' if style & 16 else '') + ('*' if style & 2 else '')
        formatted = marks + escape(text) + marks
        if highlighted:
            formatted = '<mark>' + formatted + '</mark>'
        result.append((' ' if text[0].isspace() else '') + formatted
                      + (' ' if text[-1].isspace() else ''))
    return clean(''.join(result))


def join_blocks(blocks):
    """Keep consecutive simple list items tight, with other blocks separated."""
    result, previous = [], None
    for block in blocks:
        match = re.match(r'^(?:(\d{1,2})([.)])|([a-z])([.)])|(-))\s+', block.lstrip(' '))
        kind = (('number', match[2]) if match and match[1] else
                ('letter', match[4]) if match and match[3] else
                ('bullet', '-') if match else None)
        if result:
            result.append('\n' if kind and kind == previous else '\n\n')
        result.append(block)
        previous = kind
    return ''.join(result)


def infer_line_breaks(events):
    """Preserve clear form/entry breaks; leave likely wraps and uncertain runs flowing."""
    breaks, notes = set(), []
    text = [value for _, _, kind, value in events if kind == 'text']
    if not text:
        return breaks, notes
    right_edge = max(value['bbox'][2] for value in text)

    def follows(before, after):
        size = max(before['size'], after['size'])
        return (size * .4 < after['bbox'][1] - before['bbox'][1] < size * 2.2
                and after['bbox'][1] - before['bbox'][3] <= max(4, size * .8)
                and abs(before['size'] - after['size']) <= 1)

    def field(value):
        return bool(re.search(r'_{2,}\s*$', value['text']))

    for index, (_, _, kind, value) in enumerate(events):
        if kind != 'text' or index == 0 or events[index - 1][2] != 'text':
            continue
        prior = events[index - 1][3]
        if not follows(prior, value):
            continue
        # A completed answer field is strong evidence of a separate entry.
        if field(prior) and abs(value['bbox'][0] - prior['bbox'][0]) <= 3:
            breaks.add(index)
        if not prior['text'].endswith(':') or value['bbox'][0] < prior['bbox'][0] - 3:
            continue
        run = [(index, value)]
        for offset in range(index + 1, len(events)):
            _, _, next_kind, following = events[offset]
            if (next_kind != 'text' or not follows(run[-1][1], following)
                    or abs(following['bbox'][0] - value['bbox'][0]) > 3):
                break
            run.append((offset, following))
        short = all(row['bbox'][2] < right_edge - max(24, 2 * row['size']) for _, row in run)
        starts = [row['text'].lstrip('“\"(')[:1] for _, row in run]
        independent = all(start.isupper() or start.isdigit() for start in starts)
        hyphenated = any(row['text'].endswith(('-', '–')) for _, row in run[:-1])
        if (all(field(row) for _, row in run)
                or (len(run) >= 2 and short and independent and not hyphenated)):
            breaks.update(offset for offset, _ in run)
        elif short and not hyphenated:
            notes.append(f'Possible intentional line breaks after "{prior["text"][:70]}"; '
                         'text kept flowing. Check against the original page.')
    return breaks, notes


def inside(box, region):
    x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return region[0] <= x <= region[2] and region[1] <= y <= region[3]


def is_underline(box, text_blocks):
    if box.height > 2.5 or box.width < 3:
        return False
    for block in text_blocks:
        for line in block.get('lines', []):
            if (line['bbox'][0] - 2 <= box.x0 and box.x1 <= line['bbox'][2] + 2
                    and any(-1 <= box.y0 - span['origin'][1] <= 3
                            for span in line['spans'])):
                return True
    return False


def is_decoration(drawing, text_blocks, text_traces=()):
    """Recognize rules, text borders, and colored backgrounds behind text."""
    items = drawing['items']
    if len(items) != 1:
        return False
    box = pymupdf.Rect(drawing['rect'])
    kind = items[0][0]
    width = drawing.get('width') or 0
    if kind == 'l':
        return width <= 4 and (box.width == 0 or box.height == 0)
    if kind != 're' or width > 3:
        return False
    if min(box.width, box.height) <= 1:
        return True
    fill = drawing.get('fill')
    if (6 <= box.width <= 24 and 6 <= box.height <= 24 and .75 <= box.width / box.height <= 1.35
            and (fill is None or all(channel >= .99 for channel in fill))):
        # Unresolved checkbox-like shapes must not disappear as text borders.
        return False
    if fill is not None and not all(channel >= .99 for channel in fill):
        # A highlight can cover only part of a span, and font boxes can extend
        # beyond the painted letters. Require later text over the background.
        padded = box + (-1, -1, 1, 1)
        return any(trace['seqno'] > drawing['seqno'] and sum(
            not chr(char[0]).isspace() and padded.contains(pymupdf.Rect(char[3]))
            for char in trace['chars']) >= 2 for trace in text_traces)
    return any(box.contains(pymupdf.Rect(span['bbox'])) and span['text'].strip()
               for block in text_blocks for line in block.get('lines', [])
               for span in line['spans'])


def extract_checkboxes(drawings, text_blocks, tables, images):
    """Recognize outlined boxes with separate, unambiguous text labels/marks."""
    lines = [line for block in text_blocks for line in block.get('lines', [])]
    events, consumed_spans, consumed_drawings, consumed_tables = [], set(), set(), set()
    marks = {'x', 'X', '✓', '✔'}
    for drawing in drawings:
        box = pymupdf.Rect(drawing['rect'])
        fill = drawing.get('fill')
        if (len(drawing['items']) != 1 or drawing['items'][0][0] != 're'
                or drawing.get('color') is None
                or not (6 <= box.width <= 24 and 6 <= box.height <= 24)
                or not .75 <= box.width / box.height <= 1.35
                or (fill is not None and not all(channel >= .99 for channel in fill))):
            continue
        # Only replace a table when it is the checkbox itself, never a cell
        # inside a larger table. Filled boxes and non-text marks stay for review.
        touching = [table for table in tables if box.intersects(pymupdf.Rect(table.bbox))]
        if any(len(table.cells) != 1 or not (box + (-2, -2, 2, 2)).contains(table.bbox)
               for table in touching):
            continue
        if any(box.intersects(pymupdf.Rect(info['bbox'])) for info in images):
            continue
        if any(other is not drawing and box.intersects(pymupdf.Rect(other['rect']))
               for other in drawings):
            continue
        enclosed = [span for line in lines for span in line['spans']
                    if span['text'].strip() and inside(span['bbox'], box)]
        if any(span['text'].strip() not in marks for span in enclosed):
            continue
        candidates = []
        for line in lines:
            spans = [span for span in line['spans'] if span['text'].strip()]
            label = clean(''.join(span['text'] for span in line['spans']))
            rect = pymupdf.Rect(line['bbox'])
            if (not spans or label in marks or len(label) > 60 or len(label.split()) > 6
                    or any(id(span) in consumed_spans for span in spans)
                    or any(inside(rect, table.bbox) for table in tables)):
                continue
            overlap = min(box.y1, rect.y1) - max(box.y0, rect.y0)
            if overlap < .35 * min(box.height, rect.height):
                continue
            gap = rect.x0 - box.x1 if rect.x0 >= box.x1 - 2 else box.x0 - rect.x1
            if 0 <= gap <= max(40, box.width * 3):
                candidates.append((gap, spans, label))
        candidates.sort(key=lambda candidate: candidate[0])
        if not candidates or (len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 3):
            continue
        _, spans, label = candidates[0]
        consumed_spans.update(id(span) for span in [*enclosed, *spans])
        consumed_drawings.add(drawing['seqno'])
        consumed_tables.update(id(table) for table in touching)
        events.append((box.y0, box.x0, 'checkbox', f'- [{"x" if enclosed else " "}] {escape(label)}'))
    # Slightly misaligned boxes on one row still read from left to right.
    row_y = None
    events.sort(key=lambda event: event[:2])
    for index, (y, x, kind, text) in enumerate(events):
        if row_y is None or y - row_y > 3:
            row_y = y
        events[index] = (row_y, x, kind, text)
    return events, consumed_spans, consumed_drawings, consumed_tables


def words(text):
    text = re.sub(r'</?mark>', '', text)
    text = re.sub(r'!?\[([^\]]*)\]\([^\n]*?\)', r'\1', text)
    return Counter(re.findall(r'\w+', unicodedata.normalize('NFKC', clean(text)).casefold()))


def image(page, assets, name, box=None):
    assets.mkdir(parents=True, exist_ok=True)
    page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), clip=box).save(assets / name)
    return f'{assets.name}/{name}'


def simplify_header_padding(rows, geometry):
    """Collapse header padding only when every body row defines the same columns."""
    if len(rows) < 2:
        return None
    body = [[(value, cell) for value, cell in zip(row, positions.cells) if cell is not None]
            for row, positions in zip(rows[1:], geometry[1:])]
    columns = [(cell[0], cell[2]) for _, cell in body[0]]
    if not columns or len(columns) >= len(rows[0]):
        return None
    for row, positions in zip(body, geometry[1:]):
        if len(row) != len(columns) or any(
                abs(cell[0] - left) > .1 or abs(cell[2] - right) > .1
                or abs(cell[1] - positions.bbox[1]) > .1
                or abs(cell[3] - positions.bbox[3]) > .1
                for (_, cell), (left, right) in zip(row, columns)):
            return None
    groups = [[] for _ in columns]
    for value, cell in zip(rows[0], geometry[0].cells):
        if cell is None:
            continue
        matches = [i for i, (left, right) in enumerate(columns)
                   if left - .1 <= cell[0] and cell[2] <= right + .1]
        if len(matches) != 1:
            return None
        if clean(value):
            groups[matches[0]].append(value)
    if not all(len(group) == 1 for group in groups):
        return None
    return ([[group[0] for group in groups]] + [[value for value, _ in row] for row in body],
            [left for left, _ in columns] + [columns[-1][1]])


def grid_or_html(html):
    """Use a grid only if Pandoc preserves cell contents, roles, spans and caption."""
    def signature(markup):
        table = BeautifulSoup(markup, 'html.parser').find('table')
        if table is None:
            return None
        return (clean(table.caption.get_text(' ')) if table.caption else '',
                [[(cell.name, int(cell.get('rowspan', 1)), int(cell.get('colspan', 1)),
                   clean(cell.get_text(' '))) for cell in row.find_all(['td', 'th'], recursive=False)]
                 for row in table.find_all('tr')])

    try:
        grid = subprocess.run(
            ['pandoc', '--from=html', '--to=markdown-pipe_tables-simple_tables-multiline_tables-smart',
             '--wrap=none'], input=html, capture_output=True, text=True, check=True, timeout=15).stdout.strip()
        if grid.startswith('+'):
            restored = subprocess.run(
                ['pandoc', '--from=markdown-smart', '--to=html5'], input=grid,
                capture_output=True, text=True, check=True, timeout=15).stdout
            if signature(html) == signature(restored):
                return grid, 'grid'
    except (OSError, subprocess.SubprocessError):
        pass
    return html, 'HTML'


def make_table(table, previous, page_number, page):
    rows = table.extract()
    caption = ''
    # A merged title row is a caption, not part of the column headings.
    if len(rows) > 1 and sum(bool(cell) for cell in rows[0]) == 1 and None in rows[0]:
        caption = clean(next(cell for cell in rows.pop(0) if cell))
    geometry = table.rows[-len(rows):]
    simplified = simplify_header_padding(rows, geometry)
    if simplified:
        rows, simple_edges = simplified
    columns = len(rows[0])
    first = page.get_text('dict', clip=pymupdf.Rect(geometry[0].bbox))
    spans = [s for b in first['blocks'] for line in b.get('lines', [])
             for s in line['spans'] if s['text'].strip()]
    header_bold = sum(len(s['text']) for s in spans if s['flags'] & 16) > sum(len(s['text']) for s in spans) / 2
    has_header = bool(simplified or header_bold or (caption and all(clean(cell) for cell in rows[0])))
    # Only use widths when complete rows agree on the column boundaries.
    grids = [[cell[0] for cell in row.cells] + [row.cells[-1][2]]
             for row in geometry if all(cell is not None for cell in row.cells)]
    if simplified:
        grids = [simple_edges]
    widths = []
    if grids and all(len(grid) == columns + 1 and
                     all(abs(a - b) < 1 for a, b in zip(grid, grids[0])) for grid in grids):
        edges = grids[0]
        widths = [round(100 * (b - a) / (edges[-1] - edges[0]), 2)
                  for a, b in zip(edges, edges[1:])]
    if any(None in row for row in rows):
        # Pipe tables cannot express spans. Try a lossless grid representation.
        if not caption and has_header:
            caption = 'Table: ' + next(clean(cell) for cell in rows[0] if clean(cell))
        xs = sorted({cell[0] for cell in table.cells} | {table.bbox[2]})
        ys = sorted({cell[1] for cell in table.cells} | {table.bbox[3]})
        metadata = '<!-- table: ' + json.dumps({'widths': widths, 'row_header': 0}) + ' -->'
        lines = ['<table>']
        if caption:
            lines.append(f'<caption>{html_escape(caption)}</caption>')
        for index, (row, positions) in enumerate(zip(rows, geometry)):
            if index == 0:
                lines.append('<thead>' if has_header else '<tbody>')
            elif index == 1 and has_header:
                lines.append('</thead><tbody>')
            cells = []
            for value, cell in zip(row, positions.cells):
                if cell is None:
                    continue
                cols = sum(cell[0] - .1 <= x < cell[2] - .1 for x in xs)
                spans = sum(cell[1] - .1 <= y < cell[3] - .1 for y in ys)
                attrs = (f' colspan="{cols}"' if cols > 1 else '')
                attrs += f' rowspan="{spans}"' if spans > 1 else ''
                tag = 'th' if index == 0 and has_header else 'td'
                cells.append(f'<{tag}{attrs}>{html_escape(clean(value))}</{tag}>')
            lines.append('<tr>' + ''.join(cells) + '</tr>')
        lines.append('</thead>' if has_header and len(rows) == 1 else '</tbody>')
        lines.append('</table>')
        text, form = grid_or_html('\n'.join(lines))
        note = f'Merged or split cells: retained as a {form} table. Check inferred headings and header associations.'
        if form == 'HTML':
            note += ' Grid conversion could not be verified without loss; HTML retained.'
        return metadata + '\n\n' + text, note, None
    continuation = (previous and previous['page'] == page_number - 1
                    and len(previous['header']) == columns and not caption and not header_bold
                    and abs(previous['bbox'][0] - table.bbox[0]) < 8
                    and abs(previous['bbox'][2] - table.bbox[2]) < 8)
    if continuation:
        header = previous['header']
        note = 'Possible continued table: column headings reused from the preceding page; confirm the association.'
    elif has_header:
        header = rows.pop(0)
        note = 'Table structure and column headings inferred; check cell associations.'
    else:
        header = [f'Column {i + 1}' for i in range(columns)]
        note = 'Column headings uncertain: generic headings added; all extracted rows retained.'
    if not caption and (has_header or continuation):
        caption = (previous.get('caption', '') if continuation else '') or 'Table: ' + next(
            clean(cell) for cell in header if clean(cell))
    labels = [clean(row[0]) for row in rows if any(clean(cell) for cell in row)]
    row_header = int(bool(labels) and all(labels) and len(set(labels)) == len(labels)
                     and not header[0].startswith('Column '))
    if row_header:
        note += ' First column inferred as row headers; confirm or set row_header to 0.'
    if not widths:
        note += ' Column boundaries uncertain; automatic layout used.'
    render = lambda row: '| ' + ' | '.join(escape(cell) for cell in row) + ' |'
    text = '<!-- table: ' + json.dumps({'widths': widths, 'row_header': row_header}) + ' -->\n\n'
    if caption:
        text += ': ' + escape(caption) + '\n\n'
    text += '\n'.join([render(header), '| ' + ' | '.join(['---'] * columns) + ' |',
                       *[render(row) for row in rows]])
    return text, note, {'header': header, 'bbox': table.bbox, 'page': page_number, 'caption': caption}


def review_commands(pdf, markdown, input_root=None, output_root=None, platform='mac'):
    """Fill the approved command guide with document paths and inventory URLs."""
    root = Path(__file__).resolve().parent
    pdf, markdown = pdf.resolve(), markdown.resolve()
    windows = platform == 'win'

    def argument(value):
        if not windows:
            for char in ('\\', '"', '$', '`'):
                value = value.replace(char, '\\' + char)
        return '"' + value + '"'

    def path_arg(path):
        try:
            relative = os.path.relpath(path, root)
        except ValueError:  # A different Windows drive cannot be relative.
            relative = str(path)
        relative = relative.replace(os.sep, '/')
        return argument(relative.replace('/', '\\') if windows else relative)

    def table(rows):
        lines = ['| Purpose | Command | Notes |', '| --- | --- | --- |']
        for purpose, command, note in rows:
            # Code spans retain shell syntax; longer delimiters handle backticks.
            ticks = '`' * (max((len(run) for run in re.findall(r'`+', command)), default=0) + 1)
            code = command.replace('|', '\\|')
            lines.append(f'| {purpose} | {ticks}{code}{ticks} | {note} |')
        return '\n'.join(lines)

    inputs = input_root or (root / DOWNLOADS if pdf.is_relative_to(root / DOWNLOADS) else pdf.parent)
    batch_output = output_root or markdown.parent.parent
    try:
        documents = json.loads((root / 'inventory.json').read_text(encoding='utf-8')).get('documents', {})
    except (OSError, ValueError):
        documents = {}
    urls = [url for url, entry in documents.items() if entry.get('local_path')
            and (root / entry['local_path']).resolve() == pdf]
    scope = argument('https://example.edu/department/')
    scan_intro = 'Replace `https://example.edu/department/` with the website folder you want to search. No unique source URL is recorded for this PDF.'
    tracking_rows = []
    if len(urls) == 1:
        parts = urlsplit(urls[0])
        scope = argument(parts._replace(path=parts.path.rsplit('/', 1)[0] + '/', query='', fragment='').geturl())
        scan_intro = 'These commands search the website folder recorded for this PDF. Replace the URL to search another folder.'
        tracking_rows = [
            ('Show latest scan’s status tree', 'python site.py tree', 'Shows workflow status based on local files.'),
            ('Show this scope’s status tree', f'python site.py tree {scope}', 'Shows this website folder.'),
        ]
    publication_section = 'This document uses a custom output location. `verify.py` only accepts documents under `2_pending_review/`.'
    if markdown.is_relative_to(root / PENDING):
        verify_command = f'python verify.py {path_arg(markdown)}'
        publication_section = ('```\n' + verify_command + '\n```\n\n'
                               'After the conversion has been reviewed and edited (if necessary) by a human, it is ready to be published. '
                               'This command moves the new files to a publication folder. From there they may be uploaded to a web server.')
        tracking_rows.append(('Mark reviewed and prepare for publication', verify_command,
                              'Prompts for your name, moves Markdown, HTML and assets to 3_ready_to_publish/, and deletes review/. Leaves a dated receipt and destination link behind. Does not rebuild HTML.'))
    tracking_rows.extend([
        ('Show conversion help', 'python convert.py --help', 'Lists conversion options.'),
        ('Show preview help', 'python preview.py --help', 'Lists preview options.'),
        ('Show website command help', 'python site.py --help', 'Lists website commands.'),
        ('Show scan options', 'python site.py scan --help', 'Lists scan options.'),
        ('Show verification help', 'python verify.py --help', 'Lists verification options. Custom output locations are outside the tracked workflow.'),
        ('Run regression tests', 'python -m unittest discover -s tests -q', 'Checks converter and preview behavior.'),
    ])
    template = Template((COMMAND_TEMPLATES / f'commands_{platform}.md').read_text(encoding='utf-8'))
    return template.substitute(
        title=escape(pdf.stem),
        convert_command=f'python convert.py {path_arg(pdf)} {path_arg(markdown.parent.parent)}',
        batch_command=f'python convert.py {path_arg(inputs)} {path_arg(batch_output)}',
        preview_command=f'python preview.py {path_arg(pdf)} {path_arg(markdown)} --inspect',
        publication_section=publication_section,
        scan_intro=scan_intro,
        scan_command=f'python site.py scan {scope}',
        tracking_table=table(tracking_rows),
    )


def format_review_log(pdf, markdown, page_count, page_notes, document_notes=()):
    """Summarize flagged pages, keeping every warning for each page together."""
    review = markdown.parent / 'review'
    source_link = quote(os.path.relpath(pdf.resolve(), review.resolve()), safe='/')
    content_link = quote(os.path.relpath(markdown.resolve(), review.resolve()), safe='/')
    lines = [f'# {escape(pdf.stem)}', '',
             f'[original.PDF]({source_link}) | [content.MD]({content_link}) | [preview.HTML](../preview.html)', '',
             '## Document review', '',
             *['- ' + escape(note) for note in document_notes], '',
             f'## {page_count} pages, {len(page_notes)} flagged for review:', '',
             '| Page | Check | Original | Revision |', '| --- | --- | --- | --- |']
    for number, notes in sorted(page_notes.items()):
        details = escape(' '.join(' '.join(note.split()) for note in dict.fromkeys(notes)))
        lines.append(f'| **{number}** | {details} | [PDF]({source_link}#page={number}) '
                     f'| [MD]({content_link}#source-page{number}) |')
    return '\n'.join(lines) + '\n'


def convert_pdf(pdf, destination, *, input_root=None, output_root=None):
    if destination.exists():
        raise FileExistsError(f'{destination} exists. Choose another output folder to protect edits.')
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    with pymupdf.open(pdf) as document, pdfplumber.open(pdf) as plumb:
        if document.needs_pass:
            raise ValueError('Password-protected PDF; provide an unlocked copy.')
        if not document.page_count:
            raise ValueError('PDF has no pages.')
        sizes, fonts = Counter(), Counter()
        for page in document:
            for block in page.get_text('dict')['blocks']:
                for line in block.get('lines', []):
                    for span in line['spans']:
                        characters = len(span['text'].strip())
                        if characters:
                            size = round(span['size'], 1)
                            sizes[size] += characters
                            fonts[(span['font'], span['flags'], size)] += characters
        body_size = sizes.most_common(1)[0][0] if sizes else 11
        font, font_note = infer_document_font(fonts, body_size)
        assets = destination / 'assets'
        destination.mkdir(parents=True)
        output = [f'---\nfont: {font}\n---', f'<!-- Source: {pdf.name}; SHA-256: {digest} -->']
        document_notes = ['Headings, reading order, and tables are inferred. '
                          'Review flagged pages and describe images before publication.']
        if font_note:
            document_notes.append('Font: ' + font_note)
        previous_table = None
        found_title = False
        flagged_pages = 0
        page_notes = {}
        description_number = 0
        list_stack = []
        for index, page in enumerate(document):
            number = index + 1
            notes, events = [], []
            text_source = plumb.pages[index].extract_text() or ''
            try:
                tables = plumb.pages[index].find_tables()
            except Exception as exc:
                tables = []
                notes.append(f'Table extraction failed ({type(exc).__name__}); check the page image.')
            if page.rotation or abs(plumb.pages[index].width - page.rect.width) > 1 or page.cropbox != page.mediabox:
                tables = []
                notes.append('Rotated or cropped page: table coordinates may differ; check the page image.')
            text_blocks = page.get_text('rawdict')['blocks']
            for block in text_blocks:
                for line in block.get('lines', []):
                    for span in line['spans']:
                        span['text'] = ''.join(char['c'] for char in span['chars'])
            join_bullet_lines(text_blocks)
            page_drawings = page.get_drawings()
            text_traces, annotation_drawings = mark_highlights(page, text_blocks, page_drawings)
            page_images = page.get_image_info()
            checkbox_events, checkbox_spans, checkbox_drawings, checkbox_tables = extract_checkboxes(
                page_drawings, text_blocks, tables, page_images)
            events.extend(checkbox_events)
            if checkbox_events:
                notes.append('Checkbox states and labels inferred from position; confirm the selected answers.')
            tables = [table for table in tables if id(table) not in checkbox_tables]
            boxes = [table.bbox for table in tables]
            for table in tables:
                events.append((table.bbox[1], table.bbox[0], 'table', table))
            for block in text_blocks:
                for line in block.get('lines', []):
                    remaining = [s for s in line['spans'] if id(s) not in checkbox_spans]
                    nonempty = [i for i, s in enumerate(remaining) if s['text'].strip()]
                    if not nonempty or any(inside(line['bbox'], box) for box in boxes):
                        continue
                    # Spaces may be separate spans between a number and its text.
                    spans = remaining[nonempty[0]:nonempty[-1] + 1]
                    bullet = bullet_details(spans)
                    # Separate a large bold heading from body text on the same line.
                    split = 0
                    while (not bullet and split < len(spans) and spans[split]['flags'] & 16
                           and spans[split]['size'] > body_size + 0.5):
                        split += 1
                    groups = [('heading', spans[:split]), ('text', spans[split:])] if split else [('text', spans)]
                    for kind, group in groups:
                        if group:
                            data = {'text': clean(''.join(s['text'] for s in group)),
                                    'markdown': format_spans(group, heading=kind == 'heading', bullet=bullet),
                                    'bullet': bullet,
                                    'bbox': line['bbox'], 'size': max(s['size'] for s in group)}
                            events.append((line['bbox'][1], group[0]['bbox'][0], kind, data))
            for idx, info in enumerate(page_images):
                box = pymupdf.Rect(info['bbox']) & page.rect
                if box.is_empty:
                    continue
                path = image(page, assets, f'page-{number}-image-{idx+1}.png', box)
                events.append((box.y0, box.x0, 'image', path))
                notes.append('Image preserved; add a meaningful description and check any text inside it.')
            drawings = [d for d in page_drawings
                       if d['seqno'] not in checkbox_drawings
                       and d['seqno'] not in annotation_drawings
                       and not any((pymupdf.Rect(box) + (-2, -2, 2, 2)).contains(d['rect'])
                                  for box in boxes)
                       and not is_underline(pymupdf.Rect(d['rect']), text_blocks)]
            # Keep all strokes when a figure is present, including its rules
            # and labels. A page containing only plain decoration needs no PNG.
            vectors = ([pymupdf.Rect(d['rect']) for d in drawings]
                       if any(not is_decoration(d, text_blocks, text_traces) for d in drawings) else [])
            if vectors:
                box = pymupdf.Rect(vectors[0])
                for rect in vectors[1:]:
                    box |= rect
                box = (box + (-2, -2, 2, 2)) & page.rect
                if not box.is_empty:
                    path = image(page, assets, f'page-{number}-graphics.png', box)
                    events.append((box.y0, box.x0, 'image', path))
                    notes.append('Vector graphics preserved as an image; check whether they are decorative or need a description. Mermaid conversion is manual.')
            links = []
            for link in page.get_links():
                if link.get('uri'):
                    uri = link['uri']
                    label_lines = page.get_textbox(link['from']).splitlines()
                    label = clean(label_lines[0]) if label_lines else ''
                    if urlsplit(uri).scheme.lower() in ('http', 'https', 'mailto'):
                        links.append((label, quote(uri, safe=':/?#@!$&\'*,;=+%~-._')))
                    else:
                        notes.append(f'Link with unsupported scheme retained as text: {uri}')
                elif link.get('kind') == pymupdf.LINK_GOTO:
                    notes.append(f'Internal PDF link points to source page {link.get("page", 0) + 1}; restore the Markdown destination after review.')
            page_parts, paragraph = [], ''
            prior = None
            used_links = set()

            def flush():
                nonlocal paragraph
                if paragraph:
                    page_parts.append(paragraph)
                    paragraph = ''

            ordered = sorted(events, key=lambda event: (round(event[0], 1), event[1]))
            # Keep page metadata inside the prior item when a list crosses a
            # page boundary; an unindented HTML comment would terminate it.
            first = ordered[0][3] if ordered and ordered[0][2] == 'text' else None
            continues = list_stack and first and (
                (first['bullet'] and first['bullet']['x'] >= list_stack[0]['x'] - 3)
                or (not first['bullet'] and abs(first['bbox'][0] - list_stack[-1]['body_x']) <= 4))
            page_indent = ' ' * (4 * len(list_stack)) if continues else ''
            if not continues:
                list_stack.clear()
            line_breaks, break_notes = infer_line_breaks(ordered)
            notes.extend(break_notes)
            for event_index, (_, _, kind, value) in enumerate(ordered):
                if kind == 'table':
                    flush()
                    list_stack.clear()
                    md, warning, state = make_table(value, previous_table if event_index == 0 else None, number, page)
                    page_parts.append(md)
                    notes.append(warning)
                    previous_table = state
                    prior = None
                elif kind == 'checkbox':
                    flush()
                    list_stack.clear()
                    page_parts.append(value)
                    prior = None
                elif kind == 'image':
                    flush()
                    list_stack.clear()
                    description_number += 1
                    page_parts.append(f'<!-- description: desc-{description_number:03d} -->\n\n'
                                      f'![Source image on page {number}]({value})')
                    prior = None
                else:
                    raw = value['text']
                    text = value['markdown']
                    for label, uri in links:
                        if label and escape(label) in text:
                            text = text.replace(escape(label), f'[{escape(label)}]({uri})', 1)
                            used_links.add(uri)
                    bullet = re.match(r'^[•●▪]\s*', raw)
                    label = re.match(r'^(\d{1,2}[.)]|[a-z][.)])(?:\s+|$)', raw)
                    if kind == 'heading':
                        flush()
                        list_stack.clear()
                        level = '#' if not found_title else '##'
                        page_parts.append(f'{level} {text}')
                        found_title = True
                    elif value['bullet']:
                        flush()
                        details = value['bullet']
                        while list_stack and details['x'] < list_stack[-1]['x'] - 3:
                            list_stack.pop()
                        if list_stack and abs(details['x'] - list_stack[-1]['x']) <= 3:
                            list_stack[-1] = details
                        elif list_stack and details['x'] >= list_stack[-1]['body_x'] - 3:
                            list_stack.append(details)
                        else:
                            list_stack[:] = [details]
                        paragraph = ' ' * (4 * (len(list_stack) - 1)) + text
                    elif bullet or label:
                        flush()
                        list_stack.clear()
                        paragraph = '- ' + re.sub(r'^[•●▪]\s*', '', text) if bullet else text
                    elif list_stack and abs(value['bbox'][0] - list_stack[-1]['body_x']) <= 4:
                        # Hanging lines belong to their item, not a new paragraph
                        # or a sibling bullet. At a page break they form an
                        # indented continuation after the source-page comment.
                        paragraph += (' ' if paragraph else ' ' * (4 * len(list_stack))) + text
                    else:
                        if list_stack:
                            flush()
                            list_stack.clear()
                        if prior:
                            gap = value['bbox'][1] - prior['bbox'][3]
                            if gap > max(4, prior['size'] * 0.6):
                                flush()
                            # A separate list marker and its text share a row;
                            # their indentation is not a column boundary.
                            marker = re.fullmatch(r'(?:\d{1,2}[.)]|[a-z][.)]|[•●▪])', prior['text'])
                            if (abs(value['bbox'][1] - prior['bbox'][1]) < 2
                                    and value['bbox'][0] > prior['bbox'][2] + 10 and not marker):
                                notes.append('Possible columns: check reading order against the page image.')
                                flush()
                        separator = '\\\n' if event_index in line_breaks else ' '
                        paragraph += (separator if paragraph else '') + text
                    prior = value if kind != 'heading' else None
            flush()
            for label, uri in links:
                if uri not in used_links:
                    page_parts.append(f'[{escape(label or "Source link")}]({uri})')
                    notes.append('A link was retained separately; confirm its placement.')
            if len(text_source.strip()) < 30:
                notes.append('Little or no extractable text. Page image preserved; OCR or transcription is needed.')
            if '\ufffd' in text_source or '\ufffd' in '\n'.join(page_parts):
                notes.append('Unrecognized characters found; compare with the page image.')
            missing = words(text_source) - words('\n'.join(page_parts))
            missing['o'] -= sum(value['bullet']['symbol'] == 'o' for _, _, kind, value in ordered
                                if kind == 'text' and value['bullet'])
            missing = +missing
            if missing:
                sample = ', '.join(f'{word} ({count})' for word, count in list(missing.items())[:12])
                notes.append(f'Possible text loss compared with independent extraction: {sample}. Check the page image.')
            if previous_table and (not ordered or ordered[-1][2] != 'table'):
                previous_table = None
            output.append(page_indent + f'<!-- Source page {number} -->')
            output.append(page_indent + f'<a id="source-page{number}"></a>')
            if notes:
                flagged_pages += 1
                page_notes[number] = list(dict.fromkeys(notes))
                review = destination / 'review'
                review.mkdir(exist_ok=True)
                image(page, review, f'p{number:03d}-original.png')
            output.extend(page_parts)
        markdown = destination / 'content.md'
        review = destination / 'review'
        review.mkdir(exist_ok=True)
        (review / 'log.md').write_text(
            format_review_log(pdf, markdown, len(document), page_notes, document_notes), encoding='utf-8')
        for platform in ('mac', 'win'):
            (review / f'commands_{platform}.md').write_text(
                review_commands(pdf, markdown, input_root, output_root, platform), encoding='utf-8')
        markdown.write_text(join_blocks(output) + '\n', encoding='utf-8')
        return len(document), flagged_pages


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog='Existing document folders are never overwritten. Use a new output directory to rerun.')
    parser.add_argument('input', type=Path, help='PDF file or folder (folders are scanned recursively)')
    parser.add_argument('output', type=Path, nargs='?', default=Path(PENDING), help=f'Output root (default: {PENDING})')
    parser.add_argument('--chrome', help='Chrome executable used to generate review images and HTML')
    args = parser.parse_args()
    source, target = args.input.resolve(), args.output.resolve()
    if source.is_file() and source.suffix.lower() == '.pdf':
        pdfs, base = [source], source.parent
    elif source.is_dir():
        pdfs, base = sorted(p for p in source.rglob('*') if p.is_file() and p.suffix.lower() == '.pdf'), source
    else:
        parser.error('Input must be an existing PDF or folder.')
    if not pdfs:
        parser.error('No PDFs found.')
    try:
        chrome = find_chrome(args.chrome)
    except ValueError as exc:
        parser.error(str(exc))
    failures = 0
    for pdf in pdfs:
        destination = conversion_destination(pdf, base, target)
        try:
            pages, flagged = convert_pdf(pdf, destination, input_root=base, output_root=target)
            print(f'{pdf.name}: {pages} pages, {flagged} flagged for review -> {destination / "content.md"}')
            try:
                preview(pdf, destination / 'content.md', chrome)
            except Exception as exc:
                raise RuntimeError(f'Markdown saved, but preview failed: {exc}. '
                                   f'Retry preview.py with this PDF and {destination / "content.md"}.') from exc
        except Exception as exc:
            failures += 1
            print(f'{pdf.name}: {exc}', file=sys.stderr)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
