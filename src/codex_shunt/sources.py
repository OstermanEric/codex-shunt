"""Safe source selection and isolated workspace preparation."""

from __future__ import annotations

import fnmatch
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".next",
    ".turbo",
    ".venv",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}

SENSITIVE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "credentials.json",
    "service-account*.json",
    "*.keystore",
    "*.mobileprovision",
)


@dataclass(frozen=True)
class SourceFile:
    source: Path
    relative: Path
    size: int
    lines: int


@dataclass(frozen=True)
class SourceSelection:
    root: Path
    files: tuple[SourceFile, ...]
    excluded_files: int
    source_bytes: int
    source_lines: int

    @property
    def estimated_tokens(self) -> int:
        return math.ceil(self.source_bytes / 4)


def is_sensitive(path: Path) -> bool:
    lowered = path.name.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in SENSITIVE_PATTERNS)


def is_probably_text(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            sample = handle.read(8192)
    except OSError:
        return False
    if b"\x00" in sample:
        return False
    if not sample:
        return True
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        printable = sum(byte in b"\t\n\r" or 32 <= byte <= 126 for byte in sample)
        return printable / len(sample) >= 0.9


def count_lines(path: Path) -> int:
    count = 0
    last = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            count += chunk.count(b"\n")
            last = chunk[-1:] if chunk else last
    if path.stat().st_size and last != b"\n":
        count += 1
    return count


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _iter_directory(path: Path) -> Iterable[Path]:
    for current, directories, filenames in os.walk(path):
        directories[:] = sorted(
            name
            for name in directories
            if name not in SKIP_DIRECTORIES and not name.startswith(".git")
        )
        for filename in sorted(filenames):
            yield Path(current) / filename


def collect_sources(
    root: Path,
    requested: Iterable[str],
    *,
    max_files: int,
    max_bytes: int,
) -> SourceSelection:
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise ValueError(f"Repository root is not a directory: {resolved_root}")

    candidates: list[Path] = []
    explicitly_requested: set[Path] = set()
    for raw in requested:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = resolved_root / candidate
        candidate = candidate.resolve()
        if not _inside_root(candidate, resolved_root):
            raise ValueError(f"Path escapes repository root: {raw}")
        if not candidate.exists():
            raise ValueError(f"Path does not exist: {raw}")
        if candidate.is_file():
            explicitly_requested.add(candidate)
            candidates.append(candidate)
        elif candidate.is_dir():
            candidates.extend(_iter_directory(candidate))

    seen: set[Path] = set()
    selected: list[SourceFile] = []
    excluded = 0
    total_bytes = 0
    total_lines = 0

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            excluded += 1
            continue
        if resolved in seen or not resolved.is_file() or not _inside_root(resolved, resolved_root):
            continue
        seen.add(resolved)
        relative = resolved.relative_to(resolved_root)
        if is_sensitive(resolved):
            if resolved in explicitly_requested:
                raise ValueError(f"Refusing explicitly sensitive path: {relative}")
            excluded += 1
            continue
        if any(part in SKIP_DIRECTORIES for part in relative.parts) or not is_probably_text(resolved):
            excluded += 1
            continue
        try:
            size = resolved.stat().st_size
        except OSError:
            excluded += 1
            continue
        if len(selected) >= max_files or total_bytes + size > max_bytes:
            raise ValueError(
                "Source selection exceeds configured limits "
                f"({max_files} files or {max_bytes} bytes); narrow the requested paths"
            )
        lines = count_lines(resolved)
        selected.append(SourceFile(resolved, relative, size, lines))
        total_bytes += size
        total_lines += lines

    if not selected:
        raise ValueError("No eligible text files were selected")

    return SourceSelection(
        root=resolved_root,
        files=tuple(selected),
        excluded_files=excluded,
        source_bytes=total_bytes,
        source_lines=total_lines,
    )


def copy_to_workspace(selection: SourceSelection, workspace: Path) -> None:
    for item in selection.files:
        destination = workspace / item.relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.source, destination)
