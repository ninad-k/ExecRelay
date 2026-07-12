"""Guards model/SHA256SUMS against a silent model-artifact swap.

The Dockerfile runs `sha256sum -c SHA256SUMS` after COPYing model/ so a bad
build fails fast, but that only catches drift at image-build time. This test
recomputes the same checksums locally so `pytest` fails too if someone
replaces xgb_production.json or feature_order.txt without regenerating
SHA256SUMS.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parent.parent / "model"
SUMS_PATH = MODEL_DIR / "SHA256SUMS"


def _parse_sums(path: Path) -> dict[str, str]:
    """Parse standard `sha256sum` output: '<hex digest>  <filename>' per line."""
    entries = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        digest, filename = line.split(None, 1)
        entries[filename.strip()] = digest.strip()
    return entries


def test_sha256sums_file_ends_with_newline():
    raw = SUMS_PATH.read_bytes()
    assert raw.endswith(b"\n")


def test_sha256sums_lists_expected_artifacts():
    entries = _parse_sums(SUMS_PATH)
    assert set(entries) == {"xgb_production.json", "feature_order.txt"}


def test_sha256sums_matches_shipped_artifacts():
    entries = _parse_sums(SUMS_PATH)
    for filename, expected_digest in entries.items():
        actual_digest = hashlib.sha256((MODEL_DIR / filename).read_bytes()).hexdigest()
        assert actual_digest == expected_digest, (
            f"{filename} does not match model/SHA256SUMS -- "
            "regenerate it after any model artifact swap"
        )
