"""Run the whole refresh: download, build data, check, build page.

  python pipeline/run.py            # download fresh data, then build
  python pipeline/run.py --no-fetch # rebuild from what is already in data/raw
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def step(name, *args):
    print(f'\n===== {name}', flush=True)
    r = subprocess.run([sys.executable, '-W', 'ignore', str(HERE / name), *args], cwd=HERE)
    if r.returncode:
        print(f'===== {name} failed', flush=True)
        sys.exit(r.returncode)


if __name__ == '__main__':
    if '--no-fetch' not in sys.argv:
        step('fetch.py')
    step('build_data.py')
    step('checks.py')
    step('build_page.py')
