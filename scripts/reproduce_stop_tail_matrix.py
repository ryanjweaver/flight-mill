"""SOFTWARE ONLY stop-tail reproduction; --output must be a NEW directory.

Can target the unmodified reviewed source with --source. Imports that source's
app/src, repository and Endpoint fixture explicitly, while retaining this runner's
maintained STOP-boundary helper. Exit 1 exposes the defect; exit 0 requires 8/8.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    helper = Path(__file__).resolve().parents[1] / 'app/tests'
    sys.path[:0] = [str(source / 'app/src'), str(source), str(source / 'app/tests'), str(helper)]
    from flightmill.acquisition import service
    from stop_tail_support import assert_clean, run_case
    assert Path(service.__file__).resolve() == source / 'app/src/flightmill/acquisition/service.py'
    args.output.mkdir(parents=True, exist_ok=False)
    cases = []
    for mode in ['default_150ms', 'legacy_50ms']:
        for path in ['ui', 'planned', 'close', 'control']:
            folder = args.output / (mode + '_' + path)
            result = run_case(folder, mode, path)
            (folder / 'transcript.jsonl').write_bytes(b''.join(result['frames']))
            error = None
            try:
                assert_clean(result, 2)
                assert result['stop_commands'] == 1
                assert not {'event_outside_recording', 'summary_count'} & set(result['warnings'])
            except AssertionError as exc:
                error = str(exc) or 'Expected complete two-row bundle'
            row = {'mode': mode, 'path': path, 'passed': error is None, 'error': error,
                   'raw_rows': len(result['rows']), 'accepted_count': result['summaries'][-1].accepted_event_count,
                   'incomplete': result['metadata']['incomplete'], 'sha256': result['sha256'],
                   'warnings': result['warnings'], 'conformance_passed': result['conformance_passed'],
                   'conformance_issues': result['conformance_issues']}
            cases.append(row)
            print(json.dumps(row), flush=True)
    report = {'scope': 'SOFTWARE ONLY; no physical port or native desktop validation',
              'source': str(source), 'resolved_service': service.__file__, 'cases': cases}
    (args.output / 'matrix.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return int(not all(case['passed'] for case in cases))


if __name__ == '__main__':
    raise SystemExit(main())
