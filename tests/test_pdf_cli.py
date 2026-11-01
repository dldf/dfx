"""Check argument forwarding and compatibility with the original commands."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PdfCommandTests(unittest.TestCase):
    def test_help_and_errors_match_existing_scripts(self):
        for command in ('convert', 'review', 'stage'):
            for arguments in (['--help'], [], ['--unknown-option']):
                with self.subTest(command=command, arguments=arguments):
                    old = subprocess.run(
                        [sys.executable, str(ROOT / f'{command}.py'), *arguments],
                        capture_output=True, text=True)
                    new = subprocess.run(
                        [sys.executable, str(ROOT / 'pdf_cli.py'), command, *arguments],
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
                    [sys.executable, str(ROOT / 'pdf_cli.py'), *arguments],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, code)
                self.assertIn('convert', result.stdout + result.stderr)
                self.assertIn('stage', result.stdout + result.stderr)
                self.assertIn('review', result.stdout + result.stderr)

    def test_arguments_input_working_directory_and_exit_code_survive(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            scripts = root / 'project with spaces'
            scripts.mkdir()
            shutil.copy2(ROOT / 'pdf_cli.py', scripts / 'pdf_cli.py')
            script = (
                'import json, os, sys\n'
                'print(json.dumps([sys.argv[1:], sys.stdin.readline().strip(), '
                'os.getcwd(), sys.executable]))\n'
                'print("script error", file=sys.stderr)\n'
                'sys.exit(7)\n')
            arguments = ['folder/A $report `draft`.pdf', 'output folder', '--page-mode', '1=svg']
            for command in ('convert', 'review', 'stage'):
                with self.subTest(command=command):
                    (scripts / f'{command}.py').write_text(script)
                    result = subprocess.run(
                        [sys.executable, str(scripts / 'pdf_cli.py'), command, *arguments],
                        cwd=root, input='Test Reviewer\n', capture_output=True, text=True)
                    self.assertEqual(result.returncode, 7)
                    self.assertEqual(json.loads(result.stdout),
                                     [arguments, 'Test Reviewer', str(root), sys.executable])
                    self.assertEqual(result.stderr, 'script error\n')


if __name__ == '__main__':
    unittest.main()
