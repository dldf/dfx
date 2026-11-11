# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project overview

Tooling to make the Montana State University (MSU) Gianforte School of Computing web content accessible and compliant with
ADA Title II requirements. The target standard is **WCAG 2.1 Level AA**.

DFX (Document Format eXchange) is a local CLI that discovers PDFs on a department
website, converts PDFs (and LaTeX) into editable Markdown and accessible HTML, and
supports a human review → staging workflow before the HTML is uploaded in place of
the PDF. It also runs Lighthouse accessibility audits on site HTML pages. No AI
service or API key is involved; conversion alone does not establish accessibility.

**Owner:** Daniel DeFrance (CS faculty)
**Users of the output:** human reviewers who edit `content.md` and sign off at staging;
department web editors who upload `3_publish/` to the site.

## Tech stack

- Language / runtime: Python 3.11+ (package `dfx` under `src/`, distribution name
  `pdf2md`, console script `dfx`). Node.js 22.19+ only for Lighthouse audits.
- Key libraries: PyMuPDF (`pymupdf`), pdfplumber, BeautifulSoup4; standard-library
  `urllib`/`RobotFileParser` for crawling.
- External tools (subprocess): Pandoc (Markdown/LaTeX ↔ HTML via JSON AST), Chrome or
  Chromium (headless PNG previews, Lighthouse), optional Tesseract (glyph-mapping checks),
  Lighthouse 13.6.0 installed into `.venv/lighthouse`.
- Where the site content lives: the live department website; PDFs are discovered by
  crawling a scope URL and downloaded into `1_download/<host>/<path>`.
- Output format: per-document folder with `content.md`, self-contained `index.html`,
  `assets/`, `review/log.md` (issues to check), and page-comparison PNGs. Scan and
  audit metadata in `data/pdfs.json` and `data/page_scores.json`; trees printed to
  the terminal.

## Commands

```bash
# install (Pandoc and Chrome must be on the system separately)
python3 -m venv .venv && source .venv/bin/activate && python -m pip install -e .
npm install --prefix .venv/lighthouse lighthouse@13.6.0   # only for site audit

# website: discover PDFs (respects robots.txt and .pdfignore), view status, download
dfx site scan "https://example.edu/department/" --scan-only   # --max-urls N, --folders
dfx site tree
dfx site download
dfx site audit                 # Lighthouse a11y scores for HTML pages in latest scope
dfx site scores --below 70

# document workflow
dfx diagnose "1_download/document.pdf"      # explain page classification (review/diagnostic.md)
dfx convert "1_download/document.pdf"       # or a folder, or a .tex; --page-mode N=markdown|svg
dfx review "2_review/document" --inspect    # re-render HTML/PNGs from edited content.md
dfx rebuild 2_review                        # reconvert from sources; DISCARDS Markdown edits
dfx stage 2_review                          # move verified HTML/assets to 3_publish/

# tests (unittest; no pytest config)
python -m unittest discover -s tests -q
python -m unittest tests.test_pipe_tables -q   # single module

# lint / format: none configured
```

`dfx <command>` is a thin dispatcher (`pdf_cli.py`) that runs `python -m dfx.<command>`;
use `dfx <command> --help` for options.

## Repository layout

- `src/dfx/` — the package
  - `pdf_cli.py` — `dfx` entry point; dispatches to the modules below
  - `convert.py` — PDF/LaTeX → Markdown + HTML (largest module; uses Pandoc)
  - `page_classification.py` — per-page heuristics choosing Markdown vs. SVG vs. scan
  - `diagnose.py` — Markdown report explaining classification checks for a PDF
  - `tables.py` — table geometry, header detection, continuation across pages
  - `glyphs.py` — conservative recovery of broken PDF character mappings (optional Tesseract)
  - `text.py` — shared text cleanup and Markdown/HTML escaping
  - `review.py`, `review.css` — HTML and PNG previews via Chrome; never edits Markdown
  - `review_log.py` — builds `review/log.md` issues with search strings
  - `rebuild.py` — reconvert existing review folders from `1_download/`
  - `stage.py` — verify and move HTML/assets to `3_publish/`, record reviewer
  - `site.py` — crawler, PDF inventory, status tree, downloads, `.pdfignore`
  - `site_audit.py` — Lighthouse audits and saved score trees
  - `workflow.py` — project root and stage folder names shared by all commands
- `tests/` — `unittest` suite (`test_*.py`), mostly building small PDFs/inputs in code
- `docs/` — user command guides for macOS/Linux and Windows
- `1_download/`, `2_review/`, `3_publish/` — workflow stages (gitignored, local only)
- `data/` — scan and audit JSON (gitignored)
- `.pdfignore` — folder URLs excluded from scans/downloads (gitignored, local)
- `AGENTS.md` — working constraints and git sync rules; follow them

## Accessibility principles for this project

- **WCAG 2.1 AA is the bar.** Cite the specific success criterion (e.g. "1.1.1 Non-text
  Content", "1.4.3 Contrast (Minimum)") in findings, code comments, and reports.
- **Automated checks are not enough.** Tools like axe-core catch only a fraction of
  issues. Clearly separate in reports:
  - *Violations*: automated, high-confidence failures
  - *Needs review*: items a human must judge (alt text quality, link purpose,
    reading order, caption accuracy)
- **Don't invent fixes that change meaning.** When suggesting alt text, link text, or
  headings, mark them as *suggestions* for a human editor, not final copy.
- **Decorative vs. informative images:** decorative images get `alt=""`; never pad
  alt text with "image of" or file names.
- **Prioritize by impact:** blocking issues first (keyboard traps, missing form labels,
  missing alt on informative images, inaccessible PDFs with key info), then contrast and
  structure, then minor issues.
- **PDFs and other documents** (syllabi, forms, handbooks) are in scope and often the
  largest backlog. Flag untagged/scanned PDFs explicitly.
- **Third-party content** (embedded videos, external widgets) should be reported
  separately, since fixes may be outside the department's control.

## Coding conventions

- Keep the scanner polite: respect `robots.txt` (including crawl-delay), stay inside
  the scope URL, honor `.pdfignore`, and only crawl MSU sites we're responsible for.
- Any HTML this project generates (reports, dashboards) must itself pass WCAG 2.1 AA.
- Add a `unittest` case in `tests/` for every converter or classification change (one
  passing, one failing case where applicable). Run the smallest relevant test module.
- Converter heuristics should stay conservative: when unsure, preserve content (e.g. as
  an image or SVG) and add a `review/log.md` issue rather than guessing structure.
- Never overwrite existing document folders or publish destinations; `rebuild` is the
  only command that replaces review edits.
- Prefer small, reviewable changes. Explain the WCAG rationale in PR descriptions.

## Working with me

- I teach CS, so I'm comfortable with technical detail. Be direct and concise.
- Ask before running a full-site crawl or anything that sends many requests to
  production servers.
- Don't commit generated reports or scraped page content.
- When unsure whether something is a real WCAG failure, say so and cite the criterion
  rather than guessing.
