"""Short commands for the existing conversion, review, and staging scripts."""
import argparse
from pathlib import Path
import subprocess
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='pdf', description='Diagnose and convert PDFs, review previews, and stage reviewed documents. Run from the project folder.',
        epilog='Use pdf diagnose --help, pdf convert --help, pdf review --help, or pdf stage --help for script options.')
    parser.add_argument('command', choices=('convert', 'review', 'stage', 'diagnose'))
    parser.add_argument('arguments', nargs=argparse.REMAINDER, help='Arguments passed to the selected script')
    args = parser.parse_args(argv)
    script = Path(__file__).resolve().with_name(f'{args.command}.py')
    try:
        return subprocess.run([sys.executable, str(script), *args.arguments]).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
