#!/usr/bin/env python3
"""Safely deduplicate a flat resource folder and redirect one text file.

Designed to be launched by double-clicking on Windows.  The selected text file
is inspected before any resource is hashed.  Exact, case-sensitive relative
path references are redirected to one surviving copy before redundant files
are moved into a quarantine subfolder.
"""

from __future__ import annotations

import codecs
import ctypes
import hashlib
import locale
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


CHUNK_SIZE = 4 * 1024 * 1024
QUARANTINE_PREFIX = "_Deduplicate_Quarantine_"
FILENAME_ASSIGNMENT = re.compile(
    r"^filename[ \t]*=[ \t]*(?P<value>[^\r\n]*)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class TextDocument:
    path: Path
    text: str
    encoding: str
    bom: bytes
    size: int
    mtime_ns: int

    def encode(self, text: str) -> bytes:
        return self.bom + text.encode(self.encoding)


@dataclass
class ResourceFile:
    path: Path
    size: int
    mtime_ns: int
    aliases: dict[str, str]
    references: Counter[str] = field(default_factory=Counter)
    digest: str | None = None

    @property
    def reference_count(self) -> int:
        return sum(self.references.values())


@dataclass(frozen=True)
class DuplicateGroup:
    survivor: ResourceFile
    redundant: tuple[ResourceFile, ...]


@dataclass(frozen=True)
class ReferenceOccurrence:
    path: str
    start: int
    end: int


def attach_console_if_needed() -> None:
    """Give pythonw.exe launches a usable console while changing nothing for python.exe."""
    if os.name != "nt" or (sys.stdin is not None and sys.stdout is not None):
        return
    try:
        ctypes.windll.kernel32.AllocConsole()
        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def configure_console() -> None:
    attach_console_if_needed()
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW("Resource File Deduplicator")
        except AttributeError:
            pass


def clean_pasted_path(raw: str) -> Path:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return Path(os.path.expandvars(value)).expanduser()


def prompt_existing_path(prompt: str, want_directory: bool) -> Path:
    kind = "folder" if want_directory else "file"
    while True:
        candidate = clean_pasted_path(input(prompt))
        try:
            candidate = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            print(f"[ERROR] That {kind} does not exist. Try again.\n")
            continue
        if want_directory and not candidate.is_dir():
            print("[ERROR] The path is not a folder. Try again.\n")
            continue
        if not want_directory and not candidate.is_file():
            print("[ERROR] The path is not a file. Try again.\n")
            continue
        return candidate


def decode_text_file(path: Path) -> TextDocument:
    raw = path.read_bytes()
    bom_encodings = (
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    )
    for bom, encoding in bom_encodings:
        if raw.startswith(bom):
            text = raw[len(bom) :].decode(encoding)
            stat = path.stat()
            return TextDocument(path, text, encoding, bom, stat.st_size, stat.st_mtime_ns)

    encodings = ["utf-8"]
    preferred = locale.getpreferredencoding(False) or "cp1252"
    if preferred.casefold().replace("-", "") != "utf8":
        encodings.append(preferred)
    errors: list[str] = []
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            stat = path.stat()
            return TextDocument(path, text, encoding, b"", stat.st_size, stat.st_mtime_ns)
        except (UnicodeDecodeError, LookupError) as exc:
            errors.append(f"{encoding}: {exc}")
    raise UnicodeError("Could not decode the text file (" + "; ".join(errors) + ")")


def relative_aliases(resource: Path, text_parent: Path) -> dict[str, str]:
    """Return reference spellings and their style identifiers."""
    relative = os.path.relpath(resource, text_parent)
    back = relative.replace("/", "\\")
    forward = relative.replace("\\", "/")
    aliases: dict[str, str] = {back: "back", forward: "forward"}
    if not back.startswith((".\\", "..\\")):
        aliases[".\\" + back] = "dot_back"
    if not forward.startswith(("./", "../")):
        aliases["./" + forward] = "dot_forward"
    # When both files share a directory, the relative alias already is the bare
    # filename.  Do not add a basename fallback for child resource folders: it
    # could mistake OtherTextures/foo.dds for Textures/foo.dds.
    return aliases


def scan_resources(folder: Path, document: TextDocument) -> list[ResourceFile]:
    resources: list[ResourceFile] = []
    for path in sorted(folder.iterdir(), key=lambda item: (item.name.casefold(), item.name)):
        if not path.is_file() or path.is_symlink():
            continue
        stat = path.stat()
        aliases = relative_aliases(path, document.path.parent)
        resources.append(
            ResourceFile(
                path=path,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                aliases=aliases,
            )
        )
    return resources


def parse_filename_assignments(text: str) -> list[ReferenceOccurrence]:
    """Parse values only on lines that begin exactly with ``filename``."""
    occurrences: list[ReferenceOccurrence] = []
    for match in FILENAME_ASSIGNMENT.finditer(text):
        raw = match.group("value")
        leading = len(raw) - len(raw.lstrip(" \t"))
        content = raw[leading:]
        if not content or content.startswith(";"):
            continue

        offset = leading
        if content[0] in {'"', "'"}:
            quote = content[0]
            closing = content.find(quote, 1)
            if closing < 0:
                line = text.count("\n", 0, match.start()) + 1
                raise ValueError(f"Unclosed quoted filename value on line {line}.")
            path_value = content[1:closing]
            offset += 1
        else:
            comment = re.search(r"[ \t]+;", content)
            value_part = content[: comment.start()] if comment else content
            path_value = value_part.rstrip(" \t")

        if not path_value:
            continue
        start = match.start("value") + offset
        occurrences.append(ReferenceOccurrence(path_value, start, start + len(path_value)))
    return occurrences


def folder_reference_prefixes(folder: Path, text_parent: Path) -> set[str]:
    relative = os.path.relpath(folder, text_parent)
    back = relative.replace("/", "\\").rstrip("\\")
    forward = relative.replace("\\", "/").rstrip("/")
    prefixes = {back, forward}
    if not back.startswith((".\\", "..\\")):
        prefixes.add(".\\" + back)
    if not forward.startswith(("./", "../")):
        prefixes.add("./" + forward)
    return prefixes


def belongs_to_selected_folder(path_value: str, prefixes: set[str]) -> bool:
    folded = path_value.casefold()
    for prefix in prefixes:
        folded_prefix = prefix.casefold()
        if folded.startswith(folded_prefix + "/") or folded.startswith(folded_prefix + "\\"):
            return True
    return False


def collect_reference_inventory(
    document: TextDocument,
    resources: list[ResourceFile],
    resource_folder: Path,
    *,
    record: bool,
) -> tuple[list[ReferenceOccurrence], list[str]]:
    occurrences = parse_filename_assignments(document.text)
    lookup: dict[str, ResourceFile] = {}
    for resource in resources:
        if record:
            resource.references.clear()
        for alias in resource.aliases:
            existing = lookup.get(alias)
            if existing is not None and existing is not resource:
                raise RuntimeError(f"Ambiguous relative resource path: {alias}")
            lookup[alias] = resource

    prefixes = folder_reference_prefixes(resource_folder, document.path.parent)
    missing: list[str] = []
    for occurrence in occurrences:
        resource = lookup.get(occurrence.path)
        if resource is not None:
            if record:
                resource.references[occurrence.path] += 1
        elif belongs_to_selected_folder(occurrence.path, prefixes):
            missing.append(occurrence.path)
    return occurrences, missing


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def hash_size_collisions(resources: list[ResourceFile]) -> None:
    by_size: dict[int, list[ResourceFile]] = defaultdict(list)
    for resource in resources:
        by_size[resource.size].append(resource)
    candidates = [resource for group in by_size.values() if len(group) > 1 for resource in group]
    if not candidates:
        return
    print(f"\nHashing {len(candidates)} files that share a size...")
    for index, resource in enumerate(candidates, start=1):
        print(f"  [{index}/{len(candidates)}] {resource.path.name}")
        resource.digest = sha256_file(resource.path)


def choose_survivor(group: list[ResourceFile]) -> ResourceFile:
    # Prefer a file already referenced by the selected text.  This avoids an
    # unnecessary redirect when only an unreferenced duplicate sorts first.
    return min(
        group,
        key=lambda resource: (
            0 if resource.reference_count else 1,
            resource.path.name.casefold(),
            resource.path.name,
        ),
    )


def build_duplicate_groups(resources: list[ResourceFile]) -> list[DuplicateGroup]:
    by_identity: dict[tuple[int, str], list[ResourceFile]] = defaultdict(list)
    for resource in resources:
        if resource.digest is not None:
            by_identity[(resource.size, resource.digest)].append(resource)

    result: list[DuplicateGroup] = []
    for group in by_identity.values():
        if len(group) < 2:
            continue
        survivor = choose_survivor(group)
        redundant = tuple(
            sorted(
                (resource for resource in group if resource is not survivor),
                key=lambda resource: (resource.path.name.casefold(), resource.path.name),
            )
        )
        result.append(DuplicateGroup(survivor, redundant))
    result.sort(key=lambda item: (item.survivor.path.name.casefold(), item.survivor.path.name))
    return result


def survivor_alias(old_alias: str, old: ResourceFile, survivor: ResourceFile) -> str:
    if old_alias == old.path.name:
        return survivor.path.name
    if not old_alias.endswith(old.path.name):
        raise ValueError(f"Reference does not end with its filename: {old_alias}")
    return old_alias[: -len(old.path.name)] + survivor.path.name


def build_redirects(groups: list[DuplicateGroup]) -> dict[str, str]:
    redirects: dict[str, str] = {}
    for group in groups:
        for redundant in group.redundant:
            for old_alias in redundant.references:
                replacement = survivor_alias(old_alias, redundant, group.survivor)
                existing = redirects.get(old_alias)
                if existing is not None and existing != replacement:
                    raise RuntimeError(f"Ambiguous redirect for {old_alias}: {existing} or {replacement}")
                redirects[old_alias] = replacement
    return redirects


def apply_redirects(
    text: str,
    redirects: dict[str, str],
    occurrences: list[ReferenceOccurrence],
) -> tuple[str, Counter[str]]:
    if not redirects:
        return text, Counter()
    counts: Counter[str] = Counter()
    parts: list[str] = []
    cursor = 0
    for occurrence in occurrences:
        replacement = redirects.get(occurrence.path)
        if replacement is None:
            continue
        parts.append(text[cursor : occurrence.start])
        parts.append(replacement)
        cursor = occurrence.end
        counts[occurrence.path] += 1
    parts.append(text[cursor:])
    return "".join(parts), counts


def unique_quarantine_folder(resource_folder: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = resource_folder / f"{QUARANTINE_PREFIX}{timestamp}"
    suffix = 2
    while candidate.exists():
        candidate = resource_folder / f"{QUARANTINE_PREFIX}{timestamp}_{suffix}"
        suffix += 1
    return candidate


def verify_unchanged(path: Path, expected_size: int, expected_mtime_ns: int, label: str) -> None:
    stat = path.stat()
    if stat.st_size != expected_size or stat.st_mtime_ns != expected_mtime_ns:
        raise RuntimeError(f"{label} changed while the scan was running: {path}")


def verify_resources(groups: list[DuplicateGroup]) -> None:
    checked: set[Path] = set()
    for group in groups:
        for resource in (group.survivor, *group.redundant):
            if resource.path in checked:
                continue
            verify_unchanged(resource.path, resource.size, resource.mtime_ns, "Resource file")
            if sha256_file(resource.path) != resource.digest:
                raise RuntimeError(f"Resource contents changed while the scan was running: {resource.path}")
            checked.add(resource.path)


def atomic_replace_text(document: TextDocument, new_text: str) -> None:
    payload = document.encode(new_text)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{document.path.name}.dedupe-",
            suffix=".tmp",
            dir=document.path.parent,
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copystat(document.path, temp_path)
        os.replace(temp_path, document.path)
        temp_path = None
        if document.path.read_bytes() != payload:
            raise RuntimeError("The rewritten text file did not pass byte-for-byte verification.")
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def write_report(
    report_path: Path,
    document: TextDocument,
    resource_folder: Path,
    groups: list[DuplicateGroup],
    replacement_counts: Counter[str],
    moved: list[tuple[ResourceFile, Path]],
    move_failures: list[tuple[ResourceFile, str]],
) -> None:
    lines = [
        "Resource deduplication report",
        f"Time: {datetime.now().isoformat(timespec='seconds')}",
        f"Text file: {document.path}",
        f"Resource folder: {resource_folder}",
        f"Duplicate groups: {len(groups)}",
        f"Files quarantined: {len(moved)}",
        f"Move failures: {len(move_failures)}",
        "",
        "Redirects and duplicate files:",
    ]
    for index, group in enumerate(groups, start=1):
        lines.extend(
            [
                "",
                f"[{index}] KEEP: {group.survivor.path.name}",
                f"    SHA256: {group.survivor.digest}",
            ]
        )
        for redundant in group.redundant:
            status = "QUARANTINED" if any(item is redundant for item, _ in moved) else "NOT MOVED"
            lines.append(f"    {status}: {redundant.path.name} ({redundant.size} bytes)")
            if redundant.references:
                for old_alias, detected_count in redundant.references.items():
                    new_alias = survivor_alias(old_alias, redundant, group.survivor)
                    lines.append(
                        f"        {old_alias} -> {new_alias} "
                        f"(detected {detected_count}, replaced {replacement_counts[old_alias]})"
                    )
            else:
                lines.append("        No reference found in the selected text file.")
    if move_failures:
        lines.extend(["", "Move failures:"])
        for resource, message in move_failures:
            lines.append(f"  {resource.path}: {message}")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_inventory(resources: list[ResourceFile], document: TextDocument, folder: Path) -> None:
    referenced = [resource for resource in resources if resource.reference_count]
    print("\nReference inventory:")
    print(f"  Text file:       {document.path}")
    print(f"  Resource folder: {folder}")
    print(f"  Files found:     {len(resources)}")
    print(f"  Referenced:      {len(referenced)}")
    print(f"  Not referenced:  {len(resources) - len(referenced)}")
    print("  Matching:        active 'filename = relative/path' assignments only")
    print("  Path comparison: exact and case-sensitive")


def print_plan(groups: list[DuplicateGroup]) -> None:
    redundant_count = sum(len(group.redundant) for group in groups)
    saved_bytes = sum(resource.size for group in groups for resource in group.redundant)
    print("\n" + "=" * 72)
    print(f"DUPLICATE PLAN: {len(groups)} group(s), {redundant_count} file(s) to quarantine")
    print(f"Potential saving: {saved_bytes:,} bytes ({saved_bytes / (1024 * 1024):.2f} MiB)")
    for index, group in enumerate(groups, start=1):
        print(f"\n[{index}] KEEP: {group.survivor.path.name}")
        for redundant in group.redundant:
            refs = redundant.reference_count
            print(f"    QUARANTINE: {redundant.path.name}  (references to redirect: {refs})")
    print("\nUnique unreferenced files are reported but are NOT quarantined.")


def confirm_plan() -> bool:
    print("\nSafe operation order:")
    print("  1. Create quarantine and copy the original text file into it.")
    print("  2. Atomically rewrite and verify the text file.")
    print("  3. Move only redundant byte-identical files into quarantine.")
    while True:
        answer = input("\nEnter 1 to continue, or 0 to cancel: ").strip()
        if answer == "1":
            return True
        if answer == "0":
            return False
        print("Please enter 1 or 0.")


def read_line_or_escape(prompt: str) -> str | None:
    """Read a console line, returning None as soon as Escape is pressed."""
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
    print("OPTIONAL MANUAL SIMILAR-FILE DEDUPLICATION")
    print("Use this only when you have visually confirmed that different files are interchangeable.")
    while True:
        answer = read_line_or_escape("Press ENTER to create a manual group, or ESC to finish: ")
        if answer is None:
            return False
        if answer.strip() == "0":  # Also supports redirected/non-interactive input.
            return False
        if not answer.strip() or answer.strip() == "1":
            return True
        print("Press Enter without typing anything, or press Escape.")


def collect_manual_paths(resource_folder: Path) -> list[Path] | None:
    print("\nDrag or paste files from the selected resource folder one at a time.")
    print("Press Enter on an empty line to finish this group; press ESC to exit manual mode.")
    selected: list[Path] = []
    selected_keys: set[str] = set()
    required_extension: str | None = None
    while True:
        raw = read_line_or_escape(f"File {len(selected) + 1}> ")
        if raw is None:
            return None
        if not raw.strip():
            if len(selected) >= 2:
                return selected
            if selected:
                print("A manual group needs at least two files; this incomplete group was discarded.")
            else:
                print("No files were added; the empty group was discarded.")
            return []

        candidate = clean_pasted_path(raw)
        try:
            candidate = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            print("  [ERROR] File not found. Try again.")
            continue
        if not candidate.is_file() or candidate.is_symlink():
            print("  [ERROR] Select a regular file, not a folder or symbolic link.")
            continue
        if candidate.parent != resource_folder:
            print("  [ERROR] The file must be directly inside the selected resource folder.")
            continue
        key = str(candidate).casefold()
        if key in selected_keys:
            print("  [ERROR] That file is already in this group.")
            continue
        extension = candidate.suffix.casefold()
        if required_extension is None:
            required_extension = extension
        elif extension != required_extension:
            print(f"  [ERROR] All files in one manual group must use the same extension ({required_extension}).")
            continue
        selected.append(candidate)
        selected_keys.add(key)
        print(f"  [ADDED] {candidate.name} ({candidate.stat().st_size:,} bytes)")


def choose_largest_manual_survivor(resources: list[ResourceFile]) -> ResourceFile:
    return min(
        resources,
        key=lambda resource: (-resource.size, resource.path.name.casefold(), resource.path.name),
    )


def confirm_manual_group(group: DuplicateGroup) -> bool | None:
    print("\nManual group plan:")
    print(f"  KEEP (largest): {group.survivor.path.name} ({group.survivor.size:,} bytes)")
    for resource in group.redundant:
        print(
            f"  QUARANTINE:     {resource.path.name} ({resource.size:,} bytes; "
            f"references to redirect: {resource.reference_count})"
        )
    print("WARNING: These files are not hash-identical. The script trusts your manual grouping.")
    while True:
        answer = read_line_or_escape("Enter 1 to process this group, 0 to discard it, or ESC to exit: ")
        if answer is None:
            return None
        if answer.strip() == "1":
            return True
        if answer.strip() == "0":
            return False
        print("Please enter 1, 0, or press Escape.")


def append_manual_report(
    report: Path,
    group_number: int,
    group: DuplicateGroup,
    replacement_counts: Counter[str],
    moved: list[tuple[ResourceFile, Path]],
    move_failures: list[tuple[ResourceFile, str]],
) -> None:
    lines = [
        "",
        f"MANUAL GROUP {group_number} ({datetime.now().isoformat(timespec='seconds')})",
        f"KEEP LARGEST: {group.survivor.path.name} ({group.survivor.size} bytes)",
    ]
    moved_resources = {id(resource) for resource, _ in moved}
    for resource in group.redundant:
        status = "QUARANTINED" if id(resource) in moved_resources else "NOT MOVED"
        lines.append(f"  {status}: {resource.path.name} ({resource.size} bytes)")
        if resource.references:
            for old_alias, detected_count in resource.references.items():
                replacement = survivor_alias(old_alias, resource, group.survivor)
                lines.append(
                    f"    {old_alias} -> {replacement} "
                    f"(detected {detected_count}, replaced {replacement_counts[old_alias]})"
                )
        else:
            lines.append("    No reference found in the selected text file.")
    for resource, message in move_failures:
        lines.append(f"  MOVE FAILED: {resource.path}: {message}")
    with report.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def final_reference_check(text_path: Path, resource_folder: Path) -> bool:
    """Ensure selected-folder filename assignments resolve before the tool exits."""
    print("\nRunning final selected-folder reference check...")
    try:
        document = decode_text_file(text_path)
        resources = scan_resources(resource_folder, document)
        resources = [resource for resource in resources if resource.path != text_path]
        _, missing = collect_reference_inventory(
            document,
            resources,
            resource_folder,
            record=True,
        )
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        print(f"[FINAL CHECK FAILED] Could not validate the current text and resources: {exc}")
        return False

    referenced_occurrences = sum(resource.reference_count for resource in resources)
    if missing:
        counts = Counter(missing)
        print(
            f"[FINAL CHECK FAILED] {sum(counts.values())} filename assignment(s) "
            "point to missing files in the selected folder:"
        )
        for path_value in sorted(counts, key=lambda value: (value.casefold(), value)):
            suffix = f" ({counts[path_value]} occurrences)" if counts[path_value] > 1 else ""
            print(f"  {path_value}{suffix}")
        print("References to other relative folders were intentionally not checked.")
        return False

    print(
        f"[FINAL CHECK PASSED] All {referenced_occurrences} selected-folder filename "
        "assignment(s) resolve to existing files."
    )
    print("References to other relative folders were intentionally not checked.")
    return True


def run_manual_mode(
    document: TextDocument,
    resource_folder: Path,
    quarantine: Path | None,
    backup: Path | None,
    report: Path | None,
) -> int:
    if not prompt_manual_mode():
        print("Manual mode finished.")
        return 0 if final_reference_check(document.path, resource_folder) else 1

    completed_groups = 0
    moved_total = 0
    failure_total = 0
    while True:
        selected_paths = collect_manual_paths(resource_folder)
        if selected_paths is None:
            break
        if not selected_paths:
            if not prompt_manual_mode():
                break
            continue

        try:
            current_document = decode_text_file(document.path)
            current_resources = scan_resources(resource_folder, current_document)
            current_resources = [resource for resource in current_resources if resource.path != document.path]
            occurrences, missing = collect_reference_inventory(
                current_document,
                current_resources,
                resource_folder,
                record=True,
            )
            if missing:
                raise RuntimeError(
                    "Current text contains unresolved selected-folder references: "
                    + ", ".join(sorted(set(missing)))
                )
            by_path = {str(resource.path).casefold(): resource for resource in current_resources}
            selected_resources = [by_path[str(path).casefold()] for path in selected_paths]
        except KeyError as exc:
            print(f"\n[ERROR] A selected file is no longer available: {exc}")
            failure_total += 1
            break
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            print(f"\n[ERROR] Could not prepare the manual group safely: {exc}")
            failure_total += 1
            break

        survivor = choose_largest_manual_survivor(selected_resources)
        redundant = tuple(
            sorted(
                (resource for resource in selected_resources if resource is not survivor),
                key=lambda resource: (resource.path.name.casefold(), resource.path.name),
            )
        )
        group = DuplicateGroup(survivor, redundant)
        confirmation = confirm_manual_group(group)
        if confirmation is None:
            break
        if not confirmation:
            print("Manual group discarded. Nothing was changed.")
            if not prompt_manual_mode():
                break
            continue

        redirects = build_redirects([group])
        new_text, replacement_counts = apply_redirects(current_document.text, redirects, occurrences)
        expected = sum(resource.reference_count for resource in group.redundant)
        if sum(replacement_counts.values()) != expected:
            print("[ERROR] Manual replacement preview was inconsistent. Nothing was changed.")
            failure_total += 1
            break
        try:
            verify_unchanged(
                current_document.path,
                current_document.size,
                current_document.mtime_ns,
                "Text file",
            )
            for resource in selected_resources:
                verify_unchanged(resource.path, resource.size, resource.mtime_ns, "Resource file")
        except (OSError, RuntimeError) as exc:
            print(f"[ERROR] Manual preflight failed: {exc}. Nothing was changed.")
            failure_total += 1
            break

        if quarantine is None:
            quarantine = unique_quarantine_folder(resource_folder)
            backup = quarantine / f"{current_document.path.name}.backup"
            report = quarantine / "DeduplicateResources-report.txt"
            try:
                quarantine.mkdir()
                shutil.copy2(current_document.path, backup)
                report.write_text(
                    "Resource deduplication report\n"
                    f"Time: {datetime.now().isoformat(timespec='seconds')}\n"
                    f"Text file: {current_document.path}\n"
                    f"Resource folder: {resource_folder}\n"
                    "Automatic duplicate groups: 0\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                print(f"[ERROR] Could not create recovery data: {exc}. Nothing was changed.")
                failure_total += 1
                break

        if new_text != current_document.text:
            try:
                atomic_replace_text(current_document, new_text)
                rewritten = decode_text_file(current_document.path)
                rewritten_occurrences, rewritten_missing = collect_reference_inventory(
                    rewritten,
                    current_resources,
                    resource_folder,
                    record=False,
                )
                old_remaining = [item.path for item in rewritten_occurrences if item.path in redirects]
                if rewritten_missing or old_remaining:
                    raise RuntimeError("Manual redirect semantic validation failed.")
            except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
                print(f"[ERROR] Manual text update failed: {exc}")
                try:
                    rollback_document = decode_text_file(current_document.path)
                    atomic_replace_text(rollback_document, current_document.text)
                    print("The pre-group text was restored from memory.")
                except (OSError, RuntimeError, UnicodeError) as rollback_exc:
                    print(f"[WARNING] Automatic pre-group restore failed: {rollback_exc}")
                    if backup is not None:
                        print(f"The original run backup remains at: {backup}")
                failure_total += 1
                break

        moved: list[tuple[ResourceFile, Path]] = []
        move_failures: list[tuple[ResourceFile, str]] = []
        assert quarantine is not None
        for resource in group.redundant:
            destination = quarantine / resource.path.name
            try:
                shutil.move(str(resource.path), str(destination))
                moved.append((resource, destination))
                print(f"[MANUAL QUARANTINED] {resource.path.name}")
            except OSError as exc:
                move_failures.append((resource, str(exc)))
                print(f"[MANUAL MOVE FAILED] {resource.path.name}: {exc}")

        completed_groups += 1
        moved_total += len(moved)
        failure_total += len(move_failures)
        if report is not None:
            try:
                append_manual_report(
                    report,
                    completed_groups,
                    group,
                    replacement_counts,
                    moved,
                    move_failures,
                )
            except OSError as exc:
                print(f"[WARNING] Could not append the manual group to the report: {exc}")

        print(
            f"Manual group complete: kept {survivor.path.name}; "
            f"quarantined {len(moved)} file(s)."
        )
        if not prompt_manual_mode():
            break

    final_check_passed = final_reference_check(document.path, resource_folder)
    if not final_check_passed:
        failure_total += 1

    print("\nManual mode summary:")
    print(f"  Groups completed:  {completed_groups}")
    print(f"  Files quarantined: {moved_total}")
    print(f"  Failures:          {failure_total}")
    if quarantine is not None:
        print(f"  Quarantine:        {quarantine}")
    print("No additional INI backup was created during manual groups.")
    return 1 if failure_total else 0


def run() -> int:
    print("=" * 72)
    print("RESOURCE FILE DEDUPLICATOR")
    print("=" * 72)
    print("This tool redirects one text/config file, then quarantines duplicate resources.")
    print("It scans only files directly inside the selected resource folder.\n")

    text_path = prompt_existing_path(
        "Paste or drag the TEXT / CONFIG FILE here, then press Enter:\n> ",
        want_directory=False,
    )
    try:
        document = decode_text_file(text_path)
    except (OSError, UnicodeError) as exc:
        print(f"\n[ERROR] Could not read the text file safely: {exc}")
        return 1

    resource_folder = prompt_existing_path(
        "\nPaste or drag the RESOURCE FOLDER here, then press Enter:\n> ",
        want_directory=True,
    )
    if text_path.parent == resource_folder and text_path.is_file():
        print("\nNote: The selected text file is in the resource folder and will be excluded from scanning.")

    try:
        resources = scan_resources(resource_folder, document)
        resources = [resource for resource in resources if resource.path != text_path]
        occurrences, missing_references = collect_reference_inventory(
            document,
            resources,
            resource_folder,
            record=True,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"\n[ERROR] Could not build the reference inventory: {exc}")
        return 1
    if not resources:
        print("\nNo regular files were found directly inside that folder.")
        return 0

    print_inventory(resources, document, resource_folder)
    reference_count = sum(resource.reference_count for resource in resources)
    if missing_references:
        print("\n[ERROR] The text file contains references inside the selected folder that do not resolve:")
        for path_value in sorted(set(missing_references), key=lambda value: (value.casefold(), value)):
            print(f"  {path_value}")
        print("Nothing was hashed or changed. Fix the paths or select the matching resource folder.")
        return 1
    if reference_count == 0:
        print(
            "\n[ERROR] No active filename assignments point into the selected resource folder.\n"
            "Nothing was hashed or changed. Check that the text file and folder belong together."
        )
        return 1

    try:
        hash_size_collisions(resources)
        groups = build_duplicate_groups(resources)
    except OSError as exc:
        print(f"\n[ERROR] Hashing failed: {exc}")
        return 1
    if not groups:
        print("\nNo byte-identical duplicate files were found automatically.")
        return run_manual_mode(document, resource_folder, None, None, None)

    redirects = build_redirects(groups)
    new_text, replacement_counts = apply_redirects(document.text, redirects, occurrences)
    expected_replacements = sum(
        resource.reference_count for group in groups for resource in group.redundant
    )
    actual_replacements = sum(replacement_counts.values())
    if expected_replacements != actual_replacements:
        print(
            "\n[ERROR] The replacement preview was inconsistent "
            f"({expected_replacements} expected, {actual_replacements} prepared). Nothing was changed."
        )
        return 1

    print_plan(groups)
    print(f"\nText references to redirect: {actual_replacements}")
    unreferenced_duplicates = sum(
        1 for group in groups for resource in group.redundant if not resource.references
    )
    if unreferenced_duplicates:
        print(
            f"WARNING: {unreferenced_duplicates} duplicate file(s) have no reference in the selected text.\n"
            "They will still be quarantined because an identical survivor will remain."
        )
    if not confirm_plan():
        print("\nCancelled. Nothing was changed.")
        return 0

    try:
        verify_unchanged(document.path, document.size, document.mtime_ns, "Text file")
        verify_resources(groups)
    except (OSError, RuntimeError) as exc:
        print(f"\n[ERROR] Preflight verification failed: {exc}\nNothing was changed.")
        return 1

    quarantine = unique_quarantine_folder(resource_folder)
    backup = quarantine / f"{document.path.name}.backup"
    report = quarantine / "DeduplicateResources-report.txt"
    try:
        quarantine.mkdir()
        shutil.copy2(document.path, backup)
    except OSError as exc:
        print(f"\n[ERROR] Could not create recovery data: {exc}\nNothing was changed.")
        try:
            if quarantine.exists() and not any(quarantine.iterdir()):
                quarantine.rmdir()
        except OSError:
            pass
        return 1

    if new_text != document.text:
        try:
            atomic_replace_text(document, new_text)
            rewritten_document = decode_text_file(document.path)
            rewritten_occurrences, rewritten_missing = collect_reference_inventory(
                rewritten_document,
                resources,
                resource_folder,
                record=False,
            )
            old_paths_remaining = [
                occurrence.path for occurrence in rewritten_occurrences if occurrence.path in redirects
            ]
            if rewritten_missing:
                raise RuntimeError(
                    "Rewritten text contains unresolved selected-folder references: "
                    + ", ".join(sorted(set(rewritten_missing)))
                )
            if old_paths_remaining:
                raise RuntimeError(
                    "Rewritten text still contains old duplicate paths: "
                    + ", ".join(sorted(set(old_paths_remaining)))
                )
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            print(f"\n[ERROR] Text update failed: {exc}")
            try:
                shutil.copy2(backup, document.path)
                print("The original text file was restored from its backup.")
            except OSError as restore_exc:
                print(f"[WARNING] Automatic restore also failed: {restore_exc}")
                print(f"The original backup is safe at: {backup}")
            return 1
        print(f"\n[OK] Redirected and verified {actual_replacements} text reference(s).")
    else:
        print("\n[OK] No text redirects were necessary; the original text was backed up.")

    moved: list[tuple[ResourceFile, Path]] = []
    move_failures: list[tuple[ResourceFile, str]] = []
    for group in groups:
        for resource in group.redundant:
            destination = quarantine / resource.path.name
            try:
                shutil.move(str(resource.path), str(destination))
                moved.append((resource, destination))
                print(f"[QUARANTINED] {resource.path.name}")
            except OSError as exc:
                move_failures.append((resource, str(exc)))
                print(f"[MOVE FAILED] {resource.path.name}: {exc}")

    try:
        write_report(
            report,
            document,
            resource_folder,
            groups,
            replacement_counts,
            moved,
            move_failures,
        )
    except OSError as exc:
        print(f"[WARNING] Could not write the report: {exc}")

    print("\n" + "=" * 72)
    print(f"AUTOMATIC PHASE COMPLETE: {len(moved)} duplicate file(s) quarantined")
    if move_failures:
        print(f"WARNING: {len(move_failures)} file(s) could not be moved; see the messages above.")
    print(f"Quarantine: {quarantine}")
    print(f"Text backup: {backup}")
    if report.exists():
        print(f"Report:      {report}")
    print("Nothing in quarantine is automatically deleted.")
    manual_status = run_manual_mode(document, resource_folder, quarantine, backup, report)
    return 1 if move_failures or manual_status else 0


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
