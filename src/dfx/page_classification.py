"""Conservative page-layout heuristics, not semantic or accessibility analysis."""
from statistics import median
import operator

import pymupdf


# These checks drive both classification and the diagnostic report.
# Each group passes when all (or any) of its individual checks pass.
CHECK_GROUPS = (
    ('scan', 'Likely scan', 'all', (
        ('bitmap_coverage', 'Largest image coverage', '>=', .8),
        ('characters', 'Extractable characters', '<', 30),
        ('shapes', 'Substantial shapes', '<', 6))),
    ('svg_common', 'SVG: shared requirements', 'all', (
        ('coverage', 'Area spanned by shapes', '>=', .5),
        ('aligned_prose', 'Aligned paragraph-length lines', '<', 3),
        ('table', 'Strong evidence of a regular table', '==', False))),
    ('labelled', 'SVG: labelled diagram', 'all', (
        ('lines', 'Nonempty text lines after deduplication', '>=', 12),
        ('short_labels', 'Short text labels', '>=', .65),
        ('spread', 'Page regions containing text (out of 9)', '>=', 5),
        ('overlap', 'Text inside shape bounds', '>=', .5),
        ('shapes', 'Substantial shapes', '>=', 8),
        ('connections', 'Recognized connections', '>=', 3))),
    ('unlabelled', 'SVG: unlabelled artwork alternative', 'all', (
        ('lines', 'Nonempty text lines after deduplication', '<=', 3),
        ('shapes', 'Substantial shapes', '>=', 12),
        ('curved_paths', 'Drawing paths containing curves', '>=', 8))),
    ('uncertain_common', 'Uncertain layout: shared requirements', 'all', (
        ('coverage', 'Area spanned by shapes', '>=', .4),
        ('shapes', 'Substantial shapes', '>=', 6))),
    ('uncertain_graphics', 'Uncertain layout: graphics alternatives', 'any', (
        ('connections', 'Recognized connections', '>=', 1),
        ('curved_paths', 'Drawing paths containing curves', '>=', 4))),
    ('uncertain_text', 'Uncertain layout: text alternatives', 'any', (
        ('short_labels', 'Short text labels', '>=', .5),
        ('lines', 'Nonempty text lines after deduplication', '<=', 3))),
)


def classification_checks(signals):
    comparisons = {'>=': operator.ge, '<': operator.lt, '<=': operator.le, '==': operator.eq}
    groups = []
    for key, title, combine, rules in CHECK_GROUPS:
        checks = [dict(signal=signal, label=label, value=signals[signal],
                       comparison=comparison, threshold=threshold,
                       passed=comparisons[comparison](signals[signal], threshold))
                  for signal, label, comparison, threshold in rules]
        passed = (all if combine == 'all' else any)(check['passed'] for check in checks)
        groups.append(dict(key=key, title=title, combine=combine, checks=checks, passed=passed))
    return groups


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

    # A standalone diagonal line has a broad bounding rectangle but is not a
    # label box. Keep the line for connection detection, not shape containment.
    # Thin rules and page backgrounds are also excluded; repeated bounds dedupe.
    shapes = {tuple(round(v, 1) for v in d['rect']) for d in drawings
              if not (len(d['items']) == 1 and d['items'][0][0] == 'l')
              and d['rect'].width >= 8 and d['rect'].height >= 8
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
    return classify_signals(page_signals(page, blocks, drawings, images, tables))


def classify_signals(s):
    passed = {group['key']: group['passed'] for group in classification_checks(s)}
    if passed['scan']:
        return 'scan', ('Likely scanned/image-only page: a bitmap covers most of the page with little '
                        'extractable text. This converter has no full-page OCR stage; OCR or transcription is '
                        'required to determine and recover its text. Bitmap artwork is also possible.')
    reason = (f"{s['shapes']} substantial shapes, {s['connections']} connections between shapes, "
              f"{s['coverage']:.0%} composition coverage, {s['short_labels']:.0%} short text lines, "
              f"{s['overlap']:.0%} text/shape overlap; {s['aligned_prose']} aligned prose lines; "
              f"regular table evidence {'present' if s['table'] else 'absent'}")
    if passed['svg_common'] and (passed['labelled'] or passed['unlabelled']):
        return 'svg', 'Likely diagram/artwork: ' + reason + '.'
    if passed['uncertain_common'] and passed['uncertain_graphics'] and passed['uncertain_text']:
        return 'uncertain', 'Uncertain page layout; Markdown retained: ' + reason + '.'
    return 'markdown', 'Text/table layout or insufficient evidence for whole-page artwork.'
