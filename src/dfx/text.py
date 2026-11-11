"""Shared PDF text cleanup and safe Markdown/HTML formatting."""
from html import escape as html_escape
import re


def clean(text):
    return ' '.join(re.sub(r'-\s*\n\s*', '-', text or '').split())


def escape(text):
    return re.sub(r'([\\`*_\[\]<>|])', r'\\\1', clean(text))


def format_text(text, html=False):
    """Mark form blanks without interpreting source text as markup."""
    if html:
        return re.sub(r'_{4,}', '<span class="blank">---</span>', html_escape(clean(text)))
    return re.sub(r'(?:\\_){4,}', '[---]{.blank}', escape(text))
