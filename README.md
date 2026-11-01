# pdf2md

Convert PDFs into editable Markdown drafts, with HTML previews and paired
original/revised page images for review. An optional website scanner discovers
PDFs, downloads them with confirmation, and tracks review status.

The tools run locally without an AI service or API key. Conversion infers document
structure; it does not perform OCR, automatically reconstruct diagrams, or certify
accessibility. Review the Markdown against the original before publishing it.

## Requirements

- **Python 3.11 or newer**, with `pip` and `venv`. See the
  [Python downloads](https://www.python.org/downloads/) and
  [virtual-environment instructions](https://docs.python.org/3/library/venv.html).
- **Pandoc**, available on your `PATH`. Pandoc 3.8 is the tested version;
  use a recent release for grid tables with merged cells. Follow the
  [Windows, macOS, or Linux installation instructions](https://pandoc.org/installing.html).
- **Google Chrome or Chromium**, used in headless mode to make review images.
  Install [Chrome](https://www.google.com/chrome/) or use your Linux distribution's
  Chromium package.
- **Git**, if cloning the repository. Downloading and extracting its ZIP is another option.

The workflow has been tested on macOS with Python 3.14, Pandoc 3.8, and Chrome.
Windows and Linux setup commands are provided below; those platforms have not
been verified end to end.

## Get the project

```sh
git clone https://github.com/dldf/pdf2md.git
cd pdf2md
```

If you downloaded a ZIP, open a terminal in its extracted project folder instead.

### macOS and Linux

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Some Linux distributions package `venv` separately; install the package for your
Python version if environment creation reports that it is missing.

### Windows PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

If PowerShell blocks activation, use Command Prompt instead:

```bat
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -e .
```

Activation is optional: substitute `.venv/bin/pdf` on macOS/Linux or
`.\.venv\Scripts\pdf.exe` on Windows for `pdf`. For commands starting with
`python`, use `.venv/bin/python` or `.\.venv\Scripts\python.exe`.
This avoids changing PowerShell execution policies. If `py` is unavailable,
use the command for your installed Python 3.11+ interpreter.

Run `python --version` and `pandoc --version` to check setup. Reactivate the
virtual environment in each new terminal. Recreate `.venv` if you move or rename
the project folder; virtual environments contain absolute paths.

The install command reads dependencies from `pyproject.toml` and adds `pdf` to
the environment. Existing users should run `python -m pip install -e .` once.
The editable installation uses the code in this project folder.

Existing commands such as `python convert.py "1_downloads/document.pdf"` and
`python stage.py "2_pending_review/document/content.md"` still work with the
same arguments and options.

## Convert a PDF

Place a PDF at `1_downloads/document.pdf`, or substitute the path to your own file.
Quote paths containing spaces. From the project folder, run:

```sh
pdf convert "1_downloads/document.pdf"
```

This creates `2_pending_review/document/` containing:

| Path | Purpose |
| --- | --- |
| `content.md` | Document content, invisible page comments, and page anchors |
| `assets/` | Always present; extracted figures and `main.css` |
| `review/log.md` | Document guidance and page-specific review notes |
| `index.html` | Browser preview generated from Markdown |
| `review/p001-original.png`, `review/p001-revised.png`, ... | Paired page images |

Reusable command examples are in [the macOS/Linux guide](docs/commands_mac.md)
and [the Windows guide](docs/commands_win.md). These shared guides replace
per-document command files in `review/`.

Conversion generates the initial HTML and paired review images automatically.
Styles are linked from `assets/main.css`; inline style attributes are retained.
After each successful conversion, the terminal prints a `pdf review` command to
copy and paste. It does not wait for confirmation or run the next step.
A folder input converts
PDFs recursively. The default output root is `2_pending_review/`. Converting a
PDF or subfolder within `1_downloads/` preserves its path relative to that root
when writing to `2_pending_review/`, so single and batch conversions agree.
Existing document folders are protected from overwriting; choose a custom output
root for another conversion. Custom roots are outside automatic status tracking.
If preview generation fails after extraction, the saved Markdown remains available.

### Page classification and SVG overrides

Conversion automatically keeps ordinary prose, tables, and mixed pages as
Markdown. Obvious diagram/artwork pages are preserved as one whole-page SVG in
`assets/`, with an image reference at the original page position. SVG text is
outlined to preserve appearance; it is not selectable, accessible text. Add a
meaningful description or text equivalent before publication. Page markers and
anchors remain in the Markdown.

For a single PDF, override individual pages using 1-based page numbers:

```sh
pdf convert "1_downloads/document.pdf" --page-mode 1=svg
pdf convert "1_downloads/document.pdf" "2_pending_review/retry" --page-mode 1=markdown --page-mode 3=svg
```

Modes are `auto` (the default), `markdown` (the existing extraction path), and
`svg` (the entire page, with no separate text, tables, or image fragments).
For example, `--page-mode 1=auto` explicitly selects automatic classification.
Repeat the option for different pages; each page may be specified once. Overrides
require a single PDF input, not a folder. Choose a fresh output root when retrying;
existing conversion folders are never overwritten.

Classification combines aligned paragraph-length text, short labels distributed
across the page, their overlap with shapes, connections between shapes, composition
coverage, and table-cell geometry. Starting thresholds include at least eight
substantial shapes, three connections, and 50% composition coverage for a labelled
diagram, together with predominantly short, scattered labels inside shapes.
Aligned prose and regular multi-row table cells weigh against automatic SVG.
Extensive unlabelled vector artwork uses a separate geometry check. These are
conservative heuristics calibrated on assessment reports and an advising diagram,
not reliable rules for every PDF; count or text presence alone is insufficient.

Uncertain pages retain their current Markdown and get a comparison SVG linked
from `review/log.md`, without adding a duplicate page image to the Markdown.
The log records the classification evidence and required review. Mixed-page
figures still use the existing extraction behavior; this does not segment figures.
False negatives and unusual layouts remain possible; use an override after review.

A nearly full-page bitmap with little extractable text is treated as a possible
scan, not automatically as vector artwork. Its existing image handling is retained
and the log explicitly reports that this converter has no OCR stage. OCR or
transcription is needed; a bitmap alone cannot establish whether it contains prose
or artwork. All classification and export run locally without AI API calls or
additional dependencies.

### Selecting Chrome or Chromium

The tools look for `google-chrome` or `chromium` on `PATH`, then the usual macOS
Chrome application. On Windows, or for another installation location, supply
`--chrome` to both conversion and preview commands. For a common Windows install:

```powershell
pdf convert "1_downloads/document.pdf" --chrome "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

Use the actual executable path on your computer. A per-user Windows installation
or a Linux executable named `chromium-browser` may need a different path.

## Review and edit

Open `review/log.md` for review guidance and the list of flagged pages. Each flagged
page lists what to check, with links to the source PDF page and the Markdown page anchor.
A flag means the converter found something that needs human review; it does not
necessarily mean the conversion is wrong.

Edit `2_pending_review/document/content.md`, then refresh the preview:

```sh
pdf review "2_pending_review/document/"
```

On Windows, include the browser path again:

```powershell
pdf review "2_pending_review/document/" --chrome "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

The review command locates the original PDF under `1_downloads/` using the
matching document path. If that does not identify the source, it searches
`2_pending_review/` recursively for PDFs with the document folder's name.
For example, `2_pending_review/reports/file/content.md` matches
`1_downloads/reports/file.pdf`. PDF extensions such as `.PDF` are also supported.
The recorded source hash validates candidates and distinguishes different PDFs
with the same name. Without a hash, multiple matches require your input.

If automatic lookup fails, the command prompts for the original PDF's path.
Enter a path (quoted paths are accepted), or press Enter without typing anything
to cancel. Ctrl+C also cancels during lookup. Lookup and cancellation happen
before any review files are changed. Noninteractive runs stop with an error when
a path is needed. Custom or renamed document folders may require the prompt.
There is no `--source` option.

You can also run `python review.py "2_pending_review/document/"`.
This replaces the old `python preview.py PDF MARKDOWN` command.

Folder arguments select the `content.md` inside that document folder; explicit
Markdown paths are also accepted. Review rebuilds HTML and paired images without
editing Markdown or prompting for titles or image descriptions. After successful
review of a document under `2_pending_review/`, the terminal prints the `pdf stage`
command to copy and paste. It does not run staging automatically.

Append `--inspect` to open the saved HTML, or open `index.html` yourself.
It remains available after the command finishes. Rerun preview and refresh the
browser after editing Markdown.

Sort the PNGs by filename in an image viewer to compare original/revised pairs.
Revised pages may be taller to preserve content. Check reading order, wording,
lists, tables, and image descriptions; exact visual matching is not the goal.
Use the HTML for keyboard and screen-reader review. PNGs cannot verify those
interactions, and a successful conversion does not establish accessibility.

Keep the `<!-- Source page N -->` markers and page anchors in the Markdown.
Keep any `assets/` directory beside `content.md`. Preview generation preserves
Markdown and checks that the source PDF matches the recorded hash.

### Fonts, emphasis, and line breaks

The converter preserves detected bold and italic text, represents detected highlights
with `<mark>`, and infers serif or sans
from the predominant body font. You can override the choice at the top of Markdown:

```yaml
---
font: serif
---
```

Use `serif` or `sans`. This selects a family, not an exact PDF font. Intentional
line breaks are inferred conservatively; ambiguous cases are flagged for review.
Nested lists and unusual layouts may still need manual correction.

### Images

Each figure has a stable description ID. Find it in Markdown and replace the
placeholder inside `![...]` with a meaningful description:

```md
<!-- description: desc-001 -->

![A description of what the figure communicates.](assets/figure-1.png)
```

Keep IDs unique. Add an unused ID immediately before each manually added image.
Descriptions become image alt text. Reviewer-only labels are not added to the HTML;
use `review/log.md` for review guidance.

### Tables

The converter prefers pipe tables, then grid tables for merged cells, and HTML
when a grid cannot preserve the extracted structure. Existing captions are
preserved; otherwise a detected first column heading supplies the caption.
Confirm that the resulting title describes the table.

Table metadata is editable:

```md
<!-- table: {"widths": [25, 25, 30, 10, 10], "row_header": 1} -->
```

Widths are proportions, one per actual column; `[]` permits automatic layout.
For simple tables, `row_header: 1` makes the first column row headers; use `0` for
ordinary data. Pandoc captions use a line such as `: Table: Program Learning Outcomes`
above the pipe table, separated by a blank line. Captions above or below the table
render identically in HTML. Grid-table borders must stay aligned in a monospaced editor.

Merged cells and complex header relationships still need human review. Simplify
where appropriate, or use HTML with suitable `th`, `scope`, `rowspan`, `colspan`,
and `id`/`headers` associations. Image-only or undetected tables need reconstruction.

## Optional: discover PDFs on a website

Replace `https://example.edu/department/` with the website folder you want to search:

```sh
python site.py scan "https://example.edu/department/"
python site.py tree
python site.py download
```

These commands:

1. Follow links within the website folder to discover PDFs, then offer to download them. Add `--scan-only` to skip the download prompt, or `--max-urls 100` to limit the scan to 100 visited URLs.
2. Display discovered PDFs and their local workflow status in a tree diagram.
3. Ask for confirmation before downloading PDFs into the local `1_downloads/` folder. This step is unnecessary if you already downloaded them during the scan.

The scan visits up to **250 URLs by default**, including pages and PDFs. Increase `--max-urls` to search further. It follows the website’s automated-access rules in `robots.txt` and may miss unlinked files or links generated by JavaScript, so it cannot guarantee finding every PDF.

The older `--max-pages` option remains an alias for `--max-urls`.
Downloads go under `1_downloads/<hostname>/<website-path>/`. Files already present
in the tracked workflow are skipped. Enter `y` to confirm downloading; Enter or
`n` cancels.

Scan results and download metadata are saved in local `inventory.json`. That file,
new PDF downloads, generated documents, and virtual environments are excluded from Git.
Existing inventory status fields are ignored; old history may remain for reference.

### List PDF folders without saving a scan

```sh
python site.py scan "https://example.edu/department/" --folders --max-urls 100
```

This lists only folders containing PDFs found during this scan, with a count for
each folder. Each unique PDF URL counts once in its immediate folder, not in
parent folders. It does not print individual PDF URLs or a status tree, save or
change `inventory.json`, or offer downloads. It works without an inventory file.
The scan still follows links; limits, excluded folders, and inaccessible pages can
make the listing incomplete. Run a normal scan later to save results for downloading.

### Exclude website folders

Create `.pdfignore` in the `pdf2md` project root. Replace these example URLs with
full URLs of folders to skip:

```text
# Skip these folders and everything beneath them
https://example.edu/department/archive/
https://example.edu/department/old-reports/
```

Blank lines and lines beginning with `#` are ignored. Each entry is a literal
folder URL; wildcards, query strings, and fragments are not supported. A trailing
slash is optional. `/archive/` excludes that folder and its descendants, but not
`/archive-old/`. Rules apply to the specified scheme and host.

Exclusions apply to normal scans, folder summaries, downloads, and redirects.
Previously saved records and downloaded files are kept; excluded records can
still appear in `site.py tree`, but are not offered for download. Excluding the
scan root stops the scan with an error. If `.pdfignore` is absent, nothing is
excluded beyond the existing scope and `robots.txt` rules.

### Track review status

```sh
python site.py tree
```

| Status | Determined by file presence |
| --- | --- |
| [1] Downloaded | PDF in `1_downloads/`, with no matching Markdown in later stages |
| [2] Pending Review | Markdown in the matching `2_pending_review/` document folder, but none in ready |
| [3] Ready to Publish | `index.html`, `assets/main.css`, and both archived Markdown files in the matching `3_ready_to_publish/` folder; legacy root-level Markdown remains recognized |

The highest stage present wins. A folder or receipt alone does not establish a
stage. URLs discovered without local files appear unnumbered as “not downloaded.”
There are no manual status commands. Custom output roots remain usable for conversion,
but are outside automatic status tracking and `stage.py`.

## Prepare a reviewed document for publication

Refresh and inspect the preview after your final Markdown edits, then run:

```sh
pdf stage "2_pending_review/document/"
```

The command prompts for your name; the local date, time, and UTC offset are generated.
For new conversions, it copies the reviewed files into the matching folder under
`3_ready_to_publish/` with this layout:

```text
document/
├── index.html
└── assets/
    ├── main.css
    ├── … figures …
    └── _pdf2md/
        ├── content.md
        └── log.md
```

It verifies the copies before removing originals. HTML, CSS, and images remain
unchanged. Relative links in the archived Markdown and log are adjusted for their
new locations; their other content is preserved. Staging does not rebuild HTML
or publish anything. `assets/_pdf2md/` is reserved for the archived source and log.
The HTML is installed last so incomplete copies do not appear ready to publish.

After success the original `review/` folder is deleted. Its `log.md`, including any
notes added there, is preserved in `assets/_pdf2md/log.md`; review PNGs and other
files in `review/` are removed. A two-line `ready_to_publish.txt` remains in the
pending document folder:

```text
Reviewed by "NAME"; on "YYYY-MM-DD" at "HH:MM:SS UTC_OFFSET".
file:///absolute/path/to/3_ready_to_publish/document/
```

Missing required files, blank names, existing receipts, and destination conflicts stop
the operation. Failures before installation leave originals intact. If installation,
receipt creation, or cleanup is interrupted, the error names the affected location and
what remains; do not overwrite an existing destination to retry. Completed ready copies
are retained, and interrupted cleanup can leave duplicate pending files.

Existing conversions are not automatically migrated. A document that still has
`preview.html` and no `index.html` keeps its legacy layout when explicitly reviewed
or staged: staging retains the previous root-level Markdown and `frompdf.html`
behavior. Previously staged documents remain recognized by status tracking.

Old `input/` and `output/` folders are not renamed automatically. Existing conversions
may retain old review annotations and banners; the verification command does not strip
them. Newly generated documents keep review guidance in `review/log.md` instead.

## Troubleshooting

- **Missing Pandoc or browser:** check `pandoc --version` and supply `--chrome`
  with the full executable path. Python requirements do not install these programs.
- **`CERTIFICATE_VERIFY_FAILED` during scanning:** repair the certificate setup
  for your Python installation. On macOS, python.org installers provide an
  `Install Certificates.command` in that Python version's Applications folder.
  On managed computers, ask your IT team about required certificate authorities.
- **Existing output folder:** select a new output root for reconversion. Use
  `pdf review` when you only want to render Markdown edits.
- **Little or no extracted text:** this converter has no OCR stage. Obtain a
  text-bearing PDF or transcribe/OCR the content separately before review.

Run `pdf convert --help`, `pdf review --help`,
`python site.py --help`, or `pdf stage --help` for command options.
