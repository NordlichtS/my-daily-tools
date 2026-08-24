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
from enum import IntEnum, IntFlag
from pathlib import Path
from typing import Iterable


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


MIN_SIZE = 32
MAX_SIZE = 8192
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
ALPHA_MODE_NAMES = {value: name for name, value in ALPHA_MODE_VALUES.items()}
SRGB_TO_LINEAR_DXGI = {
    "R8G8B8A8_UNORM_SRGB": (29, 28),
    "BC1_UNORM_SRGB": (72, 71),
    "BC2_UNORM_SRGB": (75, 74),
    "BC3_UNORM_SRGB": (78, 77),
    "B8G8R8A8_UNORM_SRGB": (91, 87),
    "B8G8R8X8_UNORM_SRGB": (93, 88),
    "BC7_UNORM_SRGB": (99, 98),
}
LINEAR_TO_SRGB_FORMAT = {
    srgb_format.removesuffix("_SRGB"): srgb_format for srgb_format in SRGB_TO_LINEAR_DXGI
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


UNSET = -1


class FileStatus(IntEnum):
    ACTIVE = 0
    POLICY_SKIPPED = 1
    INVALID_COMBINATION = 2
    CONVERSION_FAILED = 3
    STAGED = 4
    KEPT_STAGING = 5
    INSTALLED = 6
    INSTALL_FAILED = 7
    INSPECTION_FAILED = 8


class Slot(IntEnum):
    STATUS = 0
    IS_NORMAL = 1
    SOURCE_WIDTH = 2
    SOURCE_HEIGHT = 3
    SOURCE_DEPTH = 4
    SOURCE_MIPS = 5
    SOURCE_ARRAY_SIZE = 6
    SOURCE_FORMAT = 7
    SOURCE_SRGB = 8
    SOURCE_ALPHA_MODE = 9
    SOURCE_DIMENSION = 10
    REQUEST_TARGET_SIZE = 11
    REQUEST_NON_SQUARE = 12
    REQUEST_SMALL = 13
    REQUEST_COMPRESSION = 14
    REQUEST_NORMAL = 15
    REQUEST_BC1_ALPHA = 16
    REQUEST_SRGB = 17
    REQUEST_MIPS = 18
    OUTPUT_WIDTH = 19
    OUTPUT_HEIGHT = 20
    OUTPUT_FORMAT = 21
    OUTPUT_SRGB = 22
    COLOR_ACTION = 23
    SWIZZLE = 24
    BC_FLAGS = 25
    ALPHA_THRESHOLD = 26
    TEXCONV_MIPS = 27
    EXPECTED_MIPS = 28
    EXPECTED_ALPHA_MODE = 29
    COUNT = 30


class CompressionChoice(IntEnum):
    PRESERVE = 0
    BC1 = 1
    BC2 = 2
    BC3 = 3
    BC4 = 4
    BC5 = 5
    BC6H = 6
    BC7 = 7
    RGBA8888 = 8


class NormalTreatment(IntEnum):
    NONE = 0
    FORCE_BLUE = 1
    FORCE_BC5 = 2


class BC1AlphaTreatment(IntEnum):
    NOT_APPLICABLE = -1
    OPAQUE = 0
    THRESHOLD_025 = 1
    THRESHOLD_050 = 2
    THRESHOLD_075 = 3
    DITHER = 4


class SRGBMode(IntEnum):
    PRESERVE = 0
    ASSUME_LINEAR = 1
    CONVERT_TO_LINEAR = 2
    FORCE_SRGB_TAG = 3


class ColorAction(IntEnum):
    NONE = 0
    REINTERPRET_LINEAR = 1
    CONVERT_TO_LINEAR = 2
    ASSUME_SRGB = 3


class SwizzleMode(IntEnum):
    NONE = 0
    RGB1 = 1
    RG1A = 2
    RG11 = 3


class BCFlag(IntFlag):
    NONE = 0
    UNIFORM = 1
    DITHER = 2


class AlphaThreshold(IntEnum):
    NONE = 0
    VALUE_025 = 25
    VALUE_050 = 50
    VALUE_075 = 75


@dataclass(frozen=True)
class UserOptions:
    target_size: int
    non_square_policy: int
    small_policy: int
    compression: CompressionChoice
    normal_treatment: NormalTreatment
    bc1_alpha: BC1AlphaTreatment
    srgb_mode: SRGBMode
    mip_mode: int


@dataclass
class FileState:
    path: Path
    metadata: TextureMetadata | None
    slots: list[int]
    notes: list[str]
    reason: str = ""
    staged: Path | None = None
    output_metadata: TextureMetadata | None = None

    @property
    def status(self) -> FileStatus:
        return FileStatus(self.slots[Slot.STATUS])

    @status.setter
    def status(self, value: FileStatus) -> None:
        self.slots[Slot.STATUS] = int(value)


FORMAT_TO_CODE: dict[str, int] = {}
CODE_TO_FORMAT: list[str] = []
DIMENSION_TO_CODE: dict[str, int] = {}
CODE_TO_DIMENSION: list[str] = []

REQUEST_SLOTS = tuple(Slot(index) for index in range(Slot.REQUEST_TARGET_SIZE, Slot.OUTPUT_WIDTH))
RESOLVED_SLOTS = tuple(Slot(index) for index in range(Slot.OUTPUT_WIDTH, Slot.COUNT))


def intern_value(value: str, lookup: dict[str, int], values: list[str]) -> int:
    normalized = value.upper()
    code = lookup.get(normalized)
    if code is None:
        code = len(values)
        lookup[normalized] = code
        values.append(normalized)
    return code


def format_code(value: str) -> int:
    return intern_value(value, FORMAT_TO_CODE, CODE_TO_FORMAT)


def format_name(code: int) -> str:
    if code < 0 or code >= len(CODE_TO_FORMAT):
        raise RuntimeError(f"invalid format code {code}")
    return CODE_TO_FORMAT[code]


def dimension_code(value: str) -> int:
    return intern_value(value, DIMENSION_TO_CODE, CODE_TO_DIMENSION)


def create_file_state(path: Path, metadata: TextureMetadata | None, error: str = "") -> FileState:
    slots = [UNSET] * int(Slot.COUNT)
    slots[Slot.STATUS] = int(FileStatus.ACTIVE if metadata else FileStatus.INSPECTION_FAILED)
    state = FileState(path=path, metadata=metadata, slots=slots, notes=[], reason=error)
    if metadata is None:
        return state
    slots[Slot.IS_NORMAL] = int(is_normal_texture(path))
    slots[Slot.SOURCE_WIDTH] = metadata.width
    slots[Slot.SOURCE_HEIGHT] = metadata.height
    slots[Slot.SOURCE_DEPTH] = metadata.depth
    slots[Slot.SOURCE_MIPS] = metadata.mip_levels
    slots[Slot.SOURCE_ARRAY_SIZE] = metadata.array_size
    slots[Slot.SOURCE_FORMAT] = format_code(metadata.format)
    slots[Slot.SOURCE_SRGB] = int(metadata.is_srgb)
    slots[Slot.SOURCE_ALPHA_MODE] = ALPHA_MODE_VALUES[metadata.alpha_mode]
    slots[Slot.SOURCE_DIMENSION] = dimension_code(metadata.dimension)
    return state


def clear_round_slots(state: FileState) -> None:
    for slot in REQUEST_SLOTS + RESOLVED_SLOTS:
        state.slots[slot] = UNSET
    state.notes.clear()
    state.reason = ""
    state.staged = None
    state.output_metadata = None


def reset_for_retry(state: FileState) -> None:
    clear_round_slots(state)
    state.status = FileStatus.ACTIVE


class RunLog:
    def __init__(self, console: bool = True) -> None:
        self.lines: list[str] = []
        self.console = console

    def write(self, message: str = "") -> None:
        if self.console:
            print(message, flush=True)
        self.lines.append(message)

    def save_to(self, staging_directories: Iterable[Path]) -> None:
        text = "\n".join(self.lines) + "\n"
        for directory in staging_directories:
            try:
                (directory / "ResizeDDS.log").write_text(text, encoding="utf-8")
            except OSError as exc:
                print(f"[WARNING] Could not write log in {directory}: {exc}")


class BatchProgress:
    def __init__(self, total: int) -> None:
        self.total = total
        self.converted = 0
        self.failed = 0
        self._width = 0

    @staticmethod
    def _fit_to_console(prefix: str, current: Path) -> str:
        # Leave the final console column unused: writing into it can trigger an
        # automatic wrap before the following carriage return is processed.
        console_width = max(1, shutil.get_terminal_size(fallback=(120, 24)).columns - 1)
        path_text = str(current)
        available = console_width - len(prefix)
        if available <= 0:
            return prefix[:console_width]
        if len(path_text) > available:
            if available > 3:
                path_text = "..." + path_text[-(available - 3) :]
            else:
                path_text = path_text[-available:]
        return prefix + path_text

    def update(self, current: Path) -> None:
        prefix = (
            f"total {self.total} , converted {self.converted}, failed {self.failed} , current >> "
        )
        text = self._fit_to_console(prefix, current)
        previous_width = self._width
        self._width = len(text)
        sys.stdout.write("\r" + text + (" " * max(0, previous_width - len(text))) + "\r")
        sys.stdout.flush()

    def success(self, current: Path) -> None:
        self.converted += 1
        self.update(current)

    def failure(self, current: Path) -> None:
        self.failed += 1
        self.update(current)

    def finish(self) -> None:
        if self.total:
            sys.stdout.write("\n")
            sys.stdout.flush()


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


def is_dds_cubemap(texture: Path) -> bool:
    try:
        with texture.open("rb") as stream:
            header = stream.read(116)
    except OSError as exc:
        raise RuntimeError(f"could not read DDS header: {exc}") from exc
    if len(header) < 116 or header[:4] != b"DDS ":
        raise RuntimeError("file does not contain a valid DDS header")
    caps2 = struct.unpack_from("<I", header, 112)[0]
    return bool(caps2 & 0x200)


def unsupported_texture_shape(texture: Path, metadata: TextureMetadata) -> str | None:
    if is_dds_cubemap(texture):
        return "cubemaps are not supported; only ordinary 2D textures are accepted"
    if metadata.array_size != 1:
        return f"texture arrays are not supported (array size {metadata.array_size})"
    if metadata.depth != 1:
        return f"volume textures are not supported (depth {metadata.depth})"
    if metadata.dimension.upper() not in {"2D", "TEXTURE2D"}:
        return f"{metadata.dimension} textures are not supported; only 2D textures are accepted"
    return None


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


def prompt_bc1_transparency() -> int:
    print("\nBC1 transparency treatment:")
    print("  0 = Force every pixel opaque; do not retain transparency")
    print("  1 = Binary transparency with alpha threshold 0.25")
    print("  2 = Binary transparency with alpha threshold 0.50")
    print("  3 = Binary transparency with alpha threshold 0.75")
    print("  4 = Dither the full 0.0-1.0 alpha range into transparent/opaque coverage")
    while True:
        raw = input("Choose 0 through 4: ").strip()
        if raw in {"0", "1", "2", "3", "4"}:
            return int(raw)
        print("Enter a number from 0 through 4.")


def prompt_normal_texture_treatment() -> int:
    print("\nSpecial treatment for files with 'normal' in the filename:")
    print("  0 = No special treatment")
    print("  1 = Force the blue channel to 1.0")
    print("  2 = Change the output format to BC5_UNORM")
    while True:
        raw = input("Choose 0, 1, or 2: ").strip()
        if raw in {"0", "1", "2"}:
            return int(raw)
        print("Enter 0, 1, or 2.")


def normal_texture_treatment_label(mode: int) -> str:
    return {
        0: "none",
        1: "force blue to 1.0",
        2: "override output format with BC5_UNORM",
    }[mode]


def is_normal_texture(path: Path) -> bool:
    return "normal" in path.name.casefold()


def bc1_transparency_label(mode: int | None) -> str:
    return {
        None: "not applicable",
        0: "force opaque",
        1: "threshold 0.25",
        2: "threshold 0.50",
        3: "threshold 0.75",
        4: "full-range alpha dithering",
    }[mode]


def prompt_srgb_mode() -> int:
    print("\nsRGB handling:")
    print("  0 = Preserve each source's sRGB or linear classification")
    print("  1 = Treat every source as linear without converting its stored color values")
    print("  2 = Convert sRGB color values to linear; write linear output for every source")
    print("  3 = Assume input values are sRGB and write sRGB output (not applicable to BC4/5/6)")
    while True:
        raw = input("Choose 0, 1, 2, or 3: ").strip()
        if raw in {"0", "1", "2", "3"}:
            return int(raw)
        print("Enter 0, 1, 2, or 3.")


def prompt_mipmap_mode() -> int:
    print("\nMipmap mode:")
    print("  0 = Keep single-mip sources single; regenerate a full chain for sources with mipmaps")
    print("  1 = Keep only the biggest (top) mip level")
    print("  2 = Always generate a full mip chain")
    while True:
        raw = input("Choose 0, 1, or 2: ").strip()
        if raw in {"0", "1", "2"}:
            return int(raw)
        print("Enter 0, 1, or 2.")


def prompt_user_options() -> UserOptions:
    target_size = prompt_resolution()
    non_square_policy = prompt_non_square_policy(target_size)
    small_policy = prompt_small_texture_policy()
    compression = CompressionChoice(prompt_compression())
    normal_treatment = NormalTreatment(prompt_normal_texture_treatment())
    bc1_alpha = (
        BC1AlphaTreatment(prompt_bc1_transparency())
        if compression == CompressionChoice.BC1
        else BC1AlphaTreatment.NOT_APPLICABLE
    )
    srgb_mode = SRGBMode(prompt_srgb_mode())
    mip_mode = prompt_mipmap_mode()
    return UserOptions(
        target_size=target_size,
        non_square_policy=non_square_policy,
        small_policy=small_policy,
        compression=compression,
        normal_treatment=normal_treatment,
        bc1_alpha=bc1_alpha,
        srgb_mode=srgb_mode,
        mip_mode=mip_mode,
    )


def prompt_retry_leftovers() -> bool:
    while True:
        raw = input("Enter 1 to retry these files with new options, or 0 to finish: ").strip()
        if raw == "1":
            return True
        if raw == "0":
            return False
        print("Enter 1 to retry or 0 to finish.")


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


def is_bc1_format(target_format: str) -> bool:
    normalized = target_format.upper()
    return normalized.startswith("BC1_") or normalized == "DXT1"


def output_format(compression: int, source_metadata: TextureMetadata, srgb_mode: int) -> str:
    output_is_srgb = (source_metadata.is_srgb and srgb_mode == SRGBMode.PRESERVE) or (
        srgb_mode == SRGBMode.FORCE_SRGB_TAG
    )
    if compression == 0:
        linear_format = source_metadata.format.removesuffix("_SRGB")
        if linear_format.startswith(("BC4_", "BC5_", "BC6")):
            return linear_format
        if output_is_srgb:
            try:
                return LINEAR_TO_SRGB_FORMAT[linear_format]
            except KeyError as exc:
                raise RuntimeError(f"{linear_format} has no sRGB DXGI variant") from exc
        return linear_format
    if compression == 8:
        return "R8G8B8A8_UNORM_SRGB" if output_is_srgb else "R8G8B8A8_UNORM"
    if compression in {1, 2, 3, 7}:
        suffix = "_UNORM_SRGB" if output_is_srgb else "_UNORM"
        return f"BC{compression}{suffix}"
    if compression == 4:
        return "BC4_UNORM"
    if compression == 5:
        return "BC5_UNORM"
    if compression == 6:
        return "BC6H_UF16"
    raise RuntimeError(f"unsupported compression selection: {compression}")


def mip_argument(mode: int, source_metadata: TextureMetadata) -> int:
    if mode == 1:
        return 1
    if mode == 2:
        return 0
    return 1 if source_metadata.mip_levels == 1 else 0


def expected_mip_levels(
    mode: int, source_metadata: TextureMetadata, output_width: int, output_height: int
) -> int:
    if mode == 1 or (mode == 0 and source_metadata.mip_levels == 1):
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


def format_stores_blue(target_format: str) -> bool:
    normalized = target_format.upper()
    if normalized.startswith(("BC4_", "BC5_")):
        return False
    return not normalized.startswith(("R8_", "R8G8_", "R16_", "R16G16_", "R32_", "R32G32_", "A8_"))


def write_user_requests(state: FileState, options: UserOptions) -> None:
    state.slots[Slot.REQUEST_TARGET_SIZE] = options.target_size
    state.slots[Slot.REQUEST_NON_SQUARE] = options.non_square_policy
    state.slots[Slot.REQUEST_SMALL] = options.small_policy
    state.slots[Slot.REQUEST_COMPRESSION] = int(options.compression)
    state.slots[Slot.REQUEST_NORMAL] = int(options.normal_treatment)
    state.slots[Slot.REQUEST_BC1_ALPHA] = int(options.bc1_alpha)
    state.slots[Slot.REQUEST_SRGB] = int(options.srgb_mode)
    state.slots[Slot.REQUEST_MIPS] = options.mip_mode


def resolve_file_state(state: FileState, options: UserOptions) -> None:
    if state.metadata is None:
        return
    clear_round_slots(state)
    state.status = FileStatus.ACTIVE
    write_user_requests(state, options)
    metadata = state.metadata

    is_non_square = metadata.width != metadata.height
    if is_non_square and options.non_square_policy == 0:
        state.status = FileStatus.POLICY_SKIPPED
        state.reason = "non-square source and non-square policy is 0"
        return
    is_small_or_equal = (
        options.target_size != 0
        and metadata.width <= options.target_size
        and metadata.height <= options.target_size
    )
    if not is_non_square and is_small_or_equal and options.small_policy == 0:
        state.status = FileStatus.POLICY_SKIPPED
        state.reason = "at or below the target and smaller/equal policy is 0"
        return

    output_width, output_height = planned_dimensions(
        metadata, options.target_size, options.small_policy, options.non_square_policy
    )
    state.slots[Slot.OUTPUT_WIDTH] = output_width
    state.slots[Slot.OUTPUT_HEIGHT] = output_height

    normal_match = bool(state.slots[Slot.IS_NORMAL])
    normal_bc5_override = normal_match and options.normal_treatment == NormalTreatment.FORCE_BC5
    effective_compression = CompressionChoice.BC5 if normal_bc5_override else options.compression

    color_action = ColorAction.NONE
    format_srgb_mode = options.srgb_mode
    if metadata.is_srgb:
        if normal_bc5_override and options.srgb_mode == SRGBMode.PRESERVE:
            color_action = ColorAction.REINTERPRET_LINEAR
            format_srgb_mode = SRGBMode.ASSUME_LINEAR
            state.notes.append("sRGB normal overridden to BC5: stored values are reinterpreted as linear")
        elif options.srgb_mode == SRGBMode.ASSUME_LINEAR:
            color_action = ColorAction.REINTERPRET_LINEAR
        elif options.srgb_mode == SRGBMode.CONVERT_TO_LINEAR:
            color_action = ColorAction.CONVERT_TO_LINEAR

    try:
        target_format = output_format(int(effective_compression), metadata, int(format_srgb_mode))
    except RuntimeError as exc:
        state.status = FileStatus.INVALID_COMBINATION
        state.reason = str(exc)
        return

    if options.srgb_mode == SRGBMode.FORCE_SRGB_TAG:
        if target_format.startswith(("BC4_", "BC5_", "BC6")):
            state.notes.append(f"{target_format} has no sRGB tag; mode 3 does not apply")
        else:
            color_action = ColorAction.ASSUME_SRGB

    state.slots[Slot.OUTPUT_FORMAT] = format_code(target_format)
    state.slots[Slot.OUTPUT_SRGB] = int(target_format.endswith("_SRGB"))
    state.slots[Slot.COLOR_ACTION] = int(color_action)

    force_blue = normal_match and options.normal_treatment == NormalTreatment.FORCE_BLUE
    if force_blue and not format_stores_blue(target_format):
        force_blue = False
        state.notes.append(f"{target_format} has no stored blue channel; blue override was omitted")

    explicit_bc1 = options.compression == CompressionChoice.BC1 and is_bc1_format(target_format)
    force_opaque = explicit_bc1 and options.bc1_alpha == BC1AlphaTreatment.OPAQUE
    if force_blue and force_opaque:
        swizzle = SwizzleMode.RG11
    elif force_blue:
        swizzle = SwizzleMode.RG1A
    elif force_opaque:
        swizzle = SwizzleMode.RGB1
    else:
        swizzle = SwizzleMode.NONE
    state.slots[Slot.SWIZZLE] = int(swizzle)

    bc_flags = BCFlag.UNIFORM if supports_uniform_bc_weighting(target_format) else BCFlag.NONE
    if explicit_bc1 and options.bc1_alpha == BC1AlphaTreatment.DITHER:
        bc_flags |= BCFlag.DITHER
    state.slots[Slot.BC_FLAGS] = int(bc_flags)

    threshold = AlphaThreshold.NONE
    if explicit_bc1:
        threshold = {
            BC1AlphaTreatment.THRESHOLD_025: AlphaThreshold.VALUE_025,
            BC1AlphaTreatment.THRESHOLD_050: AlphaThreshold.VALUE_050,
            BC1AlphaTreatment.THRESHOLD_075: AlphaThreshold.VALUE_075,
        }.get(options.bc1_alpha, AlphaThreshold.NONE)
    state.slots[Slot.ALPHA_THRESHOLD] = int(threshold)

    texconv_mips = mip_argument(options.mip_mode, metadata)
    expected_mips = expected_mip_levels(options.mip_mode, metadata, output_width, output_height)
    state.slots[Slot.TEXCONV_MIPS] = texconv_mips
    state.slots[Slot.EXPECTED_MIPS] = expected_mips
    state.slots[Slot.EXPECTED_ALPHA_MODE] = (
        ALPHA_MODE_VALUES["Opaque"] if force_opaque else ALPHA_MODE_VALUES[metadata.alpha_mode]
    )

    problems = validate_resolved_state(state)
    if problems:
        state.status = FileStatus.INVALID_COMBINATION
        state.reason = "; ".join(problems)


def validate_resolved_state(state: FileState) -> list[str]:
    problems: list[str] = []
    required = RESOLVED_SLOTS
    missing = [slot.name for slot in required if state.slots[slot] == UNSET]
    if missing:
        return [f"unresolved command slots: {', '.join(missing)}"]

    metadata = state.metadata
    if metadata is None:
        return ["source metadata is unavailable"]
    try:
        target_format = format_name(state.slots[Slot.OUTPUT_FORMAT])
    except RuntimeError as exc:
        return [str(exc)]
    if state.slots[Slot.OUTPUT_WIDTH] <= 0 or state.slots[Slot.OUTPUT_HEIGHT] <= 0:
        problems.append("output dimensions must be positive")
    if bool(state.slots[Slot.OUTPUT_SRGB]) != target_format.endswith("_SRGB"):
        problems.append("output sRGB slot contradicts the target DXGI format")

    try:
        color_action = ColorAction(state.slots[Slot.COLOR_ACTION])
    except ValueError:
        problems.append(f"unknown color action {state.slots[Slot.COLOR_ACTION]}")
        color_action = ColorAction.NONE
    if color_action in {ColorAction.REINTERPRET_LINEAR, ColorAction.CONVERT_TO_LINEAR} and not metadata.is_srgb:
        problems.append("linearization was requested for an already-linear source")
    if color_action in {ColorAction.REINTERPRET_LINEAR, ColorAction.CONVERT_TO_LINEAR} and state.slots[Slot.OUTPUT_SRGB]:
        problems.append("linearization cannot produce an sRGB-labelled output")
    if color_action == ColorAction.REINTERPRET_LINEAR and metadata.format not in SRGB_TO_LINEAR_DXGI:
        problems.append(f"{metadata.format} cannot be reinterpreted by patching its DX10 format tag")
    if color_action == ColorAction.ASSUME_SRGB and not state.slots[Slot.OUTPUT_SRGB]:
        problems.append("sRGB input/output handling requires an sRGB-labelled output format")

    try:
        swizzle = SwizzleMode(state.slots[Slot.SWIZZLE])
    except ValueError:
        problems.append(f"unknown swizzle mode {state.slots[Slot.SWIZZLE]}")
        swizzle = SwizzleMode.NONE
    if swizzle in {SwizzleMode.RG1A, SwizzleMode.RG11} and not format_stores_blue(target_format):
        problems.append(f"{target_format} cannot store the requested blue-channel swizzle")

    flags = BCFlag(state.slots[Slot.BC_FLAGS])
    known_flags = BCFlag.UNIFORM | BCFlag.DITHER
    if int(flags) & ~int(known_flags):
        problems.append(f"unknown BC flag bits {int(flags) & ~int(known_flags)}")
    if flags & BCFlag.UNIFORM and not supports_uniform_bc_weighting(target_format):
        problems.append("uniform BC weighting is unsupported by the target format")
    explicit_bc1_dither = (
        state.slots[Slot.REQUEST_COMPRESSION] == int(CompressionChoice.BC1)
        and state.slots[Slot.REQUEST_BC1_ALPHA] == int(BC1AlphaTreatment.DITHER)
    )
    if flags & BCFlag.DITHER and (not is_bc1_format(target_format) or not explicit_bc1_dither):
        problems.append("full-range alpha dithering is only valid for explicit BC1 output")

    try:
        threshold = AlphaThreshold(state.slots[Slot.ALPHA_THRESHOLD])
    except ValueError:
        problems.append(f"unknown alpha threshold {state.slots[Slot.ALPHA_THRESHOLD]}")
        threshold = AlphaThreshold.NONE
    if threshold != AlphaThreshold.NONE and not is_bc1_format(target_format):
        problems.append("alpha threshold is only valid for BC1 output")
    if state.slots[Slot.TEXCONV_MIPS] < 0 or state.slots[Slot.EXPECTED_MIPS] < 1:
        problems.append("invalid mip settings")
    return problems


def build_texconv_command(
    texconv: Path,
    state: FileState,
    conversion_source: Path,
    staging: Path,
    filter_name: str,
) -> list[str | Path]:
    problems = validate_resolved_state(state)
    if problems:
        raise RuntimeError("; ".join(problems))
    command: list[str | Path] = [
        texconv,
        "-nologo",
        "-y",
        "-w",
        str(state.slots[Slot.OUTPUT_WIDTH]),
        "-h",
        str(state.slots[Slot.OUTPUT_HEIGHT]),
        "-m",
        str(state.slots[Slot.TEXCONV_MIPS]),
        "-if",
        filter_name,
        "-dx10",
        "-f",
        format_name(state.slots[Slot.OUTPUT_FORMAT]),
        "-o",
        staging,
    ]
    if ColorAction(state.slots[Slot.COLOR_ACTION]) == ColorAction.CONVERT_TO_LINEAR:
        command.append("-srgbi")
    elif ColorAction(state.slots[Slot.COLOR_ACTION]) == ColorAction.ASSUME_SRGB:
        command.append("-srgb")
    swizzle = SwizzleMode(state.slots[Slot.SWIZZLE])
    swizzle_value = {
        SwizzleMode.RGB1: "rgb1",
        SwizzleMode.RG1A: "rg1a",
        SwizzleMode.RG11: "rg11",
    }.get(swizzle)
    if swizzle_value:
        command.extend(["--swizzle", swizzle_value])
    threshold = AlphaThreshold(state.slots[Slot.ALPHA_THRESHOLD])
    threshold_value = {
        AlphaThreshold.VALUE_025: "0.25",
        AlphaThreshold.VALUE_050: "0.5",
        AlphaThreshold.VALUE_075: "0.75",
    }.get(threshold)
    if threshold_value:
        command.extend(["-at", threshold_value])
    flags = BCFlag(state.slots[Slot.BC_FLAGS])
    flag_text = ("u" if flags & BCFlag.UNIFORM else "") + ("d" if flags & BCFlag.DITHER else "")
    if flag_text:
        command.extend(["-bc", flag_text])
    command.extend(["--", conversion_source])
    return command


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


def validate_output_for_state(state: FileState, output: TextureMetadata) -> list[str]:
    metadata = state.metadata
    if metadata is None:
        return ["source metadata is unavailable"]
    expected_alpha = ALPHA_MODE_NAMES.get(state.slots[Slot.EXPECTED_ALPHA_MODE])
    if expected_alpha is None:
        return [f"invalid expected alpha-mode code {state.slots[Slot.EXPECTED_ALPHA_MODE]}"]
    comparisons = [
        ("width", output.width, state.slots[Slot.OUTPUT_WIDTH]),
        ("height", output.height, state.slots[Slot.OUTPUT_HEIGHT]),
        ("format", output.format, format_name(state.slots[Slot.OUTPUT_FORMAT])),
        ("mip levels", output.mip_levels, state.slots[Slot.EXPECTED_MIPS]),
        ("depth", output.depth, state.slots[Slot.SOURCE_DEPTH]),
        ("array size", output.array_size, state.slots[Slot.SOURCE_ARRAY_SIZE]),
        ("dimension", output.dimension.upper(), CODE_TO_DIMENSION[state.slots[Slot.SOURCE_DIMENSION]]),
        ("alpha mode", output.alpha_mode, expected_alpha),
    ]
    problems = [
        f"{label}: expected {expected!r}, got {actual!r}"
        for label, actual, expected in comparisons
        if actual != expected
    ]
    if output.is_srgb != bool(state.slots[Slot.OUTPUT_SRGB]):
        problems.append(
            f"color space: expected {'sRGB' if state.slots[Slot.OUTPUT_SRGB] else 'linear'}, "
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


def collect_source_files() -> list[Path]:
    raw_inputs = list(sys.argv[1:])
    if raw_inputs:
        print(f"Received {len(raw_inputs)} path(s) from the script launch.")
    while True:
        print(
            "\nAdd DDS files or folders by drag/drop or paste. Press Enter after each line.\n"
            "When all paths have been added, press Enter on an empty line."
        )
        while True:
            raw = input("> ")
            if not raw.strip():
                if raw_inputs:
                    break
                print("[ERROR] Add at least one DDS file or folder before finishing.")
                continue
            try:
                added_inputs = split_windows_input(raw)
            except (OSError, ValueError) as exc:
                print(f"[ERROR] Could not parse input paths: {exc}")
                continue
            if not added_inputs:
                print("[ERROR] No path was found on that line.")
                continue
            raw_inputs.extend(added_inputs)
            print(f"Added {len(added_inputs)} path(s); {len(raw_inputs)} collected so far.")
        files, warnings = collect_dds_files(raw_inputs)
        for warning in warnings:
            print(f"[WARNING] {warning}")
        if files:
            return files
        print("[ERROR] None of the collected paths contained usable DDS files. Please try again.")
        raw_inputs = []


def inspect_sources(texdiag: Path, files: list[Path]) -> list[FileState]:
    states: list[FileState] = []
    print(f"\nFound {len(files)} DDS file(s).")
    print("\nSource texture information:")
    for index, source in enumerate(files, start=1):
        try:
            metadata = read_metadata(texdiag, source)
            if "TYPELESS" in metadata.format:
                raise RuntimeError(f"typeless format {metadata.format}; color space cannot be preserved safely")
            shape_problem = unsupported_texture_shape(source, metadata)
            if shape_problem:
                raise RuntimeError(shape_problem)
            state = create_file_state(source, metadata)
            color_space = "sRGB" if metadata.is_srgb else "linear"
            normal_status = "normal=yes" if is_normal_texture(source) else "normal=no"
            print(
                f"[{index}/{len(files)}] {source.name} | {metadata.width}x{metadata.height} | "
                f"{metadata.format} | mips={metadata.mip_levels} | {color_space} | "
                f"alpha={metadata.alpha_mode} | {normal_status}"
            )
        except (KeyError, RuntimeError) as exc:
            state = create_file_state(source, None, str(exc))
            print(f"[{index}/{len(files)}] [FAILED] {source.name}: {exc}")
        states.append(state)
    return states


def display_round_settings(options: UserOptions, states: list[FileState], round_number: int) -> None:
    target = "preserve each original size" if options.target_size == 0 else f"{options.target_size}x{options.target_size}"
    print(f"\nConversion settings - round {round_number}:")
    print(f"  Target:       {target}")
    print(f"  Non-square:   {options.non_square_policy}")
    print(f"  Small policy: {options.small_policy}")
    print(f"  Compression:  {compression_label(int(options.compression))}")
    print(f"  Normal maps:  {normal_texture_treatment_label(int(options.normal_treatment))}")
    if options.bc1_alpha != BC1AlphaTreatment.NOT_APPLICABLE:
        print(f"  BC1 alpha:    {bc1_transparency_label(int(options.bc1_alpha))}")
    print(f"  sRGB mode:    {int(options.srgb_mode)}")
    print("  Filter:       FANT (TRIANGLE fallback if DirectXTex rejects FANT)")
    print(f"  Mipmap mode:  {options.mip_mode}")
    counts = {status: sum(state.status == status for state in states) for status in FileStatus}
    print(f"  Valid:        {counts[FileStatus.ACTIVE]}")
    print(f"  Policy skip:  {counts[FileStatus.POLICY_SKIPPED]}")
    print(f"  Invalid:      {counts[FileStatus.INVALID_COMBINATION]}")
    for state in states:
        for note in state.notes:
            print(f"  [NOTE] {state.path.name}: {note}")
        if state.status in {FileStatus.POLICY_SKIPPED, FileStatus.INVALID_COMBINATION}:
            print(f"  [{state.status.name}] {state.path.name}: {state.reason}")


def stage_round(
    texconv: Path,
    texdiag: Path,
    states: list[FileState],
    options: UserOptions,
    round_number: int,
) -> tuple[dict[Path, Path], RunLog]:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    size_label = "OriginalSize" if options.target_size == 0 else str(options.target_size)
    format_label = compression_label(int(options.compression))
    staging_by_parent: dict[Path, Path] = {}
    log = RunLog(console=False)
    log.write(f"ResizeDDS round {round_number}: {datetime.now().isoformat(timespec='seconds')}")
    for state in states:
        if state.status != FileStatus.ACTIVE:
            log.write(f"[{state.status.name}] {state.path}: {state.reason}")

    valid_states = [state for state in states if state.status == FileStatus.ACTIVE]
    progress = BatchProgress(len(valid_states))
    if valid_states:
        print("", flush=True)
    for index, state in enumerate(valid_states, start=1):
        metadata = state.metadata
        assert metadata is not None
        source = state.path
        progress.update(source)
        staging = staging_by_parent.get(source.parent)
        if staging is None:
            try:
                staging = unique_staging_directory(
                    source.parent, size_label, f"{format_label}_R{round_number}", timestamp
                )
            except OSError as exc:
                state.status = FileStatus.CONVERSION_FAILED
                state.reason = f"could not create staging directory: {exc}"
                log.write(f"[FAILED] {source}: {state.reason}")
                progress.failure(source)
                continue
            staging_by_parent[source.parent] = staging
            log.write(f"Staging directory: {staging}")

        log.write("")
        log.write(
            f"[{index}/{len(valid_states)}] {source.name}: "
            f"{metadata.width}x{metadata.height} {metadata.format} -> "
            f"{state.slots[Slot.OUTPUT_WIDTH]}x{state.slots[Slot.OUTPUT_HEIGHT]} "
            f"{format_name(state.slots[Slot.OUTPUT_FORMAT])}"
        )
        for note in state.notes:
            log.write(f"  [NOTE] {note}")

        conversion_source = source
        reinterpret_directory: Path | None = None
        if ColorAction(state.slots[Slot.COLOR_ACTION]) == ColorAction.REINTERPRET_LINEAR:
            try:
                conversion_source, reinterpret_directory = create_linear_interpretation_copy(
                    source, staging, metadata.format
                )
                log.write(f"  Reinterpreting {metadata.format} as linear without changing color values.")
            except (OSError, RuntimeError) as exc:
                state.status = FileStatus.CONVERSION_FAILED
                state.reason = f"could not prepare linear reinterpretation: {exc}"
                log.write(f"  [FAILED] {state.reason}")
                progress.failure(source)
                continue

        result: subprocess.CompletedProcess[str] | None = None
        generated: Path | None = None
        used_filter = ""
        try:
            for filter_name in ("FANT", "TRIANGLE"):
                command = build_texconv_command(texconv, state, conversion_source, staging, filter_name)
                result = run_command(command)
                for line in result.stdout.strip().splitlines():
                    log.write(f"  {line}")
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
                    log.write("  [RETRY] FANT was rejected; retrying with TRIANGLE.")
        except RuntimeError as exc:
            state.status = FileStatus.INVALID_COMBINATION
            state.reason = str(exc)
            log.write(f"  [INVALID COMBINATION] {exc}")
        finally:
            if reinterpret_directory is not None:
                shutil.rmtree(reinterpret_directory, ignore_errors=True)

        if state.status == FileStatus.INVALID_COMBINATION:
            progress.failure(source)
            continue
        if result is None or result.returncode != 0 or generated is None:
            exit_code = result.returncode if result is not None else "not run"
            state.status = FileStatus.CONVERSION_FAILED
            state.reason = f"texconv failed with exit code {exit_code}"
            if generated is None:
                state.reason += "; expected output file was not found"
            log.write(f"  [FAILED] {state.reason}")
            progress.failure(source)
            continue

        try:
            expected_alpha = ALPHA_MODE_NAMES[state.slots[Slot.EXPECTED_ALPHA_MODE]]
            restore_alpha_mode_metadata(generated, expected_alpha)
            output_metadata = read_metadata(texdiag, generated)
            problems = validate_output_for_state(state, output_metadata)
            if problems:
                raise RuntimeError("; ".join(problems))
        except (KeyError, RuntimeError) as exc:
            state.status = FileStatus.CONVERSION_FAILED
            state.reason = f"validation failed: {exc}"
            log.write(f"  [FAILED VALIDATION] {exc}")
            quarantine_failed_output(generated)
            progress.failure(source)
            continue

        state.status = FileStatus.STAGED
        state.staged = generated
        state.output_metadata = output_metadata
        log.write(
            f"  [OK] {output_metadata.width}x{output_metadata.height}, {output_metadata.format}, "
            f"{output_metadata.mip_levels} mip(s), alpha={output_metadata.alpha_mode}, filter={used_filter}"
        )
        progress.success(source)

    progress.finish()
    log.save_to(staging_by_parent.values())
    return staging_by_parent, log


def finalize_round(
    states: list[FileState],
    staging_by_parent: dict[Path, Path],
    log: RunLog,
    texdiag: Path,
) -> int:
    staged_states = [state for state in states if state.status == FileStatus.STAGED]
    action = prompt_staging_action(bool(staged_states))
    if action == 0:
        for state in staged_states:
            state.status = FileStatus.KEPT_STAGING
        log.write("Staging kept; originals were not changed.")
        log.save_to(staging_by_parent.values())
        print("Staged files were kept. Originals remain unchanged.")
        return 0
    if action == 2:
        remove_staging_directories(staging_by_parent)
        print("This round's staging was deleted; every file from the round will be retried.")
        return 2

    install_failures: list[FileState] = []
    progress = BatchProgress(len(staged_states))
    if staged_states:
        print("", flush=True)
    for state in staged_states:
        assert state.metadata is not None and state.staged is not None and state.output_metadata is not None
        job = TextureJob(state.path, state.metadata, state.staged, state.output_metadata)
        progress.update(state.path)
        try:
            install_staged_job(job, texdiag)
            state.status = FileStatus.INSTALLED
            log.write(f"[INSTALLED] {state.path}")
            progress.success(state.path)
        except (OSError, RuntimeError) as exc:
            state.status = FileStatus.INSTALL_FAILED
            state.reason = str(exc)
            install_failures.append(state)
            log.write(f"[INSTALL FAILED] {state.path}: {exc}")
            progress.failure(state.path)
    progress.finish()
    log.save_to(staging_by_parent.values())
    if install_failures:
        for state in install_failures:
            print(f"[INSTALL_FAILED] {state.path}: {state.reason}")
        print("Some replacements failed, so this round's staging was kept for recovery.")
    else:
        remove_staging_directories(staging_by_parent)
        print("All replacements succeeded; this round's staging was deleted.")
    return 1


def main() -> int:
    print("=" * 62)
    print("DDS Batch Downsizer - Microsoft DirectXTex")
    print("=" * 62)
    print("Quick use:")
    print("  1. Add DDS files/folders one line at a time; finish with an empty line.")
    print("  2. Each round applies one option set to all currently active files.")
    print("  3. Skipped, invalid, and failed files can be retried with new options.\n")
    try:
        texconv = find_directxtex_tool("texconv")
        texdiag = find_directxtex_tool("texdiag")
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        return 2

    states = inspect_sources(texdiag, collect_source_files())
    if not any(state.status == FileStatus.ACTIVE for state in states):
        print("[ERROR] None of the selected textures could be inspected safely.")
        return 1

    round_number = 1
    while True:
        round_states = [state for state in states if state.status == FileStatus.ACTIVE]
        if not round_states:
            break
        print(f"\n{'=' * 62}\nROUND {round_number}: {len(round_states)} active file(s)")
        options = prompt_user_options()
        for state in round_states:
            resolve_file_state(state, options)
        display_round_settings(options, round_states, round_number)

        valid_count = sum(state.status == FileStatus.ACTIVE for state in round_states)
        ignored_count = len(round_states) - valid_count
        if not prompt_conversion_confirmation(valid_count, ignored_count):
            print("Conversion aborted. No new staging folder was created.")
            break

        staging_by_parent: dict[Path, Path] = {}
        log = RunLog()
        if valid_count:
            staging_by_parent, log = stage_round(texconv, texdiag, round_states, options, round_number)

        staged_count = sum(state.status == FileStatus.STAGED for state in round_states)
        print(f"\nRound {round_number} complete: {staged_count} staged")
        if staged_count:
            for staging in staging_by_parent.values():
                print(f"Staged output: {staging}", flush=True)
        for status in (FileStatus.POLICY_SKIPPED, FileStatus.INVALID_COMBINATION, FileStatus.CONVERSION_FAILED):
            count = sum(state.status == status for state in round_states)
            if count:
                print(f"  {status.name}: {count}")

        if staged_count:
            final_action = finalize_round(round_states, staging_by_parent, log, texdiag)
            if final_action == 2:
                for state in round_states:
                    reset_for_retry(state)
                round_number += 1
                continue
        elif staging_by_parent:
            log.save_to(staging_by_parent.values())
            remove_staging_directories(staging_by_parent)
            print("No validated outputs were produced; empty/failed staging was removed.")

        leftovers = [
            state
            for state in round_states
            if state.status
            in {FileStatus.POLICY_SKIPPED, FileStatus.INVALID_COMBINATION, FileStatus.CONVERSION_FAILED}
        ]
        if not leftovers:
            break
        print("\nFiles not converted in this round:")
        for state in leftovers:
            print(f"  [{state.status.name}] {state.path.name}: {state.reason}")
        if not prompt_retry_leftovers():
            print("Leaving these files unchanged.")
            break
        for state in leftovers:
            reset_for_retry(state)
        round_number += 1

    inspection_failures = [state for state in states if state.status == FileStatus.INSPECTION_FAILED]
    if inspection_failures:
        print("\nInspection failures (not retryable with conversion options):")
        for state in inspection_failures:
            print(f"  {state.path.name}: {state.reason}")
    hard_failures = {
        FileStatus.INVALID_COMBINATION,
        FileStatus.CONVERSION_FAILED,
        FileStatus.INSTALL_FAILED,
        FileStatus.INSPECTION_FAILED,
    }
    return 1 if any(state.status in hard_failures for state in states) else 0


def run_and_pause() -> int:
    try:
        exit_code = main()
    except KeyboardInterrupt:
        print("\nCancelled. Any originals not explicitly installed remain unchanged.")
        exit_code = 130
    except Exception as exc:  # Keep unexpected failures visible when launched from Explorer.
        print(f"\n[UNEXPECTED ERROR] {exc}")
        exit_code = 1

    if sys.stdin.isatty():
        try:
            print("\nClosing automatically in 10 seconds...")
            time.sleep(10)
        except KeyboardInterrupt:
            pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_and_pause())
