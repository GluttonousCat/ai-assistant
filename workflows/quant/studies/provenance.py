"""Freeze study declarations and verify source snapshots across the jobs migration."""

from functools import lru_cache
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile

from core.local_state import file_lock


_STUDIES = (
    "dual_forecast_60f", "intraday_protection_60f", "label_alignment_60f",
    "mainboard_60f", "mainboard_regime", "market_context", "online_60f",
    "online_market_gate", "ordered_60f", "quality_60f", "stop_orders_60f",
    "support_tranches_60f", "tranches",
)
_WYCKOFF = {
    "wyckoff_60f": "price_volume",
    "wyckoff_family_60f": "family",
    "wyckoff_family_review": "family_review",
    "wyckoff_forward_60f": "forward",
    "wyckoff_hybrid_60f": "hybrid",
}
STUDY_MODULES = {
    **{f"research_{name}": f"{__package__}.{name}" for name in _STUDIES},
    **{f"research_{name}": f"{__package__}.wyckoff.{new}" for name, new in _WYCKOFF.items()},
}


@lru_cache(maxsize=1)
def _relocations():
    # Only these reviewed before/after pairs may bridge a historical declaration.
    # Never infer either digest from the live checkout or the experiment archive.
    ledger = Path(__file__).with_name("source_migration.json")
    pairs = json.loads(ledger.read_text(encoding="utf-8"))
    modules = {**STUDY_MODULES, "backtest_mtf": "workflows.quant.backtests.mtf"}
    return {
        f"jobs/{old}.py": (modules[old].replace(".", "/") + ".py", *hashes)
        for old, hashes in pairs.items()
    }


def declaration_matches(recorded, current):
    if recorded == current:
        return True
    if not isinstance(recorded.get("code_sha256"), dict):
        return False
    hashes = dict(recorded["code_sha256"])
    for old, (new, before, after) in _relocations().items():
        if old in hashes:
            if hashes.pop(old) != before or new in hashes:
                return False
            hashes[new] = after
    return {**recorded, "code_sha256": hashes} == current


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def source_hashes(root, names):
    return {name: digest(root / name) for name in names}


def _publish_once(path, content):
    """Publish complete bytes without replacing any file another run already wrote."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
    temporary = Path(stream.name)
    try:
        with stream:
            stream.write(content)
        try:
            if os.name == "nt":
                temporary.rename(path)  # Windows rename fails if the target exists.
            else:
                os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError as error:
            if error.errno not in (errno.ENOTSUP, errno.EPERM, errno.ENOSYS):
                raise
            # POSIX volumes without hard links still support atomic replacement.
            with file_lock(path.parent / ".freeze.lock") as acquired:
                if not acquired:
                    raise BlockingIOError(f"Concurrent study archive write: {path}") from error
                if not path.exists():
                    temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _archive_sources(root, folder, recorded, current):
    previous = {
        new: old for old, (new, _, _) in _relocations().items() if old in recorded
    }
    for name, expected in current.items():
        old = previous.get(name)
        if old and (folder / old).exists():
            if digest(folder / old) != recorded[old]:
                raise ValueError(f"Frozen source archive changed: {old}")
            continue
        target = folder / name
        if not target.exists():
            data = (root / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError(f"Study source changed while archiving: {name}")
            _publish_once(target, data)
        if digest(target) != expected:
            raise ValueError(f"Frozen source archive changed: {name}")


def freeze(
    path, value, *, source_root=None, mismatch="Frozen experiment changed; use a new version"
):
    """Compare the whole declaration and preserve the original manifest on resume."""
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    current = json.loads(encoded)
    if not path.exists():
        # Match the existing write_json format, including platform newline handling.
        _publish_once(path, encoded.replace("\n", os.linesep).encode("utf-8"))
    recorded = json.loads(path.read_text(encoding="utf-8"))
    if not declaration_matches(recorded, current):
        raise ValueError(mismatch)
    if source_root is not None and current.get("code_sha256"):
        _archive_sources(
            source_root, path.parent / "source", recorded["code_sha256"], current["code_sha256"]
        )
    return current
