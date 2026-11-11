# DFX — Document Format eXchange

Convert PDFs and LaTeX into editable Markdown and HTML for review and publication.
DFX runs locally without an AI service or API key. Review converted content before
publishing; conversion alone does not establish accessibility.

## Setup

Install **Python 3.11+**, [Pandoc](https://pandoc.org/installing.html), and
[Chrome](https://www.google.com/chrome/) or Chromium. Pandoc must be on your `PATH`.

```sh
git clone https://github.com/dldf/dfx.git
cd dfx
```

On macOS or Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Run commands from the project folder. Activate the environment in each new
terminal, or use `.venv/bin/dfx` on macOS/Linux and `.\.venv\Scripts\dfx.exe`
on Windows. Recreate `.venv` if you move the project folder.

If Chrome is not found, add `--chrome` with its executable path to `convert`,
`review`, `rebuild`, or `site audit`. For example:

```powershell
dfx review "2_review/document" --chrome "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

The workflow has been tested on macOS. Windows and Linux have not been verified
end to end.

## Convert

Place source files in `1_download/`, then convert one PDF or a folder of PDFs:

```sh
dfx convert "1_download/document.pdf"
dfx convert 1_download
```

A single `.tex` file is also accepted. Output goes into matching folders under
`2_review/`. Each document contains:

| File | Purpose |
| --- | --- |
| `content.md` | Editable content and source page markers |
| `index.html` | HTML with embedded styles |
| `assets/` | Figures and other page assets |
| `review/log.md` | Source links and issues to check |
| `review/p001-original.png`, `review/p001-revised.png`, … | Page comparisons |

Existing document folders are protected from overwriting. To use another output
root, supply it as the second argument. Quote paths containing spaces.

DFX extracts PDF text directly. Optional Tesseract checks can verify suspect
character mappings; scanned pages still need OCR or transcription.

### Page overrides

For a single PDF, choose `auto`, `markdown`, or `svg` for individual pages:

```sh
dfx convert "1_download/document.pdf" --page-mode 1=markdown --page-mode 3=svg
```

Automatic classification preserves diagram pages as SVG. SVG text is outlined;
add a text equivalent where needed. Page numbers are 1-based.

## Review and edit

Read `review/log.md`, compare the page images, and edit `content.md`. The log's
**Possible Issue** column explains each flag; **Check** gives text to find in the
Markdown.

Refresh the HTML and comparison images after editing:

```sh
dfx review "2_review/document" --inspect
```

`--inspect` opens the HTML in your browser. Review reading order, wording, tables,
image descriptions, keyboard access, and screen-reader output. Keep source page
markers, anchors, and image description IDs intact.

The review command locates the original source and checks its recorded hash.
If it cannot locate the source, it asks for the path.

### Editing details

- **Images:** replace the placeholder inside `![description](assets/image.png)`
  with meaningful alt text. Each image needs a unique `<!-- description: ID -->`.
- **Tables:** the converter uses Markdown pipe tables and can split complicated
  structures into multiple tables. Ambiguous cell relationships are preserved as
  images for review. Check captions and column associations.
- **Table layout:** edit `<!-- table: {"widths": [40, 60], "row_header": 1} -->`.
  Widths are proportions; `[]` uses automatic layout. Set `row_header` to `0` when
  the first column contains ordinary data.
- **Fonts:** set `font: serif` or `font: sans` in the Markdown's YAML front matter.

## Rebuild from downloaded sources

Reconvert the documents already present in `2_review/`:

```sh
dfx rebuild 2_review
```

A subfolder or individual document folder also works. Rebuild replaces Markdown
edits, assets, logs, HTML, and previews using the matching sources in `1_download/`.
It does not make backups. Use `dfx review` to render edits without reconverting.

## Stage for publication

After the final review, stage every document under `2_review/`:

```sh
dfx stage 2_review
```

You can also supply a subfolder or one document. The command asks for your name
once, then:

- Moves verified HTML and assets into matching folders under `3_publish/`.
- Keeps `content.md` and `review/log.md` in `2_review/`, updating links and recording
  the reviewer in the log.
- Removes generated previews after verifying the staged copy.
- Skips existing destinations and reports failures while processing other documents.

Staging prepares local files; upload the contents of `3_publish/` to publish them.

## Website tools

Discover PDFs within a website folder and download them after confirmation:

```sh
dfx site scan "https://example.edu/department/"
dfx site tree
dfx site download
```

Downloads preserve the hostname and website path under `1_download/`. Files already
in the workflow are skipped. `site tree` shows the highest local stage:
**[1] Downloaded**, **[2] Pending Review**, or **[3] Ready to Publish**.

Scans follow `robots.txt` and visit up to 9999 URLs by default. Use `--max-urls` to
change the limit, `--scan-only` to save results without offering downloads, or
`--folders` to list PDF folders and counts without saving a scan. Unlinked files
and JavaScript-generated links may be missed.

To exclude folders from PDF scans and downloads, put their full URLs in `.pdfignore`:

```text
https://example.edu/department/archive/
https://example.edu/department/old-reports/
```

Each entry excludes that folder and its descendants. Blank lines and `#` comments
are allowed; wildcards are not supported.

### Accessibility scores

With Node.js 22.19+ and Chrome/Chromium installed:

```sh
npm install --prefix .venv/lighthouse lighthouse@13.6.0
dfx site audit
dfx site scores --below 70
```

Audit runs Lighthouse on HTML pages in the most recently scanned website scope.
Both commands accept an optional scope URL. Omit `--below 70` to show all saved
scores. Automated scores do not establish accessibility conformance.

Scan metadata is stored in `data/pdfs.json`; audit results are in
`data/page_scores.json`. Local data, generated documents, downloads, and virtual
environments are excluded from Git.

## Troubleshooting

- **`dfx: command not found`:** activate `.venv` and run `python -m pip install -e .`.
- **Missing Pandoc or browser:** check `pandoc --version` and supply `--chrome`
  with the browser executable path.
- **Preview failed:** the converted Markdown remains available. Fix the reported
  issue and run `dfx review` again.
- **Certificate errors during scanning:** repair your Python certificate setup.
  On macOS, python.org installations include `Install Certificates.command`.

Run `dfx <command> --help` for options. More examples are in the
[macOS/Linux](docs/commands_mac.md) and [Windows](docs/commands_win.md) command guides.
