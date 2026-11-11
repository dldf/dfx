"""Conservative, document-local recovery of incorrect PDF character mappings."""
from collections import Counter, defaultdict
import csv
from difflib import SequenceMatcher
import io
import re
import shutil
import subprocess
import unicodedata

import pymupdf


MIN_CONFIDENCE = 90
MAX_OCR_LINES = 40


def position(text, origin):
    return text, round(origin[0], 1), round(origin[1], 1)


def plumber_position(char, height):
    matrix = char.get('matrix')
    return position(char['text'], (matrix[4], height - matrix[5])) if matrix else None


def letter_sequence(text):
    return 1 <= len(text) <= 4 and text.isalpha()


def suspect_positions(text):
    """Identify candidates, never replacements. Avoid URLs, paths, and short formulas."""
    found = set()
    for word in re.finditer(r'\S+', text):
        value = word[0]
        if any(marker in value for marker in ('://', '@', '/', '\\', '::')):
            continue
        for index, char in enumerate(value):
            if char == '\ufffd' or unicodedata.category(char) == 'Co' or '\ufb00' <= char <= '\ufb06':
                found.add(word.start() + index)
            elif len(value) >= 5 and 0 < index < len(value) - 1:
                before, after = value[index - 1], value[index + 1]
                if before.isalpha() and after.islower() and (
                        char in ':;?!|' or before.islower() and char.isupper()):
                    found.add(word.start() + index)
    return found


def ocr_line(page, box, executable):
    """Read only a suspicious line, returning word text and Tesseract confidence."""
    clip = (pymupdf.Rect(box) + (-3, -3, 3, 3)) & page.rect
    if clip.is_empty or clip.width * clip.height > 150000:
        return []
    pix = page.get_pixmap(matrix=pymupdf.Matrix(4, 4), clip=clip, alpha=False)
    result = subprocess.run([executable, 'stdin', 'stdout', '-l', 'eng', '--psm', '7', 'tsv'],
                            input=pix.tobytes('png'), capture_output=True, timeout=15, check=True)
    rows = csv.DictReader(io.StringIO(result.stdout.decode('utf-8')), delimiter='\t')
    return [(row['text'], float(row['conf'])) for row in rows if row.get('level') == '5' and row.get('text')]


def word_changes(original, recognized, allowed):
    """Accept only isolated suspect-glyph substitutions; all surrounding text must agree."""
    changes = {}
    for operation, left, right, start, end in SequenceMatcher(None, original, recognized, autojunk=False).get_opcodes():
        if operation == 'equal':
            continue
        replacement = recognized[start:end]
        if operation != 'replace' or right != left + 1 or left not in allowed or not letter_sequence(replacement):
            return None
        changes[left] = replacement
    return changes


class GlyphRepair:
    """Verify mappings before applying them, so conflicting later evidence can veto OCR.

    Keys include the PDF font resource, glyph ID, and original character. Font
    names alone are insufficient: different subsets may share a display name.
    Nothing is cached across documents, and the source PDF is never modified.
    """
    def __init__(self, document, plumber, page_modes=None):
        self.locations = {}
        self.suspects = defaultdict(set)
        self.corrections = {}
        self.verified = set()
        self.methods = {}
        self.font_names = {}
        self.font_maps = {}
        self.issues = defaultdict(list)
        self.examples = defaultdict(lambda: defaultdict(set))
        self.evidence = defaultdict(lambda: defaultdict(set))
        executable = shutil.which('tesseract')
        self.ocr_issue = '' if executable else 'Tesseract is unavailable; install Tesseract with English language data for targeted glyph verification.'
        lines = []
        for index, page in enumerate(document):
            if (page_modes or {}).get(index + 1) == 'svg':
                continue
            blocks = page.get_text('rawdict')['blocks']
            candidates = []
            for block in blocks:
                for line in block.get('lines', []):
                    chars = [char for span in line['spans'] for char in span['chars']]
                    text = ''.join(char['c'] for char in chars)
                    suspects = suspect_positions(text)
                    if suspects:
                        candidates.append((line, chars, text, suspects))
            plumb_page = plumber.pages[index]
            if page.rotation or page.cropbox != page.mediabox or abs(page.rect.height - plumb_page.height) > 1:
                if candidates:
                    self.issues[index].append('Suspicious character mappings on a rotated/cropped page; verify the text visually.')
                continue
            fonts = defaultdict(set)
            for xref, _, _, name, *_ in page.get_fonts(full=True):
                fonts[name].add(xref)
                self.font_names[xref] = name
            traces = defaultdict(set)
            for span in page.get_texttrace():
                for code, glyph, origin, _ in span['chars']:
                    traces[position(chr(code), origin)].add(glyph)
            matches = defaultdict(set)
            for char in plumb_page.chars:
                point = plumber_position(char, plumb_page.height)
                refs = fonts.get(char['fontname'], set())
                glyphs = traces.get(point, set())
                if len(refs) == len(glyphs) == 1:
                    matches[point].add((next(iter(refs)), next(iter(glyphs)), char['text']))
            locations = {point: next(iter(keys)) for point, keys in matches.items() if len(keys) == 1}
            self.locations[index] = locations
            for line, chars, text, suspects in candidates:
                if len(chars) != len(text) or tuple(line['dir']) != (1, 0):
                    self.issues[index].append('Suspicious character mappings could not be aligned; verify the text visually.')
                    continue
                keys = [locations.get(position(char['c'], char['origin'])) for char in chars]
                self.suspects[index].update(keys[i] for i in suspects)
                for word in re.finditer(r'\S+', text):
                    for i in suspects:
                        if word.start() <= i < word.end():
                            self.examples[index][keys[i]].add(word[0])
                lines.append((index, line['bbox'], text, suspects, keys))

        pending = {key for keys in self.suspects.values() for key in keys if key is not None}
        for key in pending:
            replacement = self.font_replacement(document, key)
            if replacement == key[2]:
                self.verified.add(key)
            elif replacement is not None:
                self.corrections[key] = replacement
                self.methods[key] = ('Unicode ligature expansion' if '\ufb00' <= key[2] <= '\ufb06'
                                     else 'embedded font mapping')
        attempts = 0
        for index, box, text, suspects, keys in lines:
            if not any(keys[i] in pending and keys[i] not in self.corrections and keys[i] not in self.verified
                       for i in suspects):
                continue
            if not executable or attempts >= MAX_OCR_LINES:
                if executable:
                    self.ocr_issue = f'Targeted OCR limit of {MAX_OCR_LINES} lines reached.'
                continue
            attempts += 1
            try:
                recognized = ocr_line(document[index], box, executable)
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, UnicodeError):
                self.ocr_issue = 'Targeted OCR failed; check Tesseract and its English language data.'
                executable = None
                continue
            words = list(re.finditer(r'\S+', text))
            if len(words) != len(recognized):
                continue
            for word, (new, confidence) in zip(words, recognized):
                if confidence < MIN_CONFIDENCE:
                    continue
                offsets = {i - word.start() for i in suspects if word.start() <= i < word.end()}
                changes = word_changes(word[0], new, offsets)
                if changes is None:
                    continue
                for offset in offsets:
                    key = keys[word.start() + offset]
                    if key is not None:
                        self.evidence[key][changes.get(offset, word[0][offset])].add(word[0].casefold())
        for key, votes in self.evidence.items():
            if key in self.corrections or key in self.verified or len(votes) != 1:
                continue
            replacement, words = next(iter(votes.items()))
            if len(words) >= 2 and replacement != key[2]:
                self.corrections[key] = replacement
                self.methods[key] = 'targeted OCR agreement across distinct words'
            elif len(words) >= 2:
                self.verified.add(key)

    def font_replacement(self, document, key):
        xref, glyph, original = key
        normalized = unicodedata.normalize('NFKC', original)
        if '\ufb00' <= original <= '\ufb06' and letter_sequence(normalized):
            return normalized
        if xref not in self.font_maps:
            reverse = defaultdict(set)
            try:
                data = document.extract_font(xref)[3]
                if data:
                    font = pymupdf.Font(fontbuffer=data)
                    for code in font.valid_codepoints():
                        if code:
                            reverse[font.has_glyph(code, fallback=False)].add(chr(code))
            except (ValueError, RuntimeError):
                pass
            self.font_maps[xref] = reverse
        characters = self.font_maps[xref].get(glyph, set())
        if original in characters:
            return original
        normalized = {unicodedata.normalize('NFKC', char) for char in characters}
        if original not in characters and len(normalized) == 1:
            value = next(iter(normalized))
            if letter_sequence(value):
                return value
        return None

    def textbox(self, page, box):
        """Use the same glyph mappings for separately extracted PDF link labels."""
        original = page.get_textbox(box)
        if not self.corrections:
            return original
        locations = self.locations.get(page.number, {})
        lines, changed = [], False
        for block in page.get_text('rawdict', clip=box)['blocks']:
            for line in block.get('lines', []):
                text = []
                for span in line['spans']:
                    for char in span['chars']:
                        key = locations.get(position(char['c'], char['origin']))
                        replacement = self.corrections.get(key, char['c'])
                        changed |= replacement != char['c']
                        text.append(replacement)
                lines.append(''.join(text))
        return '\n'.join(lines) if changed else original

    def apply(self, index, blocks, plumb_page):
        locations = self.locations.get(index, {})
        applied = Counter()
        for block in blocks:
            for line in block.get('lines', []):
                for span in line['spans']:
                    expanded = []
                    for char in span['chars']:
                        key = locations.get(position(char['c'], char['origin']))
                        replacement = self.corrections.get(key)
                        if replacement is None:
                            expanded.append(char)
                        else:
                            applied[key] += 1
                            expanded.extend(dict(char, c=value, source_c=char['c']) for value in replacement)
                    span['chars'] = expanded
                    span['text'] = ''.join(char['c'] for char in expanded)
        notes = list(self.issues.get(index, []))
        for key, count in applied.items():
            xref, glyph, old = key
            notes.append(f'Corrected {old!r} to {self.corrections[key]!r} for {self.font_names[xref]} '
                         f'glyph {glyph}: {count} occurrences, verified by {self.methods[key]}. Confirm against the page image.')
        unresolved = self.suspects.get(index, set()) - self.corrections.keys() - self.verified
        if unresolved:
            examples = sorted({word for key in unresolved for word in self.examples[index][key]})[:6]
            notes.append('Suspicious font/character mappings remain unverified; extracted text retained: '
                         + ', '.join(repr(word[:80]) for word in examples) + '. Check against the page image.'
                         + (' ' + self.ocr_issue if self.ocr_issue else ''))
        if not applied:
            return plumb_page, notes
        # FilteredPage owns its object lists; copy character records so the
        # independent source page and the PDF remain unchanged.
        corrected = plumb_page.filter(lambda obj: True)
        corrected.objects['char'] = [
            dict(char, text=self.corrections.get(locations.get(plumber_position(char, plumb_page.height)), char['text']))
            for char in plumb_page.chars]
        return corrected, notes
