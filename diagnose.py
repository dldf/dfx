#!/usr/bin/env python3
"""Explain a PDF's page structure and automatic classification in Markdown."""
import argparse
from collections import Counter
import hashlib
from html import escape
from pathlib import Path
import tempfile

import pdfplumber
import pymupdf

from convert import extract_page_inputs
from page_classification import classification_checks, classify_signals, page_signals
from workflow import DIAGNOSTIC, PENDING, conversion_destination


def measurement(key, value):
    if key == 'table':
        return 'Present' if value else 'Absent'
    if key in {'bitmap_coverage', 'coverage', 'short_labels', 'overlap'}:
        return f'{value:.2%}'
    return str(value)


def page_report(page, inputs, number):
    blocks, drawings, images, tables = (inputs[key] for key in ('blocks', 'drawings', 'images', 'tables'))
    signals = page_signals(page, blocks, drawings, images, tables)
    decision, reason = classify_signals(signals)
    lines = [line for block in blocks for line in block.get('lines', [])]
    spans = [span for line in lines for span in line['spans']]
    commands = Counter(item[0] for drawing in drawings for item in drawing['items'])
    text_block_count = sum(block['type'] == 0 for block in blocks)
    detected = inputs['detected_tables']
    result = [f'## Page {number}', '', f'**Automatic classification: {decision}**', '', reason, '',
              '```text', f'Page: {page.rect.width:g} × {page.rect.height:g} points; rotation {page.rotation}°',
              '├── Text', f'│   ├── {text_block_count} text blocks',
              f'│   ├── {len(lines)} text lines',
              f'│   ├── {len(spans)} spans: text, font, size, position',
              f'│   └── {signals["lines"]} nonempty lines after classifier deduplication',
              f'├── Vector drawings: {len(drawings)} drawing paths',
              f'│   ├── {commands["re"]} rectangle commands',
              f'│   ├── {commands["l"]} straight-line commands',
              f'│   ├── {commands["c"]} curve commands',
              f'│   └── {sum(count for key, count in commands.items() if key not in {"re", "l", "c"})} other commands',
              f'├── Images: {len(images)} placements',
              f'└── Inferred tables: {len(detected)} detected; {len(tables)} used for classification', '```', '']
    for index, table in enumerate(detected, 1):
        box = pymupdf.Rect(table.bbox)
        coverage = (box & page.rect).get_area() / page.rect.get_area()
        use = 'used for classification' if table in tables else 'excluded before classification'
        result.append(f'- Table {index}: {len(table.rows)} rows × {len(table.columns)} columns; '
                      f'bounds cover {coverage:.1%} of the page; {use}.')
    if detected:
        result.append('')
    if inputs['notes']:
        result.extend(['### Extraction notes', '', *['- ' + escape(note) for note in inputs['notes']], ''])
    result.extend(['### Representative objects', ''])
    span = next((span for span in spans if span['text'].strip()), None)
    if span:
        box = span['bbox']
        example = (f'Text: {span["text"]}\nFont: {span["font"]}, {span["size"]:g} points\n'
                   f'Position: x={box[0]:.2f}–{box[2]:.2f}, y={box[1]:.2f}–{box[3]:.2f}')
        result.extend(['First nonempty text span:', '',
                       *['    ' + line for line in example.splitlines()], ''])
    else:
        result.extend(['No nonempty text span was extracted.', ''])
    segment = next((item for drawing in drawings for item in drawing['items']
                    if item[0] == 'l' and abs(item[2] - item[1]) >= 12), None)
    if segment:
        start, end = segment[1:3]
        result.extend(['Example straight segment (not necessarily a recognized connection):', '',
                       f'    Start: ({start.x:.2f}, {start.y:.2f})',
                       f'    End:   ({end.x:.2f}, {end.y:.2f})', ''])
    else:
        result.extend(['No straight segment at least 12 points long was extracted.', ''])
    requirements = {'>=': 'At least', '<': 'Fewer than', '<=': 'At most', '==': 'Must be'}
    for group in classification_checks(signals):
        result.extend([f'### {group["title"]}', '',
                       f'{group["combine"].capitalize()} checks must pass. '
                       f'Group result: **{"Pass" if group["passed"] else "Fail"}**.', '',
                       '| Check | This page | Requirement | Result |',
                       '| --- | --- | --- | --- |'])
        for check in group['checks']:
            value = measurement(check['signal'], check['value'])
            threshold = measurement(check['signal'], check['threshold'])
            requirement = requirements[check['comparison']] + ' ' + threshold
            result.append(f'| {check["label"]} | {value} | {requirement} | '
                          f'{"Pass" if check["passed"] else "Fail"} |')
        result.append('')
    result.extend(['### Candidate SVG signals (observational only)', '',
                   'These proposed thresholds are evidence to evaluate. They do not affect '
                   'mode selection and are not combined into a score. Counts are raw: text '
                   'blocks, individual drawing commands, and image placements.', '',
                   '| Signal | This page | Proposed threshold | Met? |',
                   '| --- | --- | --- | --- |'])
    for label, value, threshold in (
            ('Text blocks', text_block_count, 100),
            ('Straight-line commands', commands['l'], 10),
            ('Curve commands', commands['c'], 10),
            ('Image placements', len(images), 20)):
        result.append(f'| {label} | {value} | More than {threshold} | '
                      f'{"Yes" if value > threshold else "No"} |')
    result.append('')
    return '\n'.join(result)


def diagnostic_report(pdf):
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    result = ['# PDF diagnostic', '', f'Source: {escape(str(pdf.resolve()))}', '',
              f'SHA-256: `{digest}`', '',
              'This report describes the current automatic classifier, not a previous conversion '
              'or an explicit page-mode override. It does not perform OCR or convert the document.', '',
              'Decision order:', '',
              '1. **scan** if all scan checks pass. The converter preserves image content and flags missing OCR.',
              '2. **svg** if shared SVG checks and either the labelled-diagram or unlabelled-artwork group pass.',
              '3. **uncertain** if shared uncertain-layout checks and both alternative groups pass. '
              'The converter retains Markdown and provides a comparison SVG.',
              '4. **markdown** otherwise.', '',
              'These are geometric heuristics, not semantic understanding. A failed group does not '
              'prevent another path from succeeding. Percentages are rounded for display; pass/fail '
              'uses the unrounded values. Table structures are inferred. Shape coverage measures '
              'the enclosing area, not the amount of ink on the page.', '']
    with pymupdf.open(pdf) as document:
        if document.needs_pass:
            raise ValueError('Password-protected PDF; provide an unlocked copy.')
        if not document.page_count:
            raise ValueError('PDF has no pages.')
        result.extend([f'Pages: {document.page_count}', ''])
        with pdfplumber.open(pdf) as plumb:
            for index, page in enumerate(document):
                inputs = extract_page_inputs(page, plumb.pages[index])
                result.append(page_report(page, inputs, index + 1))
    return '\n'.join(result) + '\n'


def diagnose_pdf(pdf, destination):
    report = diagnostic_report(pdf)
    review = destination / 'review'
    path = review / DIAGNOSTIC
    for directory in (destination, review):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError(f'Expected an ordinary directory: {directory}')
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f'Expected an ordinary diagnostic file: {path}')
    review.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=review, encoding='utf-8', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(report)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     epilog='Rerunning replaces only review/diagnostic.md. No browser is required.')
    parser.add_argument('input', type=Path, help='PDF file to diagnose')
    parser.add_argument('output', type=Path, nargs='?', default=Path(PENDING),
                        help=f'Output root, as with dfx convert (default: {PENDING})')
    args = parser.parse_args()
    source = args.input.resolve()
    if not source.is_file() or source.suffix.lower() != '.pdf':
        parser.error('Input must be an existing PDF file.')
    destination = conversion_destination(source, source.parent, args.output.resolve())
    try:
        path = diagnose_pdf(source, destination)
    except Exception as exc:
        parser.exit(1, f'Diagnosis failed: {exc}\n')
    print(f'Diagnostic saved: {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
