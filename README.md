# my-daily-tools
simple scripts to make life easier

====

## CompareHash.bat
use windows shell to compaire if two files are identical. 

double click to open, drag two files one after another into the window and see if they are the same.

====

## CheckFolderMergeSafety.bat

Checks two folders before a recursive merge and reports files that share the same relative path. It compares the colliding files by size and SHA-256 hash, labels them as identical or different, and does not modify either folder.

Double-click the script, then paste the two folder paths when prompted.

====

## clear-empty-line.py

Limits consecutive blank lines in a UTF-8 text file. Drag a text file onto the script, then choose the maximum number of consecutive blank lines to keep (`0` through `8`).

The selected file is overwritten in place, so make a backup first if needed.

====

## DeduplicateResources.py

Deduplicates files directly inside a resource folder and updates matching `filename = relative/path` references in one text or configuration file. Byte-identical redundant files are moved to a timestamped quarantine folder, which also contains a backup of the text file and a report. An optional manual mode can group non-identical files that you have confirmed are interchangeable.

Double-click the script and follow its prompts. It shows and verifies a plan before making changes; nothing in quarantine is deleted automatically.

====

## fakefiles.bat

Replaces every file in the script's current folder (except the script itself) with an empty file of the same name. The original files are sent to the Windows Recycle Bin.

This is intentionally destructive: place the script in the target folder and double-click it only when you want to create zero-byte stand-ins for all files there.

====

## FlattenFolder.bat

Flattens one directory level. It moves the files and subfolders found directly inside each immediate child folder into the current folder; it does not remove the now-empty child folders.

Run it from the folder whose immediate children you want to flatten. Name collisions may cause moves to fail or prompt for confirmation.

====

## FlattenFolderOneLayerDown.bat

Flattens one level inside each immediate child folder. For a structure such as `A/A1/files`, it moves the contents of `A1` into `A`, then removes `A1` if it is empty.

Run it from the common parent folder. Move errors and name collisions are suppressed, so check the result afterward.

====

## FlattenFolderRecursive.bat

Moves every file from all nested subfolders into the current folder, then removes folders that became empty.

Run it from the folder you want to flatten completely. Files with the same name can collide, so use it only after checking the contents or making a backup.

====

## ResizeDDS.py

Batch-resizes and converts DDS textures using Microsoft's DirectXTex tools while validating important DDS metadata with `texdiag`.

### Requirements

- Windows with Python 3 installed and associated with `.py` files.
- `texconv.exe` and `texdiag.exe` placed in the same folder as `ResizeDDS.py`, or installed and callable from Command Prompt or PowerShell. Local copies are preferred when both are available.

### Usage

1. Double-click `ResizeDDS.py`. You can also drag a DDS file or folder directly onto the script.
2. Drag or paste DDS files and folders into the window one line at a time. Press Enter after each entry, then submit an empty line when everything has been added. All collected files are processed as one batch; folder scanning is not recursive. Duplicate files are removed automatically.
3. Review the source information printed by `texdiag`.
4. Choose the target size, non-square handling, smaller-image handling, output compression, sRGB handling, and mipmap behavior.
5. Review how many textures have valid settings, are skipped by policy, or contain an incompatible combination. Enter `1` to create the valid outputs, or `0` to cancel.
6. Converted files are first written to a timestamped `_DDS_Resized_...` folder inside the source folder. Originals are unchanged at this point.
7. Choose the final action for that round: `0` keeps staging without changing originals, `1` installs replacements and deletes staging, and `2` deletes staging and retries every file from the round with new options.
8. After action `0` or `1`, skipped, invalid, and failed files can be retried as another round without selecting or inspecting them again.

Important options:

- Target size `0` preserves each source's dimensions.
- Compression `0` preserves each source's existing format, including existing BC2/BC4/BC5/BC6 files.
- Non-square mode `2` chooses the largest power-of-two width and height that do not exceed either the source dimensions or target-size cap.
- sRGB mode `0` preserves color-space labels, `1` treats stored values as linear, and `2` converts sRGB values to linear output.
- Mipmap mode `0` preserves whether each source had a chain, `1` keeps only the biggest mip level, and `2` generates full chains for all.

Compression reference ([Microsoft Direct3D 11 documentation](https://learn.microsoft.com/en-us/windows/win32/direct3d11/texture-block-compression-in-direct3d-11)):

| Input | Output | Typical channel use | Bits per pixel |
| --- | --- | --- | ---: |
| `0` | Preserve | Keep each source's existing DXGI format | Varies |
| `1` | BC1 | RGB with optional 1-bit alpha | 4 |
| `2` | BC2 | RGB with explicit 4-bit alpha | 8 |
| `3` | BC3 | RGB with interpolated alpha | 8 |
| `4` | BC4 | One linear channel | 4 |
| `5` | BC5 | Two linear channels, often normal-map X/Y | 8 |
| `6` | BC6H UF16 | Three-channel unsigned HDR half-float | 8 |
| `7` | BC7 | High-quality RGB or RGBA | 8 |
| `8` | RGBA8888 | Uncompressed RGBA | 32 |

BC4, BC5, and BC6H have no sRGB output variant. Incompatible combinations are marked before conversion and can be retried with different options. An sRGB-labelled file detected as a normal map and explicitly overridden to BC5 is reinterpreted as linear without changing its stored X/Y values.

BC1, BC2, and BC3 output always uses uniform RGB-channel error weighting (`texconv -bc u`) instead of the default perceptual weighting. This is more suitable for normal maps and other textures whose channels contain data. Other formats are left unchanged because DirectXTex does not support this option for them.

Keep a separate backup of important game files. Staging is safe, but final action `1` replaces the originals without creating `.bak` copies. After the final action, the tool waits 10 seconds and closes automatically.
