"""Run a deterministic portion of the suite in an isolated CI checkout.

Keep the whole checkout available: test modules share fixture helpers. Distribute
individual cases, so one slow module cannot monopolize a worker. Use --list to
inspect compilation assignments without importing tests or production code.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


def test_cases(suite):
    import unittest

    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_cases(item)
        else:
            yield item


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=int, required=True)
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()
    if args.count < 1 or not 0 <= args.index < args.count:
        parser.error('require count > 0 and 0 <= index < count')
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / 'tests').rglob('test_*.py'))
    if list(root.glob('test_*.py')):
        parser.error('server tests must live under tests/')
    names = {path: '.'.join(path.relative_to(root).with_suffix('').parts) for path in paths}
    selected = paths[args.index::args.count]
    if not selected:
        parser.error('empty test shard')
    if args.list:
        print('\n'.join(names[path] for path in selected))
        return 0
    for path in selected:
        compile(path.read_bytes(), str(path), 'exec')
    # Import only after assignment; --list stays safe in a live development tree.
    import unittest

    sys.path.insert(0, str(root))
    discovered = unittest.defaultTestLoader.loadTestsFromNames([names[path] for path in paths])
    cases = list(test_cases(discovered))
    selected_cases = cases[args.index::args.count]
    print(f'Shard {args.index + 1}/{args.count}: {len(selected_cases)}/{len(cases)} test cases', flush=True)
    suite = unittest.TestSuite(selected_cases)
    # Let unittest release each completed case and its fixture graph. Keeping
    # the discovery copies alive defeats TestSuite's normal cleanup.
    del discovered, cases, selected_cases
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
