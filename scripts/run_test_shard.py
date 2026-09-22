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
    # Keep legacy root modules and the tests/ package in the same case pool
    # while the suite is being reorganized. Fail visibly for other packages so
    # new test locations cannot silently fall out of all eight workers.
    for package in root.iterdir():
        if package.name != 'tests' and package.is_dir() and (package / '__init__.py').is_file():
            if any(package.rglob('test_*.py')):
                parser.error('nested test modules require updating shard discovery')
    paths = sorted([*root.glob('test_*.py'), *(root / 'tests').rglob('test_*.py')])
    for path in paths:
        for parent in path.relative_to(root).parents:
            if parent != Path('.') and not (root / parent / '__init__.py').is_file():
                parser.error(f'test package requires __init__.py: {parent}')
    module_names = {
        path: '.'.join(path.relative_to(root).with_suffix('').parts)
        for path in paths
    }
    selected = paths[args.index::args.count]
    if not selected:
        parser.error('empty test shard')
    if args.list:
        print('\n'.join(module_names[path] for path in selected))
        return 0
    for path in selected:
        compile(path.read_bytes(), str(path), 'exec')
    # Import only after assignment; --list stays safe in a live development tree.
    import unittest

    sys.path.insert(0, str(root))
    discovered = unittest.defaultTestLoader.loadTestsFromNames([module_names[path] for path in paths])
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
