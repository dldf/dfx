"""Lighthouse accessibility audits and saved page-score trees."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

from . import site


def score_path():
    return site.ROOT / 'data' / 'page_scores.json'


def load_scores():
    try:
        data = json.loads(score_path().read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'version': 1, 'scans': {}}
    if data.get('version') != 1 or not isinstance(data.get('scans'), dict):
        raise ValueError('Expected page_scores.json version 1 with a scans object.')
    return data


def lighthouse_command():
    # A local install keeps Node packages out of the user's global environment.
    local = Path(sys.prefix) / 'lighthouse' / 'node_modules' / 'lighthouse' / 'cli' / 'index.js'
    node = shutil.which('node')
    if local.is_file() and node:
        return [node, str(local)]
    executable = shutil.which('lighthouse')
    if executable and node:
        return [executable]
    raise ValueError('Lighthouse requires Node.js >=22.19 and Chrome/Chromium. '
                     'Install locally with: npm install --prefix .venv/lighthouse lighthouse@13.6.0')


def page_score(command, url, client, chrome=None):
    if not client.allowed(url):
        raise ValueError('Page is outside scope, excluded, or disallowed by robots.txt.')
    env = os.environ.copy()
    if chrome:
        env['CHROME_PATH'] = chrome
    with tempfile.TemporaryDirectory(prefix='dfx-lighthouse-') as temporary:
        report = Path(temporary) / 'report.json'
        result = subprocess.run([
            *command, url, '--only-categories=accessibility', '--output=json',
            f'--output-path={report}', '--chrome-flags=--headless', '--quiet',
        ], capture_output=True, text=True, timeout=120, env=env)
        if result.returncode:
            raise ValueError(f'Lighthouse failed: {(result.stderr.strip() or result.stdout.strip())[-500:]}')
        data = json.loads(report.read_text(encoding='utf-8'))
    if data.get('runtimeError'):
        raise ValueError(f'Lighthouse failed: {data["runtimeError"].get("message", "runtime error")}')
    score = data.get('categories', {}).get('accessibility', {}).get('score')
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
        raise ValueError('Lighthouse did not return an accessibility score.')
    final = site.canonical(data.get('finalDisplayedUrl') or data.get('finalUrl') or url)
    if not client.allowed(final):
        raise ValueError('Lighthouse redirected outside the allowed scope; score discarded.')
    return {'score': int(score * 100 + .5), 'final_url': final,
            'lighthouse_version': data.get('lighthouseVersion')}


def audit(scope, limit, *, chrome=None, interactive=False):
    command = lighthouse_command()
    data = load_scores()
    # Replace this scope's previous run; other website scopes remain intact.
    run = {'audited_at': site.now(), 'finished_at': None, 'pages': {}}
    data['scans'][scope] = run

    def record_page(url, client):
        if url in run['pages']:
            return
        print(f'Auditing: {url}', flush=True)
        entry = {'audited_at': site.now(), 'score': None}
        try:
            entry.update(page_score(command, url, client, chrome))
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            entry['error'] = str(exc)
        run['pages'][url] = entry
        site.save_json(score_path(), data)
        print(f'  ({entry["score"] if entry["score"] is not None else "error"})'
              + (f' {entry["error"]}' if 'error' in entry else ''), flush=True)

    summary = site.scan({}, scope, limit, on_page=record_page, interactive=interactive)
    run.update(summary)
    run['finished_at'] = site.now()
    site.save_json(score_path(), data)
    failures = sum(entry['score'] is None for entry in run['pages'].values())
    print(f'Saved {len(run["pages"])} HTML page results to data/page_scores.json; {failures} audit errors.')
    print('Run dfx site scores to display the saved scores.')


def scores(scope):
    run = load_scores()['scans'].get(scope)
    if run is None:
        raise ValueError('No audit saved for this scope. Run dfx site audit first.')
    pages = run['pages']
    print(f'{scope}    {len(pages)} HTML pages    Audited: {run["audited_at"]}')
    # Separate leaves from directories so /about and /about/team can coexist.
    root = {'folders': {}, 'pages': []}
    for url, entry in sorted(pages.items()):
        parts = urlsplit(url)
        relative = parts.path[len(urlsplit(scope).path):]
        segments = relative.split('/')
        branch = root
        for segment in segments[:-1]:
            if segment:
                branch = branch['folders'].setdefault(unquote(segment), {'folders': {}, 'pages': []})
        name = unquote(segments[-1]) or '/'
        if parts.query:
            name += '?' + parts.query
        score = entry.get('score')
        label = f'({score if score is not None else "error"}) {name}'
        branch['pages'].append((name, label, None))

    def display(branch, prefix=''):
        items = [(name, name + '/', children) for name, children in branch['folders'].items()]
        items.extend(branch['pages'])
        items.sort(key=lambda item: (item[0], item[1]))
        for index, (_, label, children) in enumerate(items):
            last = index == len(items) - 1
            print(prefix + ('└── ' if last else '├── ') + label)
            if children is not None:
                display(children, prefix + ('    ' if last else '│   '))

    display(root)
    if not run.get('finished_at'):
        print('Audit interrupted; only completed page attempts are shown.')
    if run.get('limited'):
        print(f'Discovery stopped at --max-urls {run["max_urls"]}; increase the limit and audit again.')
    if run.get('errors'):
        print(f'{len(run["errors"])} URLs could not be scanned; see errors in data/page_scores.json.')
    for url, entry in pages.items():
        if entry.get('error'):
            print(f'Audit error: {url}: {entry["error"]}')
    print('Scores are out of 100 and reflect automated accessibility checks, not complete accessibility conformance.')
