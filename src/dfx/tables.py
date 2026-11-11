"""PDF table geometry, formatting, and continuation handling."""
import json
import re
from collections import defaultdict

import pymupdf
from pdfplumber.table import Table

from .text import clean, format_text


def inside(box, region):
    x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return region[0] <= x <= region[2] and region[1] <= y <= region[3]


def detect_tables(page):
    """Keep filled and outlined rectangles available as candidate cell boundaries."""
    return [normalize_cell_padding(table) for table in page.find_tables()]


def normalize_cell_padding(table):
    """Recover cells from boundary continuity and inset subdivisions.

    Drawing primitives, colors, fills, and stroke widths are not consulted.
    Every boundary that crosses the table is retained unless paired, empty
    side gaps prove it bounds an inset text lane. Real row divisions survive.
    """
    if not any(cell is None for row in table.rows for cell in row.cells):
        return table
    axes = []
    for axis in (0, 1):
        other = 1 - axis
        spans = defaultdict(list)
        for cell in table.cells:
            for side in (axis, axis + 2):
                spans[cell[side]].append((cell[other], cell[other + 2]))
        boundaries = []
        for coordinate, intervals in sorted(spans.items()):
            end = table.bbox[other]
            for start, stop in sorted(intervals):
                if start > end + .1:
                    break
                end = max(end, stop)
            if end >= table.bbox[other + 2] - .1:
                boundaries.append(coordinate)
        axes.append(boundaries)
    xs, ys = axes
    if len(xs) < 3 or len(ys) < 3:
        return table
    characters = [(char['x0'], char['top'], char['x1'], char['bottom'])
                  for char in table.page.chars if char['text'].strip()]

    def empty(box):
        return not any(inside(char, box) for char in characters)

    # Inset text lanes can repeat through every row. Identify them by partial
    # horizontal divisions and empty gaps on BOTH sides, never by their paint.
    inset_edges = set()
    for left, top, right, bottom in table.cells:
        if (top in ys and bottom in ys) or left not in xs or right not in xs:
            continue
        first, last = xs.index(left), xs.index(right)
        if first == 0 or last == len(xs) - 1:
            continue
        outer_left, outer_right = xs[first - 1], xs[last + 1]
        if (left - outer_left <= (right - left) / 4
                and outer_right - right <= (right - left) / 4
                and empty((outer_left, table.bbox[1], left, table.bbox[3]))
                and empty((right, table.bbox[1], outer_right, table.bbox[3]))):
            inset_edges.update((left, right))
    xs = [x for x in xs if x not in inset_edges]
    if len(xs) < 3:
        return table
    cells = [(left, top, right, bottom) for top, bottom in zip(ys, ys[1:])
             for left, right in zip(xs, xs[1:])]
    if len(cells) >= len(table.cells):
        return table
    lanes = defaultdict(set)
    for box in table.cells:
        owners = [cell for cell in cells if (cell[0] - .1 <= box[0] and cell[1] - .1 <= box[1]
                                             and box[2] <= cell[2] + .1 and box[3] <= cell[3] + .1)]
        if len(owners) != 1:
            return table
        cell = owners[0]
        if all(abs(a - b) <= .1 for a, b in zip(box, cell)):
            continue
        full_height = abs(box[1] - cell[1]) <= .1 and abs(box[3] - cell[3]) <= .1
        at_side = abs(box[0] - cell[0]) <= .1 or abs(box[2] - cell[2]) <= .1
        if full_height and at_side and empty(box):
            continue  # Empty side strips around one inset lane.
        if not (box[0] > cell[0] + 1 and box[2] < cell[2] - 1):
            return table  # Preserve full-width splits and side-by-side cells.
        lanes[cell].add(box)
    if len(lanes) < 2 and not any(len(boxes) > 1 for boxes in lanes.values()):
        return table
    for boxes in lanes.values():
        first = next(iter(boxes))
        if any(abs(box[0] - first[0]) > .1 or abs(box[2] - first[2]) > .1 for box in boxes):
            return table
    recovered = Table(table.page, cells)
    recovered.padding_normalized = True
    return recovered


def extract_grid_text(table):
    """Keep source character order when it agrees with each recovered cell.

    This avoids moving trailing whitespace from inset text boxes into words.
    PDFs painted out of reading order still use geometric text ordering.
    """
    flow = getattr(table, 'padding_normalized', False)
    if flow:
        for cell in table.cells:
            previous = None
            for char in table.page.chars:
                box = (char['x0'], char['top'], char['x1'], char['bottom'])
                if not char['text'].strip() or not inside(box, cell):
                    continue
                if previous and (char['top'] < previous['top'] - 3
                                 or (abs(char['top'] - previous['top']) <= 3
                                     and char['x0'] < previous['x0'] - 1)):
                    flow = False
                    break
                previous = char
            if not flow:
                break
    return table.extract(use_text_flow=flow)


def simplify_header_padding(rows, geometry):
    """Recover a header grid supported by every complete body row.

    Multi-line fragments may be joined only inside a padded text lane: the
    fragments must share their left/right edges, be inset on BOTH sides of the
    body column, and occupy separate vertical bands. Full-width subdivisions
    and side-by-side nonempty cells remain meaningful candidate cells.
    """
    actual = [[(value, cell) for value, cell in zip(row, positions.cells) if cell is not None]
              for row, positions in zip(rows, geometry)]
    for start in range(1, len(rows)):
        body = actual[start:]
        columns = [(cell[0], cell[2]) for _, cell in body[0]]
        if not columns or len(columns) >= len(rows[0]):
            continue
        if any(len(row) != len(columns) or any(
                abs(cell[0] - left) > .1 or abs(cell[2] - right) > .1
                or abs(cell[1] - positions.bbox[1]) > .1
                or abs(cell[3] - positions.bbox[3]) > .1
                for (_, cell), (left, right) in zip(row, columns))
               for row, positions in zip(body, geometry[start:])):
            continue
        groups = [[] for _ in columns]
        valid = True
        for row in actual[:start]:
            for value, cell in row:
                matches = [i for i, (left, right) in enumerate(columns)
                           if left - .1 <= cell[0] and cell[2] <= right + .1]
                if len(matches) != 1:
                    valid = False
                    break
                if clean(value):
                    groups[matches[0]].append((value, cell))
        if not valid or not all(groups):
            continue
        for group, (left, right) in zip(groups, columns):
            if len(group) <= 1:
                continue
            first = group[0][1]
            if not (first[0] > left + 1 and first[2] < right - 1) or any(
                    abs(cell[0] - first[0]) > .1 or abs(cell[2] - first[2]) > .1
                    or cell[1] < prior[3] - .1
                    for (_, prior), (_, cell) in zip(group, group[1:])):
                valid = False
                break
        if valid:
            return ([[clean('\n'.join(value for value, _ in group)) for group in groups]]
                    + [[value for value, _ in row] for row in body],
                    [left for left, _ in columns] + [columns[-1][1]])
    return None


def pipe_table(header, rows, *, widths=(), row_header=0, caption=''):
    """Emit a rectangular pipe table with the converter's existing metadata."""
    if not header or any(len(row) != len(header) for row in rows):
        raise ValueError('Pipe table rows must match the header column count.')
    render = lambda row: '| ' + ' | '.join(format_text(cell) for cell in row) + ' |'
    text = '<!-- table: ' + json.dumps({'widths': list(widths), 'row_header': row_header}) + ' -->\n\n'
    if caption:
        text += ': ' + format_text(caption) + '\n\n'
    return text + '\n'.join([render(header), '| ' + ' | '.join(['---'] * len(header)) + ' |',
                             *[render(row) for row in rows]])


def bold_text(page, box):
    blocks = page.get_text('dict', clip=pymupdf.Rect(box))['blocks']
    spans = [s for block in blocks for line in block.get('lines', [])
             for s in line['spans'] if s['text'].strip()]
    return sum(len(s['text']) for s in spans if s['flags'] & 16) > sum(len(s['text']) for s in spans) / 2


def complex_pipe_tables(rows, geometry, page, caption):
    """Flatten grouped headings and split consecutive body grids without spans.

    Return no Markdown when cell relationships are uncertain, so the caller
    preserves the source region instead of inventing replacement content.
    """
    cells = [(value, box) for row, positions in zip(rows, geometry)
             for value, box in zip(row, positions.cells) if box is not None]
    xs = sorted({box[i] for _, box in cells for i in (0, 2)})
    ys = sorted({box[i] for _, box in cells for i in (1, 3)})
    # Each record describes one original cell, never a padded None slot.
    records = [(value, xs.index(box[0]), xs.index(box[2]), ys.index(box[1]), ys.index(box[3]))
               for value, box in cells]

    def preserve_source():
        return None, ('Ambiguous cell spans: source region preserved as an image. '
                      'Transcribe or describe it after checking the PDF; no replacement cells supplied.'), None

    header_rows = 1
    # Only extend an explicitly bold, subdivided heading. Ordinary body text
    # below a spanning cell must not be consumed as another header level.
    while header_rows < len(ys) - 1:
        parents = [r for r in records if r[3] < header_rows <= r[4]
                   and (r[2] - r[1] > 1 or r[4] > header_rows)]
        children = [r for r in records if r[3] == header_rows and clean(r[0])]
        if not parents or not children or not all(
                bold_text(page, (xs[left], ys[top], xs[right], ys[bottom]))
                for value, left, right, top, bottom in parents + children if clean(value)):
            break
        header_rows += 1
    if any(top < header_rows < bottom or (top >= header_rows and bottom > top + 1)
           for _, left, right, top, bottom in records):
        return preserve_source()
    headings = [r for r in records if r[3] < header_rows]
    if not any(clean(r[0]) for r in headings):
        return preserve_source()
    body = [[r for r in records if r[3] == index] for index in range(header_rows, len(ys) - 1)]
    if not body:
        return preserve_source()
    sections = []
    for row in body:
        row.sort(key=lambda r: r[1])
        pattern = tuple((r[1], r[2]) for r in row)
        if not pattern or pattern[0][0] != 0 or pattern[-1][1] != len(xs) - 1 or any(
                left[1] != right[0] for left, right in zip(pattern, pattern[1:])):
            return preserve_source()
        if not sections or sections[-1][0] != pattern:
            sections.append((pattern, []))
        sections[-1][1].append([r[0] for r in row])
    output = []
    for pattern, body_rows in sections:
        header = []
        for left, right in pattern:
            labels = [clean(value) for value, hleft, hright, top, bottom in headings
                      if hleft < right and hright > left and clean(value)]
            header.append(' — '.join(labels))
        widths = [100 * (xs[right] - xs[left]) / (xs[-1] - xs[0]) for left, right in pattern]
        output.append(pipe_table(header, body_rows, widths=widths, caption=caption))
    note = ('Grouped column headings flattened into pipe-table labels; check header associations.'
            if len(sections) == 1 else
            'Different body column layouts split into separate pipe tables with their associated headings; check associations.')
    state = None
    if len(sections) == 1:
        state = {'header': header, 'caption': caption}
    return '\n\n'.join(output), note, state


def is_page_layout_table(table, page, text_blocks):
    """Reject a page-sized single body cell containing numbered prose."""
    box = pymupdf.Rect(table.bbox)
    if (len(table.columns) != 1 or not 1 <= len(table.rows) <= 2
            or box.width < page.rect.width * .9 or box.height < page.rect.height * .9):
        return False
    body = max((pymupdf.Rect(cell) for cell in table.cells), key=lambda cell: cell.height)
    if body.height < page.rect.height * .75:
        return False
    lines = [clean(''.join(span['text'] for span in line['spans']))
             for block in text_blocks for line in block.get('lines', [])
             if inside(line['bbox'], body)]
    lines = [line for line in lines if line]
    return len(lines) >= 6 and sum(bool(re.match(r'^\d{1,2}[.)](?:\s|$)', line)) for line in lines) >= 2


def make_table(table, previous, page_number, page):
    rows = extract_grid_text(table)
    caption = ''
    # A merged title row is a caption, not part of the column headings.
    first_cells = [cell for cell in table.rows[0].cells if cell is not None]
    if (len(rows) > 1 and None in rows[0] and len(first_cells) == 1 and clean(rows[0][0])
            and abs(first_cells[0][0] - table.bbox[0]) < .1
            and abs(first_cells[0][2] - table.bbox[2]) < .1
            and first_cells[0][3] <= table.rows[1].bbox[1] + .1):
        caption = clean(next(cell for cell in rows.pop(0) if cell))
    geometry = table.rows[-len(rows):]
    simplified = simplify_header_padding(rows, geometry)
    if simplified:
        rows, simple_edges = simplified
    columns = len(rows[0])
    header_bold = bold_text(page, geometry[0].bbox)
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
    continuation = (previous and previous['page'] == page_number - 1
                    and len(previous['header']) == columns and not caption and not header_bold
                    and abs(previous['bbox'][0] - table.bbox[0]) < 8
                    and abs(previous['bbox'][2] - table.bbox[2]) < 8)
    complex_cells = any(None in row for row in rows)
    if complex_cells:
        text, note, state = complex_pipe_tables(rows, geometry, page, caption)
        if text is not None and not has_header:
            note += ' No header detected; first row assumed to be the header.'
        if state:
            state.update(bbox=table.bbox, page=page_number)
        return text, note, state
    if continuation:
        header = previous['header']
        note = 'Possible continued table: column headings reused from the preceding page; confirm the association.'
    elif has_header:
        header = rows.pop(0)
        note = 'Table structure and column headings inferred; check cell associations.'
    else:
        header = rows.pop(0)
        note = 'No header detected; first row assumed to be the header. Check cell associations.'
    if not any(clean(cell) for cell in header):
        header = [f'Column {index + 1}' for index in range(columns)]
        note += ' Empty header: positional column labels supplied; replace with meaningful headings.'
    if not caption and (has_header or continuation):
        label = next((clean(cell) for cell in header if clean(cell)), '')
        caption = (previous.get('caption', '') if continuation else '') or ('Table: ' + label if label else '')
        if caption:
            note += ' Caption inferred from the column headings.'
    labels = [clean(row[0]) for row in rows if any(clean(cell) for cell in row)]
    row_header = int(bool(labels) and all(labels) and len(set(labels)) == len(labels)
                     and not header[0].startswith('Column '))
    if row_header:
        note += ' First column inferred as row headers; confirm or set row_header to 0.'
    if not widths:
        note += ' Column boundaries uncertain; automatic layout used.'
    if simplified:
        note += ' Header padding and wrapped labels normalized to the body column grid; confirm against the PDF.'
    if getattr(table, 'padding_normalized', False):
        note += ' Cell padding normalized throughout the table grid.'
    text = pipe_table(header, rows, widths=widths, row_header=row_header, caption=caption)
    return text, note, {'header': header, 'bbox': table.bbox, 'page': page_number, 'caption': caption}
