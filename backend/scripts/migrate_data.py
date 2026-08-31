"""Copy and verify runtime data while moving the service under ``backend/``."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import sqlite3


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPO_ROOT / "backend" / "data_legacy"
DEFAULT_TARGET = REPO_ROOT / "backend" / "data"


def _files(root: Path):
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path.relative_to(root), path


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _summary(root: Path) -> tuple[int, int]:
    items = list(_files(root))
    return len(items), sum(path.stat().st_size for _, path in items)


def _verify_files(source: Path, target: Path) -> None:
    missing: list[str] = []
    mismatched: list[str] = []
    for relative, source_path in _files(source):
        target_path = target / relative
        if not target_path.exists():
            missing.append(str(relative))
            continue
        if source_path.stat().st_size != target_path.stat().st_size:
            mismatched.append(str(relative))
            continue
        if _digest(source_path) != _digest(target_path):
            mismatched.append(str(relative))
    if missing or mismatched:
        details = []
        if missing:
            details.append(f"缺失 {len(missing)} 个文件")
        if mismatched:
            details.append(f"内容不一致 {len(mismatched)} 个文件")
        raise RuntimeError("数据校验失败：" + "，".join(details))


def _verify_sqlite(target: Path) -> None:
    database = target / "marker_web.sqlite3"
    if not database.exists():
        return
    connection = sqlite3.connect(database)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite 完整性校验失败：{database} -> {result!r}")


def migrate(source: Path, target: Path, *, verify_only: bool = False) -> None:
    source = source.resolve()
    target = target.resolve()
    if not source.exists():
        raise FileNotFoundError(f"源数据目录不存在：{source}")
    if source == target:
        raise ValueError("源数据目录和目标数据目录不能相同")
    if not verify_only:
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True)
    _verify_files(source, target)
    _verify_sqlite(target)
    count, size = _summary(source)
    print(f"数据迁移校验通过：{count} 个文件，{size:,} bytes")
    print(f"源目录：{source}")
    print(f"目标目录：{target}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--verify-only", action="store_true", help="只校验，不复制")
    args = parser.parse_args()
    migrate(args.source, args.target, verify_only=args.verify_only)


if __name__ == "__main__":
    main()
