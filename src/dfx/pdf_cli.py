"""Short commands for the existing conversion, review, staging, and website scripts."""
import argparse
import subprocess
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='dfx', description='Convert or rebuild PDFs and LaTeX, diagnose PDFs, review previews, stage reviewed documents, and scan websites for PDFs. Run from the project folder.',
        epilog='Use dfx diagnose --help, dfx convert --help, dfx rebuild --help, dfx review --help, dfx stage --help, or dfx site --help for script options.')
    parser.add_argument('command', choices=('convert', 'rebuild', 'review', 'stage', 'diagnose', 'site'))
    parser.add_argument('arguments', nargs=argparse.REMAINDER, help='Arguments passed to the selected script')
    args = parser.parse_args(argv)
    try:
        return subprocess.run([sys.executable, '-m', f'dfx.{args.command}', *args.arguments]).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
