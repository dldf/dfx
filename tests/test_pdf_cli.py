"""Check argument forwarding and compatibility with the original commands."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PdfCommandTests(unittest.TestCase):
    def test_help_and_errors_match_existing_scripts(self):
        for command in ('convert', 'review', 'stage', 'diagnose', 'site'):
            for arguments in (['--help'], [], ['--unknown-option']):
                with self.subTest(command=command, arguments=arguments):
                    old = subprocess.run(
                        [sys.executable, '-m', f'dfx.{command}', *arguments],
                        capture_output=True, text=True)
                    new = subprocess.run(
                        [sys.executable, '-m', 'dfx.pdf_cli', command, *arguments],
                        capture_output=True, text=True)
                    self.assertEqual(new.returncode, old.returncode)
                    self.assertEqual(new.stdout, old.stdout)
                    # This dependency warning includes a timestamp and process ID.
                    def command_errors(text):
                        return '\n'.join(line for line in text.splitlines()
                                         if '[W:onnxruntime:Default, telemetry_1ds.cc:' not in line)
                    self.assertEqual(command_errors(new.stderr), command_errors(old.stderr))

    def test_top_level_help_and_invalid_command(self):
        for arguments, code in ((['--help'], 0), ([], 2), (['unknown'], 2)):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, '-m', 'dfx.pdf_cli', *arguments],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, code)
                self.assertIn('convert', result.stdout + result.stderr)
                self.assertIn('stage', result.stdout + result.stderr)
                self.assertIn('review', result.stdout + result.stderr)
                self.assertIn('diagnose', result.stdout + result.stderr)
                self.assertIn('site', result.stdout + result.stderr)

    def test_arguments_input_working_directory_and_exit_code_survive(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            package_root = root / 'project with spaces'
            scripts = package_root / 'dfx'
            scripts.mkdir(parents=True)
            (scripts / '__init__.py').touch()
            shutil.copy2(ROOT / 'src/dfx/pdf_cli.py', scripts / 'pdf_cli.py')
            script = (
                'import json, os, sys\n'
                'print(json.dumps([sys.argv[1:], sys.stdin.readline().strip(), '
                'os.getcwd(), sys.executable]))\n'
                'print("script error", file=sys.stderr)\n'
                'sys.exit(7)\n')
            arguments = ['folder/A $report `draft`.pdf', 'output folder', '--page-mode', '1=svg']
            for command in ('convert', 'review', 'stage', 'diagnose', 'site'):
                with self.subTest(command=command):
                    (scripts / f'{command}.py').write_text(script)
                    result = subprocess.run(
                        [sys.executable, '-m', 'dfx.pdf_cli', command, *arguments],
                        cwd=root, env={**os.environ, 'PYTHONPATH': str(package_root)},
                        input='Test Reviewer\n', capture_output=True, text=True)
                    self.assertEqual(result.returncode, 7)
                    self.assertEqual(json.loads(result.stdout),
                                     [arguments, 'Test Reviewer', str(root), sys.executable])
                    self.assertEqual(result.stderr, 'script error\n')


if __name__ == '__main__':
    unittest.main()
