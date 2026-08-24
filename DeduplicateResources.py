#!/usr/bin/env python3
"""Folder-only duplicate-file quarantine tool with a redirect journal.

Double-click on Windows, select one folder, and the tool will find exact
duplicates, optionally accept manually grouped similar files, quarantine the
redundant copies, and record old filename -> kept filename mappings. No INI or
other reference file is read or modified.
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import os
import shutil
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


CHUNK_SIZE = 4 * 1024 * 1024
QUARANTINE_PREFIX = "_Deduplicate_Quarantine_"
JOURNAL_NAME = "DeduplicateResources-redirects.tsv"
JOURNAL_FIELDS = (
    "status",
    "method",
    "old_filename",
    "kept_filename",
    "old_size",
    "kept_size",
    "old_sha256",
    "kept_sha256",
    "updated_at",
)


@dataclass
class FileItem:
    path: Path
    size: int
    mtime_ns: int
    digest: str = ""


@dataclass(frozen=True)
class DuplicateGroup:
    survivor: FileItem
    redundant: tuple[FileItem, ...]
    method: str


@dataclass
class RedirectRow:
    status: str
    method: str
    old_filename: str
    kept_filename: str
    old_size: int
    kept_size: int
    old_sha256: str
    kept_sha256: str
    updated_at: str

    def as_dict(self) -> dict[str, str | int]:
        return {field: getattr(self, field) for field in JOURNAL_FIELDS}


def configure_console() -> None:
    if os.name == "nt" and (sys.stdin is None or sys.stdout is None):
        try:
            ctypes.windll.kernel32.AllocConsole()
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW("Folder Duplicate Quarantine")
        except AttributeError:
            pass


def clean_pasted_path(raw: str) -> Path:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return Path(os.path.expandvars(value)).expanduser()


def prompt_folder() -> Path:
    while True:
        candidate = clean_pasted_path(
            input("Paste or drag the FOLDER TO DEDUPLICATE here, then press Enter:\n> ")
        )
        try:
            candidate = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            print("[ERROR] Folder not found. Try again.\n")
            continue
        if not candidate.is_dir():
            print("[ERROR] The path is not a folder. Try again.\n")
            continue
        return candidate


def scan_folder(folder: Path) -> list[FileItem]:
    script_path = Path(__file__).resolve()
    items: list[FileItem] = []
    for path in sorted(folder.iterdir(), key=lambda value: (value.name.casefold(), value.name)):
        if not path.is_file() or path.is_symlink() or path.resolve() == script_path:
            continue
        stat = path.stat()
        items.append(FileItem(path, stat.st_size, stat.st_mtime_ns))
    return items


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def hash_size_collisions(items: list[FileItem]) -> None:
    by_size: dict[int, list[FileItem]] = defaultdict(list)
    for item in items:
        by_size[item.size].append(item)
    candidates = [item for group in by_size.values() if len(group) > 1 for item in group]
    if not candidates:
        return
    print(f"\nHashing {len(candidates)} files that share a size...")
    for index, item in enumerate(candidates, start=1):
        print(f"  [{index}/{len(candidates)}] {item.path.name}")
        item.digest = sha256_file(item.path)


def find_exact_groups(items: list[FileItem]) -> list[DuplicateGroup]:
    by_identity: dict[tuple[int, str], list[FileItem]] = defaultdict(list)
    for item in items:
        if item.digest:
            by_identity[(item.size, item.digest)].append(item)
    groups: list[DuplicateGroup] = []
    for identical in by_identity.values():
        if len(identical) < 2:
            continue
        ordered = sorted(identical, key=lambda item: (item.path.name.casefold(), item.path.name))
        groups.append(DuplicateGroup(ordered[0], tuple(ordered[1:]), "sha256"))
    groups.sort(key=lambda group: (group.survivor.path.name.casefold(), group.survivor.path.name))
    return groups


def print_exact_plan(groups: list[DuplicateGroup]) -> None:
    count = sum(len(group.redundant) for group in groups)
    saving = sum(item.size for group in groups for item in group.redundant)
    print("\n" + "=" * 72)
    print(f"EXACT DUPLICATES: {len(groups)} group(s), {count} file(s) to quarantine")
    print(f"Potential saving: {saving:,} bytes ({saving / (1024 * 1024):.2f} MiB)")
    for index, group in enumerate(groups, start=1):
        print(f"\n[{index}] KEEP: {group.survivor.path.name}")
        for item in group.redundant:
            print(f"    QUARANTINE: {item.path.name}")


def confirm_exact_plan() -> bool:
    while True:
        answer = input("\nEnter 1 to quarantine these exact duplicates, or 0 to skip: ").strip()
        if answer == "1":
            return True
        if answer == "0":
            return False
        print("Please enter 1 or 0.")


def unique_quarantine_folder(folder: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = folder / f"{QUARANTINE_PREFIX}{timestamp}"
    suffix = 2
    while candidate.exists():
        candidate = folder / f"{QUARANTINE_PREFIX}{timestamp}_{suffix}"
        suffix += 1
    return candidate


def write_journal_atomic(path: Path, rows: list[RedirectRow]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(
                stream,
                fieldnames=JOURNAL_FIELDS,
                dialect="excel-tab",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(row.as_dict() for row in rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def verify_unchanged(item: FileItem, verify_hash: bool) -> None:
    stat = item.path.stat()
    if stat.st_size != item.size or stat.st_mtime_ns != item.mtime_ns:
        raise RuntimeError(f"File changed while the plan was open: {item.path}")
    if verify_hash and sha256_file(item.path) != item.digest:
        raise RuntimeError(f"File contents changed while the plan was open: {item.path}")


def ensure_quarantine(folder: Path, quarantine: Path | None) -> tuple[Path, Path]:
    if quarantine is None:
        quarantine = unique_quarantine_folder(folder)
        quarantine.mkdir()
    return quarantine, quarantine / JOURNAL_NAME


def process_group(
    group: DuplicateGroup,
    folder: Path,
    quarantine: Path | None,
    rows: list[RedirectRow],
) -> tuple[Path | None, int, int]:
    verify_hash = group.method == "sha256"
    try:
        verify_unchanged(group.survivor, verify_hash)
        for item in group.redundant:
            verify_unchanged(item, verify_hash)
    except (OSError, RuntimeError) as exc:
        print(f"[GROUP FAILED] {exc}")
        return quarantine, 0, 1

    try:
        quarantine, journal = ensure_quarantine(folder, quarantine)
    except OSError as exc:
        print(f"[GROUP FAILED] Could not create quarantine: {exc}")
        return quarantine, 0, 1

    group_rows: list[tuple[FileItem, RedirectRow]] = []
    now = datetime.now().isoformat(timespec="seconds")
    for item in group.redundant:
        row = RedirectRow(
            status="PLANNED",
            method=group.method,
            old_filename=item.path.name,
            kept_filename=group.survivor.path.name,
            old_size=item.size,
            kept_size=group.survivor.size,
            old_sha256=item.digest,
            kept_sha256=group.survivor.digest,
            updated_at=now,
        )
        rows.append(row)
        group_rows.append((item, row))
    try:
        write_journal_atomic(journal, rows)
    except OSError as exc:
        del rows[-len(group_rows) :]
        print(f"[GROUP FAILED] Could not write redirect journal: {exc}")
        return quarantine, 0, 1

    moved = 0
    failures = 0
    for item, row in group_rows:
        destination = quarantine / item.path.name
        try:
            if destination.exists():
                raise FileExistsError(f"Quarantine already contains {destination.name}")
            shutil.move(str(item.path), str(destination))
            row.status = "QUARANTINED"
            row.updated_at = datetime.now().isoformat(timespec="seconds")
            moved += 1
            print(f"[QUARANTINED] {item.path.name} -> keep {group.survivor.path.name}")
        except OSError as exc:
            row.status = "MOVE_FAILED"
            row.updated_at = datetime.now().isoformat(timespec="seconds")
            failures += 1
            print(f"[MOVE FAILED] {item.path.name}: {exc}")
        try:
            write_journal_atomic(journal, rows)
        except OSError as exc:
            failures += 1
            print(f"[JOURNAL WARNING] Could not update status for {item.path.name}: {exc}")
    return quarantine, moved, failures


def read_line_or_escape(prompt: str) -> str | None:
    if os.name != "nt" or sys.stdin is None or not sys.stdin.isatty():
        value = input(prompt)
        return None if value == "\x1b" else value
    import msvcrt

    print(prompt, end="", flush=True)
    characters: list[str] = []
    while True:
        character = msvcrt.getwch()
        if character == "\x1b":
            print("[ESC]")
            return None
        if character == "\x03":
            raise KeyboardInterrupt
        if character in {"\r", "\n"}:
            print()
            return "".join(characters)
        if character == "\b":
            if characters:
                characters.pop()
                print("\b \b", end="", flush=True)
            continue
        if character in {"\x00", "\xe0"}:
            msvcrt.getwch()
            continue
        if character.isprintable() or character == "\t":
            characters.append(character)
            print(character, end="", flush=True)


def prompt_manual_mode() -> bool:
    print("\n" + "=" * 72)
    print("OPTIONAL MANUAL SIMILAR-FILE GROUPING")
    print("Only group files you have confirmed are interchangeable.")
    while True:
        answer = read_line_or_escape("Press ENTER to create a manual group, or ESC to finish: ")
        if answer is None or answer.strip() == "0":
            return False
        if not answer.strip() or answer.strip() == "1":
            return True
        print("Press Enter without typing anything, or press Escape.")


def collect_manual_paths(folder: Path) -> list[Path] | None:
    print("\nDrag or paste files one at a time.")
    print("Empty Enter finishes the group; ESC exits manual mode and discards the incomplete group.")
    selected: list[Path] = []
    selected_keys: set[str] = set()
    extension: str | None = None
    while True:
        raw = read_line_or_escape(f"File {len(selected) + 1}> ")
        if raw is None:
            return None
        if not raw.strip():
            if len(selected) >= 2:
                return selected
            print("A group needs at least two files; the incomplete group was discarded.")
            return []
        candidate = clean_pasted_path(raw)
        try:
            candidate = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            print("  [ERROR] File not found.")
            continue
        if not candidate.is_file() or candidate.is_symlink() or candidate.parent != folder:
            print("  [ERROR] Select a regular file directly inside the chosen folder.")
            continue
        key = str(candidate).casefold()
        if key in selected_keys:
            print("  [ERROR] That file is already in this group.")
            continue
        candidate_extension = candidate.suffix.casefold()
        if extension is None:
            extension = candidate_extension
        elif candidate_extension != extension:
            print(f"  [ERROR] All files in the group must use the same extension ({extension}).")
            continue
        selected.append(candidate)
        selected_keys.add(key)
        print(f"  [ADDED] {candidate.name} ({candidate.stat().st_size:,} bytes)")


def build_manual_group(paths: list[Path]) -> DuplicateGroup:
    items: list[FileItem] = []
    for path in paths:
        stat = path.stat()
        items.append(FileItem(path, stat.st_size, stat.st_mtime_ns))
    ordered = sorted(items, key=lambda item: (-item.size, item.path.name.casefold(), item.path.name))
    return DuplicateGroup(ordered[0], tuple(ordered[1:]), "manual")


def confirm_manual_group(group: DuplicateGroup) -> bool | None:
    print("\nManual group plan:")
    print(f"  KEEP LARGEST: {group.survivor.path.name} ({group.survivor.size:,} bytes)")
    for item in group.redundant:
        print(f"  QUARANTINE:   {item.path.name} ({item.size:,} bytes)")
    print("WARNING: Hash equality is intentionally not required for a manual group.")
    while True:
        answer = read_line_or_escape("Enter 1 to process, 0 to discard, or ESC to finish: ")
        if answer is None:
            return None
        if answer.strip() == "1":
            return True
        if answer.strip() == "0":
            return False
        print("Please enter 1, 0, or press Escape.")


def run() -> int:
    print("=" * 72)
    print("FOLDER DUPLICATE QUARANTINE")
    print("=" * 72)
    print("No INI or reference file will be read or modified.\n")
    folder = prompt_folder()
    try:
        items = scan_folder(folder)
    except OSError as exc:
        print(f"[ERROR] Could not scan the folder: {exc}")
        return 1
    if not items:
        print("No regular files were found directly inside that folder.")
        return 0
    print(f"\nFound {len(items)} top-level file(s). Subfolders are ignored.")

    try:
        hash_size_collisions(items)
        exact_groups = find_exact_groups(items)
    except OSError as exc:
        print(f"[ERROR] Hashing failed: {exc}")
        return 1

    quarantine: Path | None = None
    rows: list[RedirectRow] = []
    moved_total = 0
    failure_total = 0
    if exact_groups:
        print_exact_plan(exact_groups)
        if confirm_exact_plan():
            for group in exact_groups:
                quarantine, moved, failures = process_group(group, folder, quarantine, rows)
                moved_total += moved
                failure_total += failures
        else:
            print("Automatic exact-duplicate phase skipped.")
    else:
        print("\nNo exact byte-identical duplicates were found.")

    while prompt_manual_mode():
        paths = collect_manual_paths(folder)
        if paths is None:
            break
        if not paths:
            continue
        try:
            group = build_manual_group(paths)
        except OSError as exc:
            print(f"[ERROR] Could not prepare manual group: {exc}")
            failure_total += 1
            break
        confirmation = confirm_manual_group(group)
        if confirmation is None:
            break
        if not confirmation:
            print("Manual group discarded. Nothing was changed.")
            continue
        quarantine, moved, failures = process_group(group, folder, quarantine, rows)
        moved_total += moved
        failure_total += failures
        print(f"Manual group complete: {moved} file(s) quarantined.")

    print("\n" + "=" * 72)
    print("FINAL SUMMARY")
    print(f"Files quarantined: {moved_total}")
    print(f"Failures:          {failure_total}")
    if quarantine is not None:
        print(f"Quarantine:        {quarantine}")
        print(f"Redirect journal:  {quarantine / JOURNAL_NAME}")
        print("Use only journal rows whose status is QUARANTINED.")
    else:
        print("No quarantine or redirect journal was created because no files were moved.")
    print("Nothing was permanently deleted.")
    return 1 if failure_total else 0


def main() -> int:
    configure_console()
    try:
        return run()
    except KeyboardInterrupt:
        print("\n\nCancelled by user.")
        return 130
    except Exception as exc:
        print(f"\n[UNEXPECTED ERROR] {exc}")
        return 1
    finally:
        if sys.stdin is not None and getattr(sys.stdin, "isatty", lambda: False)():
            try:
                input("\nPress Enter to close...")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
