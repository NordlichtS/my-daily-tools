#!/usr/bin/env python3
"""Double-clickable batch DDS downsizer powered by Microsoft DirectXTex."""

from __future__ import annotations

import ctypes
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


MIN_SIZE = 32
MAX_SIZE = 8192
RESTART_REQUESTED = 1000
METADATA_KEYS = {
    "width": "width",
    "height": "height",
    "depth": "depth",
    "miplevels": "mip_levels",
    "arraysize": "array_size",
    "format": "format",
    "dimension": "dimension",
    "alpha mode": "alpha_mode",
}
ALPHA_MODE_VALUES = {
    "Unknown": 0,
    "Straight": 1,
    "Premultiplied": 2,
    "Opaque": 3,
    "Custom": 4,
}
SRGB_TO_LINEAR_DXGI = {
    "R8G8B8A8_UNORM_SRGB": (29, 28),
    "BC1_UNORM_SRGB": (72, 71),
    "BC2_UNORM_SRGB": (75, 74),
    "BC3_UNORM_SRGB": (78, 77),
    "B8G8R8A8_UNORM_SRGB": (91, 87),
    "B8G8R8X8_UNORM_SRGB": (93, 88),
    "BC7_UNORM_SRGB": (99, 98),
}


@dataclass(frozen=True)
class TextureMetadata:
    width: int
    height: int
    depth: int
    mip_levels: int
    array_size: int
    format: str
    dimension: str
    alpha_mode: str

    @property
    def is_srgb(self) -> bool:
        return self.format.upper().endswith("_SRGB")


@dataclass(frozen=True)
class TextureJob:
    source: Path
    source_metadata: TextureMetadata
    staged: Path
    output_metadata: TextureMetadata


@dataclass(frozen=True)
class PlannedTexture:
    source: Path
    source_metadata: TextureMetadata
    output_width: int
    output_height: int
    target_format: str


class RunLog:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, message: str = "") -> None:
        print(message)
        self.lines.append(message)

    def save_to(self, staging_directories: Iterable[Path]) -> None:
        text = "\n".join(self.lines) + "\n"
        for directory in staging_directories:
            try:
                (directory / "ResizeDDS.log").write_text(text, encoding="utf-8")
            except OSError as exc:
                print(f"[WARNING] Could not write log in {directory}: {exc}")


def find_directxtex_tool(name: str) -> Path:
    executable = f"{name}.exe"
    local_tool = Path(__file__).resolve().parent / executable
    path_tool = shutil.which(executable)
    candidates = [local_tool]
    if path_tool:
        resolved_path_tool = Path(path_tool).resolve()
        if resolved_path_tool != local_tool:
            candidates.append(resolved_path_tool)

    failures: list[str] = []
    for tool in candidates:
        if not tool.is_file():
            continue
        try:
            probe = subprocess.run(
                [str(tool), "--version"],
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{tool}: {exc}")
            continue
        if probe.returncode == 0:
            return tool
        failures.append(f"{tool}: version check returned {probe.returncode}")

    details = f" Tried: {'; '.join(failures)}" if failures else ""
    raise FileNotFoundError(
        f"{executable} was not usable. Put it beside ResizeDDS.py or add it to PATH."
        f"{details}"
    )


def run_command(arguments: list[str | Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(argument) for argument in arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def read_metadata(texdiag: Path, texture: Path) -> TextureMetadata:
    result = run_command([texdiag, "info", "-nologo", "--", texture])
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(details or f"texdiag exited with code {result.returncode}")

    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*([^=]+?)\s*=\s*(.*?)\s*$", line)
        if not match:
            continue
        key = match.group(1).strip().lower()
        attribute = METADATA_KEYS.get(key)
        if attribute:
            values[attribute] = match.group(2).strip()

    missing = sorted(set(METADATA_KEYS.values()) - values.keys())
    if missing:
        raise RuntimeError(f"texdiag output is missing: {', '.join(missing)}")

    try:
        return TextureMetadata(
            width=int(values["width"]),
            height=int(values["height"]),
            depth=int(values["depth"]),
            mip_levels=int(values["mip_levels"]),
            array_size=int(values["array_size"]),
            format=values["format"].upper(),
            dimension=values["dimension"],
            alpha_mode=values["alpha_mode"],
        )
    except ValueError as exc:
        raise RuntimeError(f"Could not parse texdiag numeric metadata: {exc}") from exc


def split_windows_input(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw:
        return []

    whole = raw.strip('"')
    if Path(whole).exists():
        return [whole]

    if os.name != "nt":
        import shlex

        return shlex.split(raw)

    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    count = ctypes.c_int()
    pointer = command_line_to_argv(f"ResizeDDS {raw}", ctypes.byref(count))
    if not pointer:
        raise OSError("Windows could not parse the dropped paths.")
    try:
        return [pointer[index] for index in range(1, count.value)]
    finally:
        local_free(pointer)


def collect_dds_files(inputs: Iterable[str]) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for raw_path in inputs:
        candidate = Path(raw_path.strip().strip('"')).expanduser()
        try:
            candidate = candidate.resolve()
        except OSError:
            pass

        if candidate.is_dir():
            candidates = sorted(
                (item for item in candidate.iterdir() if item.is_file() and item.suffix.lower() == ".dds"),
                key=lambda item: item.name.casefold(),
            )
            if not candidates:
                warnings.append(f"No top-level DDS files found in: {candidate}")
        elif candidate.is_file():
            if candidate.suffix.lower() != ".dds":
                warnings.append(f"Ignored non-DDS file: {candidate}")
                continue
            candidates = [candidate]
        else:
            warnings.append(f"Path not found: {candidate}")
            continue

        for texture in candidates:
            identity = os.path.normcase(str(texture.resolve()))
            if identity not in seen:
                seen.add(identity)
                files.append(texture.resolve())

    return files, warnings


def prompt_resolution() -> int:
    while True:
        raw = input(
            f"Target square resolution ({MIN_SIZE}-{MAX_SIZE}, power of two; 0 = preserve each original size): "
        ).strip()
        try:
            value = int(raw)
        except ValueError:
            print("Enter 0 or a number such as 512, 1024, or 2048.")
            continue
        if value == 0:
            return 0
        if MIN_SIZE <= value <= MAX_SIZE and value & (value - 1) == 0:
            return value
        print(f"Resolution must be 0 or a power of two from {MIN_SIZE} through {MAX_SIZE}.")


def prompt_small_texture_policy() -> int:
    print("\nSmaller/equal image policy (applies when target size is not 0):")
    print("  0 = Ignore them completely")
    print("  1 = Keep their original dimensions, but recompress and apply the selected mip policy")
    print("  2 = Upscale them to the square target size")
    while True:
        raw = input("Choose 0, 1, or 2: ").strip()
        if raw in {"0", "1", "2"}:
            return int(raw)
        print("Enter 0, 1, or 2.")


def prompt_non_square_policy(target_size: int) -> int:
    print("\nNon-square source policy:")
    print("  0 = Ignore non-square images; process square images only")
    if target_size == 0:
        print("  1 = Process non-square images while preserving their original dimensions")
        print("  2 = Independently floor width and height to powers of two not exceeding each source dimension")
    else:
        print(f"  1 = Stretch every non-square image to {target_size}x{target_size}")
        print(
            "  2 = Independently floor width and height to powers of two not exceeding "
            f"the source dimensions or {target_size}"
        )
    while True:
        raw = input("Choose 0, 1, or 2: ").strip()
        if raw in {"0", "1", "2"}:
            return int(raw)
        print("Enter 0, 1, or 2.")


def prompt_compression() -> int:
    print("\nTarget compression:")
    print("  0 = Preserve each original DXGI format")
    print("  1 = BC1  | RGB + optional 1-bit alpha                 | 4 bpp")
    print("  2 = BC2  | RGB + explicit 4-bit alpha                 | 8 bpp")
    print("  3 = BC3  | RGB + interpolated alpha                   | 8 bpp")
    print("  4 = BC4  | One linear channel                         | 4 bpp")
    print("  5 = BC5  | Two linear channels (often normal-map XY)  | 8 bpp")
    print("  6 = BC6H | Three-channel HDR, unsigned half-float     | 8 bpp")
    print("  7 = BC7  | High-quality RGB/RGBA                      | 8 bpp")
    print("  8 = RGBA8888 | Uncompressed RGBA                      | 32 bpp")
    print("  Note: BC4/BC5/BC6H are linear-only; BC1 may reduce alpha to 1 bit")
    while True:
        raw = input("Choose 0 through 8: ").strip()
        if raw in {str(value) for value in range(9)}:
            return int(raw)
        print("Invalid compression choice. Enter a number from 0 through 8.")


def prompt_srgb_mode() -> int:
    print("\nsRGB handling:")
    print("  0 = Preserve each source's sRGB or linear classification")
    print("  1 = Treat every source as linear without converting its stored color values")
    print("  2 = Convert sRGB color values to linear; write linear output for every source")
    while True:
        raw = input("Choose 0, 1, or 2: ").strip()
        if raw in {"0", "1", "2"}:
            return int(raw)
        print("Enter 0, 1, or 2.")


def prompt_mipmap_mode() -> int:
    print("\nMipmap mode:")
    print("  0 = Keep single-mip sources single; regenerate a full chain for sources with mipmaps")
    print("  1 = Always generate a full mip chain")
    print("  2 = Top level only; remove mip chains")
    while True:
        raw = input("Choose 0, 1, or 2: ").strip()
        if raw in {"0", "1", "2"}:
            return int(raw)
        print("Enter 0, 1, or 2.")


def compression_label(compression: int) -> str:
    if compression == 0:
        return "OriginalFormat"
    if compression == 8:
        return "RGBA8888"
    return f"BC{compression}"


def supports_uniform_bc_weighting(target_format: str) -> bool:
    """Return whether DirectXTex supports -bc u for this output format."""
    normalized = target_format.upper()
    return normalized.startswith(("BC1_", "BC2_", "BC3_")) or normalized in {
        "DXT1",
        "DXT2",
        "DXT3",
        "DXT4",
        "DXT5",
        "BC3N",
        "DXT5NM",
        "RXGB",
    }


def output_format(compression: int, source_metadata: TextureMetadata, srgb_mode: int) -> str:
    output_is_srgb = source_metadata.is_srgb and srgb_mode == 0
    if compression == 0:
        return source_metadata.format if output_is_srgb else source_metadata.format.removesuffix("_SRGB")
    if compression == 8:
        return "R8G8B8A8_UNORM_SRGB" if output_is_srgb else "R8G8B8A8_UNORM"
    if compression in {1, 2, 3, 7}:
        suffix = "_UNORM_SRGB" if output_is_srgb else "_UNORM"
        return f"BC{compression}{suffix}"
    if output_is_srgb:
        raise RuntimeError(f"BC{compression} has no sRGB DXGI format, so the sRGB label cannot be preserved")
    if compression == 4:
        return "BC4_UNORM"
    if compression == 5:
        return "BC5_UNORM"
    if compression == 6:
        return "BC6H_UF16"
    raise RuntimeError(f"unsupported compression selection: {compression}")


def mip_argument(mode: int, source_metadata: TextureMetadata) -> int:
    if mode == 1:
        return 0
    if mode == 2:
        return 1
    return 1 if source_metadata.mip_levels == 1 else 0


def expected_mip_levels(
    mode: int, source_metadata: TextureMetadata, output_width: int, output_height: int
) -> int:
    if mode == 2 or (mode == 0 and source_metadata.mip_levels == 1):
        return 1
    return int(math.floor(math.log2(max(output_width, output_height, source_metadata.depth)))) + 1


def planned_dimensions(
    source_metadata: TextureMetadata,
    target_size: int,
    small_policy: int,
    non_square_policy: int,
) -> tuple[int, int]:
    is_non_square = source_metadata.width != source_metadata.height
    if is_non_square and non_square_policy == 2:
        width_cap = source_metadata.width if target_size == 0 else min(source_metadata.width, target_size)
        height_cap = source_metadata.height if target_size == 0 else min(source_metadata.height, target_size)
        return floor_power_of_two(width_cap), floor_power_of_two(height_cap)
    if target_size == 0:
        return source_metadata.width, source_metadata.height
    if is_non_square and non_square_policy == 1:
        return target_size, target_size
    is_small_or_equal = source_metadata.width <= target_size and source_metadata.height <= target_size
    if is_small_or_equal and small_policy == 1:
        return source_metadata.width, source_metadata.height
    return target_size, target_size


def floor_power_of_two(value: int) -> int:
    if value < 1:
        raise ValueError("texture dimensions must be positive")
    return 1 << (value.bit_length() - 1)


def prompt_conversion_confirmation(convert_count: int, ignored_count: int) -> bool:
    print("\n" + "=" * 62)
    print(f"Will convert: {convert_count}")
    print(f"Will ignore:  {ignored_count}")
    while True:
        raw = input("Enter 1 to continue conversion, or 0 to abort: ").strip()
        if raw == "1":
            return True
        if raw == "0":
            return False
        print("Enter 1 to continue or 0 to abort.")


def prompt_staging_action(install_available: bool) -> int:
    print("\nFinal action:")
    print("  0 = Keep the staging folder; do not change originals")
    print("  1 = Install validated replacements, then delete the staging folder")
    print("  2 = Delete the staging folder and start over")
    while True:
        raw = input("Choose 0, 1, or 2: ").strip()
        if raw not in {"0", "1", "2"}:
            print("Enter 0, 1, or 2.")
            continue
        selected = int(raw)
        if selected == 1 and not install_available:
            print("No validated outputs are available to install. Choose 0 or 2.")
            continue
        return selected


def validate_output(
    source: TextureMetadata,
    output: TextureMetadata,
    output_width: int,
    output_height: int,
    target_format: str,
    mip_mode: int,
    expected_srgb: bool,
) -> list[str]:
    problems: list[str] = []
    expected_mips = expected_mip_levels(mip_mode, source, output_width, output_height)
    comparisons = [
        ("width", output.width, output_width),
        ("height", output.height, output_height),
        ("format", output.format, target_format),
        ("mip levels", output.mip_levels, expected_mips),
        ("depth", output.depth, source.depth),
        ("array size", output.array_size, source.array_size),
        ("dimension", output.dimension, source.dimension),
        ("alpha mode", output.alpha_mode, source.alpha_mode),
    ]
    for label, actual, expected in comparisons:
        if actual != expected:
            problems.append(f"{label}: expected {expected!r}, got {actual!r}")
    if output.is_srgb != expected_srgb:
        problems.append(
            f"color space: expected {'sRGB' if expected_srgb else 'linear'}, "
            f"got {'sRGB' if output.is_srgb else 'linear'}"
        )
    return problems


def unique_staging_directory(parent: Path, size_label: str, format_label: str, timestamp: str) -> Path:
    base = parent / f"_DDS_Resized_{size_label}_{format_label}_{timestamp}"
    candidate = base
    index = 2
    while candidate.exists():
        candidate = parent / f"{base.name}_{index}"
        index += 1
    candidate.mkdir()
    return candidate


def find_generated_dds(staging_directory: Path, source: Path) -> Path | None:
    matches = [
        item
        for item in staging_directory.iterdir()
        if item.is_file() and item.suffix.lower() == ".dds" and item.stem.casefold() == source.stem.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def quarantine_failed_output(output: Path) -> None:
    if not output.exists():
        return
    failed = output.with_name(output.name + ".failed")
    index = 2
    while failed.exists():
        failed = output.with_name(output.name + f".failed-{index}")
        index += 1
    os.replace(output, failed)


def restore_alpha_mode_metadata(output: Path, alpha_mode: str) -> None:
    """Restore the DDS DX10 alpha-mode bits without altering texture pixels."""
    if alpha_mode not in ALPHA_MODE_VALUES:
        raise RuntimeError(f"unsupported DDS alpha mode reported by texdiag: {alpha_mode}")

    with output.open("r+b") as stream:
        header = stream.read(148)
        if len(header) < 148 or header[:4] != b"DDS " or header[84:88] != b"DX10":
            raise RuntimeError("generated file does not contain the expected DDS DX10 header")
        misc_flags_2 = struct.unpack_from("<I", header, 144)[0]
        preserved = (misc_flags_2 & ~0x7) | ALPHA_MODE_VALUES[alpha_mode]
        stream.seek(144)
        stream.write(struct.pack("<I", preserved))


def create_linear_interpretation_copy(source: Path, staging_directory: Path, source_format: str) -> tuple[Path, Path]:
    """Copy an sRGB DDS and change only its DXGI format tag to the linear equivalent."""
    mapping = SRGB_TO_LINEAR_DXGI.get(source_format)
    if mapping is None:
        raise RuntimeError(f"cannot reinterpret unsupported sRGB format as linear: {source_format}")

    temporary_directory = staging_directory / f".linear-input-{uuid.uuid4().hex}"
    temporary_directory.mkdir()
    try:
        temporary_source = temporary_directory / source.name
        shutil.copy2(source, temporary_source)

        expected_srgb_value, linear_value = mapping
        with temporary_source.open("r+b") as stream:
            header = stream.read(132)
            if len(header) < 132 or header[:4] != b"DDS " or header[84:88] != b"DX10":
                raise RuntimeError("sRGB reinterpretation requires a DDS DX10 header")
            current_value = struct.unpack_from("<I", header, 128)[0]
            if current_value != expected_srgb_value:
                raise RuntimeError(
                    f"unexpected DXGI format value {current_value}; expected {expected_srgb_value} for {source_format}"
                )
            stream.seek(128)
            stream.write(struct.pack("<I", linear_value))
        return temporary_source, temporary_directory
    except Exception:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise


def install_staged_job(job: TextureJob, texdiag: Path) -> None:
    temporary = job.source.parent / f".{job.source.name}.{uuid.uuid4().hex}.installing.dds"
    try:
        shutil.copy2(job.staged, temporary)
        copied_metadata = read_metadata(texdiag, temporary)
        if copied_metadata != job.output_metadata:
            raise RuntimeError("metadata changed while copying the staged output")
        os.replace(temporary, job.source)
    finally:
        if temporary.exists():
            temporary.unlink()


def remove_staging_directories(staging_by_parent: dict[Path, Path]) -> None:
    for source_parent, staging in staging_by_parent.items():
        if not staging.exists():
            continue
        resolved_parent = source_parent.resolve()
        resolved_staging = staging.resolve()
        if resolved_staging.parent != resolved_parent or not resolved_staging.name.startswith("_DDS_Resized_"):
            raise RuntimeError(f"refusing to delete unexpected staging path: {resolved_staging}")
        shutil.rmtree(resolved_staging)


def main() -> int:
    print("=" * 62)
    print("DDS Batch Downsizer - Microsoft DirectXTex")
    print("=" * 62)
    print("Quick use:")
    print("  1. Drag DDS files or a folder here; folders are not recursive.")
    print("  2. Review source info, choose conversion options, then enter 1 to stage outputs.")
    print("  3. Choose the final 0/1/2 action after validated outputs are staged.")
    print("  See README.md beside this script for option details.\n")

    try:
        texconv = find_directxtex_tool("texconv")
        texdiag = find_directxtex_tool("texdiag")
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        return 2

    raw_inputs = sys.argv[1:]
    while True:
        if not raw_inputs:
            raw = input("Drag DDS file(s) or a folder here, then press Enter:\n> ")
            try:
                raw_inputs = split_windows_input(raw)
            except (OSError, ValueError) as exc:
                print(f"[ERROR] Could not parse input paths: {exc}")
                raw_inputs = []
                continue

        files, discovery_warnings = collect_dds_files(raw_inputs)
        for warning in discovery_warnings:
            print(f"[WARNING] {warning}")
        if files:
            break
        print("[ERROR] No DDS files were selected. Please try again.\n")
        raw_inputs = []

    print(f"\nFound {len(files)} DDS file(s).")

    inspected: list[tuple[Path, TextureMetadata]] = []
    inspection_failures: list[tuple[Path, str]] = []
    print("\nSource texture information:")
    for index, source in enumerate(files, start=1):
        try:
            metadata = read_metadata(texdiag, source)
            if "TYPELESS" in metadata.format:
                raise RuntimeError(f"typeless format {metadata.format}; color space cannot be preserved safely")
            inspected.append((source, metadata))
            color_space = "sRGB" if metadata.is_srgb else "linear"
            mip_status = f"yes ({metadata.mip_levels})" if metadata.mip_levels > 1 else "no (1)"
            print(
                f"[{index}/{len(files)}] {source.name}\n"
                f"    size={metadata.width}x{metadata.height}, mip-chain={mip_status}, "
                f"format={metadata.format}, color-space={color_space}, alpha={metadata.alpha_mode}, "
                f"type={metadata.dimension}, array={metadata.array_size}, depth={metadata.depth}"
            )
        except RuntimeError as exc:
            inspection_failures.append((source, str(exc)))
            print(f"[{index}/{len(files)}] [FAILED] {source.name}: {exc}")

    if not inspected:
        print("[ERROR] None of the selected textures could be inspected safely.")
        return 1

    target_size = prompt_resolution()
    non_square_policy = prompt_non_square_policy(target_size)
    small_policy = prompt_small_texture_policy()
    compression = prompt_compression()
    srgb_mode = prompt_srgb_mode()
    mip_mode = prompt_mipmap_mode()

    selected: list[PlannedTexture] = []
    ignored: list[tuple[Path, str]] = list(inspection_failures)
    for source, metadata in inspected:
        is_non_square = metadata.width != metadata.height
        if is_non_square and non_square_policy == 0:
            ignored.append((source, "non-square source and non-square policy is 0"))
            continue

        is_small_or_equal = target_size != 0 and metadata.width <= target_size and metadata.height <= target_size
        if not is_non_square and is_small_or_equal and small_policy == 0:
            ignored.append((source, "at or below the target and smaller/equal policy is 0"))
            continue

        try:
            target_format = output_format(compression, metadata, srgb_mode)
        except RuntimeError as exc:
            ignored.append((source, str(exc)))
            continue

        output_width, output_height = planned_dimensions(
            metadata, target_size, small_policy, non_square_policy
        )
        selected.append(
            PlannedTexture(source, metadata, output_width, output_height, target_format)
        )

    print("\nConversion settings:")
    target_description = "preserve each original size" if target_size == 0 else f"{target_size}x{target_size}"
    print(f"  Target:       {target_description}")
    print(f"  Non-square:   {non_square_policy}")
    print(f"  Small policy: {small_policy}")
    print(f"  Compression:  {compression_label(compression)}")
    print(f"  sRGB mode:    {srgb_mode}")
    print("  Filter:       FANT (TRIANGLE fallback if DirectXTex rejects FANT)")
    print(f"  Mipmap mode:  {mip_mode}")
    if target_size == 0:
        print("  Note:         Small-image policy is inactive because target size is 0")

    if ignored:
        reason_counts: dict[str, int] = {}
        for _, reason in ignored:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        print("\nIgnored breakdown:")
        for reason, count in reason_counts.items():
            print(f"  {count}: {reason}")

    if not prompt_conversion_confirmation(len(selected), len(ignored)):
        print("\nConversion aborted. No staging folder was created and no originals were changed.")
        return 0

    if not selected:
        print("\nNothing needs to be processed.")
        return 0

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    size_label = "OriginalSize" if target_size == 0 else str(target_size)
    format_label = compression_label(compression)
    staging_by_parent: dict[Path, Path] = {}
    log = RunLog()
    log.write(f"ResizeDDS run: {datetime.now().isoformat(timespec='seconds')}")
    log.write(
        f"Target: {target_description}; small policy: {small_policy}; "
        f"non-square policy: {non_square_policy}; compression: {format_label}; "
        f"sRGB mode: {srgb_mode}; mip mode: {mip_mode}"
    )
    for source, reason in ignored:
        log.write(f"[IGNORED] {source}: {reason}")
    log.write("")

    jobs: list[TextureJob] = []
    conversion_failures: list[tuple[Path, str]] = []
    for index, planned in enumerate(selected, start=1):
        source = planned.source
        source_metadata = planned.source_metadata
        output_width = planned.output_width
        output_height = planned.output_height
        target_format = planned.target_format
        staging = staging_by_parent.get(source.parent)
        if staging is None:
            try:
                staging = unique_staging_directory(source.parent, size_label, format_label, timestamp)
            except OSError as exc:
                reason = f"could not create staging directory: {exc}"
                conversion_failures.append((source, reason))
                log.write(f"[FAILED] {source}: {reason}")
                continue
            staging_by_parent[source.parent] = staging
            log.write(f"Staging directory: {staging}")

        mips = mip_argument(mip_mode, source_metadata)
        log.write("")
        log.write(
            f"[{index}/{len(selected)}] {source.name}: "
            f"{source_metadata.width}x{source_metadata.height} {source_metadata.format} "
            f"-> {output_width}x{output_height} {target_format}"
        )
        conversion_source = source
        reinterpret_directory: Path | None = None
        if srgb_mode == 1 and source_metadata.is_srgb:
            try:
                conversion_source, reinterpret_directory = create_linear_interpretation_copy(
                    source, staging, source_metadata.format
                )
                log.write(f"  Reinterpreting {source_metadata.format} as linear without changing color values.")
            except (OSError, RuntimeError) as exc:
                reason = f"could not prepare linear reinterpretation: {exc}"
                conversion_failures.append((source, reason))
                log.write(f"  [FAILED] {reason}")
                continue

        result: subprocess.CompletedProcess[str] | None = None
        generated: Path | None = None
        used_filter = ""
        for filter_name in ("FANT", "TRIANGLE"):
            command: list[str | Path] = [
                texconv,
                "-nologo",
                "-y",
                "-w",
                str(output_width),
                "-h",
                str(output_height),
                "-m",
                str(mips),
                "-if",
                filter_name,
                "-dx10",
                "-f",
                target_format,
                "-o",
                staging,
            ]
            if srgb_mode == 2 and source_metadata.is_srgb:
                command.append("-srgbi")
            if supports_uniform_bc_weighting(target_format):
                # DirectXTex supports uniform instead of perceptual RGB error
                # weighting for BC1-BC3 (including their legacy aliases).
                command.extend(["-bc", "u"])
            command.extend(["--", conversion_source])
            result = run_command(command)
            if result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    log.write(f"  {line}")
            if result.stderr.strip():
                for line in result.stderr.strip().splitlines():
                    log.write(f"  {line}")

            generated = find_generated_dds(staging, source)
            if result.returncode == 0 and generated is not None:
                used_filter = filter_name
                break
            if generated:
                quarantine_failed_output(generated)
                generated = None
            if filter_name == "FANT":
                log.write("  [RETRY] FANT was rejected; retrying with the finite low-pass TRIANGLE filter.")

        if reinterpret_directory is not None:
            shutil.rmtree(reinterpret_directory, ignore_errors=True)

        assert result is not None
        if result.returncode != 0 or generated is None:
            reason = f"texconv failed with exit code {result.returncode}"
            if generated is None:
                reason += "; expected output file was not found"
            conversion_failures.append((source, reason))
            log.write(f"  [FAILED] {reason}")
            if generated:
                quarantine_failed_output(generated)
            continue

        try:
            restore_alpha_mode_metadata(generated, source_metadata.alpha_mode)
            output_metadata = read_metadata(texdiag, generated)
            problems = validate_output(
                source_metadata,
                output_metadata,
                output_width,
                output_height,
                target_format,
                mip_mode,
                source_metadata.is_srgb and srgb_mode == 0,
            )
            if problems:
                raise RuntimeError("; ".join(problems))
        except RuntimeError as exc:
            conversion_failures.append((source, str(exc)))
            log.write(f"  [FAILED VALIDATION] {exc}")
            quarantine_failed_output(generated)
            continue

        jobs.append(TextureJob(source, source_metadata, generated, output_metadata))
        log.write(
            f"  [OK] {output_metadata.width}x{output_metadata.height}, "
            f"{output_metadata.format}, {output_metadata.mip_levels} mip(s), "
            f"alpha={output_metadata.alpha_mode}, filter={used_filter}"
        )

    log.write("")
    log.write(
        f"Staging finished: {len(jobs)} successful, {len(ignored)} ignored, "
        f"{len(conversion_failures)} failed."
    )
    log.save_to(staging_by_parent.values())

    print("\n" + "=" * 62)
    print(
        f"STAGING COMPLETE: {len(jobs)} successful, {len(ignored)} ignored, "
        f"{len(conversion_failures)} failed"
    )
    for staging in staging_by_parent.values():
        print(f"Staged output: {staging}")
    if conversion_failures:
        print("\nFailures:")
        for source, reason in conversion_failures:
            print(f"  {source.name}: {reason}")

    print("\nOriginal files have NOT been changed.")
    if not jobs:
        print("No validated outputs are available to install.")

    action = prompt_staging_action(bool(jobs))
    if action == 0:
        log.write("Staging kept; originals were not changed.")
        log.save_to(staging_by_parent.values())
        print("Staged files were kept. Originals remain unchanged.")
        return 0 if not conversion_failures else 1

    if action == 2:
        print("Deleting this run's staging folder(s)...")
        remove_staging_directories(staging_by_parent)
        print("Staging deleted. Restarting from the beginning.\n")
        return RESTART_REQUESTED

    installed = 0
    install_failures: list[tuple[Path, str]] = []
    print("\nInstalling validated outputs...")
    for job in jobs:
        try:
            install_staged_job(job, texdiag)
            installed += 1
            log.write(f"[INSTALLED] {job.source}")
            print(f"[OK] {job.source.name}")
        except (OSError, RuntimeError) as exc:
            install_failures.append((job.source, str(exc)))
            log.write(f"[INSTALL FAILED] {job.source}: {exc}")
            print(f"[FAILED] {job.source.name}: {exc}")

    log.write(f"Install finished: {installed} installed, {len(install_failures)} failed.")
    log.save_to(staging_by_parent.values())
    print(f"\nINSTALL COMPLETE: {installed} installed, {len(install_failures)} failed")
    if install_failures:
        print("Some replacements failed, so the staging folder was kept for recovery.")
    else:
        remove_staging_directories(staging_by_parent)
        print("All replacements succeeded; the staging folder was deleted.")
    return 0 if not conversion_failures and not install_failures else 1


def run_and_pause() -> int:
    while True:
        try:
            exit_code = main()
        except KeyboardInterrupt:
            print("\nCancelled. Any originals not explicitly installed remain unchanged.")
            exit_code = 130
        except Exception as exc:  # Keep unexpected failures visible when launched from Explorer.
            print(f"\n[UNEXPECTED ERROR] {exc}")
            exit_code = 1

        if exit_code == RESTART_REQUESTED:
            continue
        break

    if sys.stdin.isatty():
        try:
            print("\nClosing automatically in 10 seconds...")
            time.sleep(10)
        except KeyboardInterrupt:
            pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_and_pause())
