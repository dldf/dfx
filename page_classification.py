"""Conservative page-layout heuristics, not semantic or accessibility analysis."""
from statistics import median

import pymupdf


def page_signals(page, blocks, drawings, images, tables):
    """Reuse extraction data; deduplicate overprinted text and shape outlines."""
    bounds = page.rect
    area = bounds.get_area()
    unique = {}
    for block in blocks:
        for line in block.get('lines', []):
            text = ''.join(span['text'] for span in line['spans']).strip()
            if text:
                key = (tuple(round(v, 1) for v in line['bbox']), text)
                unique[key] = (pymupdf.Rect(line['bbox']), text)
    lines = list(unique.values())
    centers = [(rect.tl + rect.br) / 2 for rect, _ in lines]
    long_lines = [rect for rect, text in lines
                  if len(text) >= 60 and rect.width >= bounds.width * .35]
    aligned = sum(any(other is not rect and abs(other.x0 - rect.x0) <= 10
                      and 0 < abs(other.y0 - rect.y0) <= max(rect.height, other.height) * 1.8
                      for other in long_lines) for rect in long_lines)

    # Thin rules and page backgrounds are not label boxes. Deduplication keeps
    # repeated fills/strokes from inflating the evidence.
    shapes = {tuple(round(v, 1) for v in d['rect']) for d in drawings
              if d['rect'].width >= 8 and d['rect'].height >= 8
              and d['rect'].get_area() <= area * .15}
    shapes = [pymupdf.Rect(rect) for rect in shapes]
    connections = set()
    curves = 0
    for drawing in drawings:
        curves += any(item[0] == 'c' for item in drawing['items'])
        for item in drawing['items']:
            if item[0] != 'l':
                continue
            start, end = item[1:3]
            if abs(end - start) < 12:
                continue
            starts = {i for i, rect in enumerate(shapes)
                      if (rect + (-4, -4, 4, 4)).contains(start)}
            ends = {i for i, rect in enumerate(shapes)
                    if (rect + (-4, -4, 4, 4)).contains(end)}
            # A stroke along one box's edge is not a connection between boxes.
            if starts and ends and starts.isdisjoint(ends):
                connections.add(tuple(round(v, 1) for point in (start, end) for v in point))

    composition = pymupdf.Rect()
    for rect in shapes:
        composition |= rect
    composition &= bounds
    spread = len({(min(2, max(0, int((p.x - bounds.x0) / bounds.width * 3))),
                   min(2, max(0, int((p.y - bounds.y0) / bounds.height * 3)))) for p in centers})
    overlap = sum(any(rect.contains(point) for rect in shapes) for point in centers) / max(1, len(lines))

    # A regular multi-row grid supports table interpretation. Large, sparse
    # cells (e.g. semester columns in advising diagrams) are weaker evidence.
    regular_cells = []
    for table in tables:
        cells = [pymupdf.Rect(cell) for cell in table.cells if cell]
        if (len(table.rows) >= 3 and len(table.columns) >= 2 and len(cells) >= 6
                and median(cell.height for cell in cells) <= bounds.height * .18):
            regular_cells.extend(cells)
    table_text = sum(any(cell.contains(point) for cell in regular_cells) for point in centers)
    return {
        'lines': len(lines), 'characters': sum(len(text) for _, text in lines),
        'aligned_prose': aligned,
        'short_labels': sum(len(text) <= 40 for _, text in lines) / max(1, len(lines)),
        'shapes': len(shapes), 'connections': len(connections), 'curved_paths': curves,
        'coverage': composition.get_area() / area, 'spread': spread, 'overlap': overlap,
        'table': bool(regular_cells) and table_text >= len(lines) * .5,
        'bitmap_coverage': max(((pymupdf.Rect(info['bbox']) & bounds).get_area() / area
                                for info in images), default=0),
    }


def classify_page(page, blocks, drawings, images, tables):
    """Return markdown, svg, uncertain, or scan, plus a reviewable reason.

    Thresholds are conservative starting points calibrated on assessment prose,
    assessment tables, and an advising diagram, not universal PDF rules.
    """
    s = page_signals(page, blocks, drawings, images, tables)
    if s['bitmap_coverage'] >= .8 and s['characters'] < 30 and s['shapes'] < 6:
        return 'scan', ('Likely scanned/image-only page: a bitmap covers most of the page with little '
                        'extractable text. This converter has no OCR stage; OCR or transcription is '
                        'required to determine and recover its text. Bitmap artwork is also possible.')
    broad = s['coverage'] >= .5
    labels = s['lines'] >= 12 and s['short_labels'] >= .65 and s['spread'] >= 5 and s['overlap'] >= .5
    connected = s['shapes'] >= 8 and s['connections'] >= 3
    # Unlabelled vector artwork also needs extensive, non-grid geometry.
    unlabelled = s['lines'] <= 3 and s['shapes'] >= 12 and s['curved_paths'] >= 8
    reason = (f"{s['shapes']} substantial shapes, {s['connections']} connections between shapes, "
              f"{s['coverage']:.0%} composition coverage, {s['short_labels']:.0%} short text lines, "
              f"{s['overlap']:.0%} text/shape overlap; {s['aligned_prose']} aligned prose lines; "
              f"regular table evidence {'present' if s['table'] else 'absent'}")
    if broad and ((labels and connected) or unlabelled) and s['aligned_prose'] < 3 and not s['table']:
        return 'svg', 'Likely diagram/artwork: ' + reason + '.'
    if (s['coverage'] >= .4 and s['shapes'] >= 6
            and (s['connections'] >= 1 or s['curved_paths'] >= 4)
            and (s['short_labels'] >= .5 or s['lines'] <= 3)):
        return 'uncertain', 'Uncertain page layout; Markdown retained: ' + reason + '.'
    return 'markdown', 'Text/table layout or insufficient evidence for whole-page artwork.'
