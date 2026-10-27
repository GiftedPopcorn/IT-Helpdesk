"""Verify the submitted runtime snapshot and evidence without model calls."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    # Normalize text line endings so a Windows Git checkout also verifies.
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def main():
    manifest = json.loads((ROOT / 'evals/submission/manifest.json').read_text(encoding='utf-8'))
    failures = []
    for group in ('runtime_files', 'evidence_files'):
        for name, expected in manifest[group].items():
            path = ROOT / name
            if not path.is_file() or digest(path) != expected:
                failures.append(name)
    if failures:
        print('Snapshot mismatch (do not cite the old results for changed files):')
        print('\n'.join(failures))
        return 1
    print('PASS: runtime/data/cases and submitted evidence match the recorded snapshot.')
    print('This verifies provenance, not that all functional tests passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
