# my-daily-tools
simple scripts to make life easier

====

## CompareHash.bat
use windows shell to compaire if two files are identical. 

double click to open, drag two files one after another into the window and see if they are the same.

====

## ResizeDDS.py

Batch-resizes and converts DDS textures using Microsoft's DirectXTex tools while validating important DDS metadata with `texdiag`.

### Requirements

- Windows with Python 3 installed and associated with `.py` files.
- `texconv.exe` and `texdiag.exe` placed in the same folder as `ResizeDDS.py`, or installed and callable from Command Prompt or PowerShell. Local copies are preferred when both are available.

### Usage

1. Double-click `ResizeDDS.py`. You can also drag DDS files or a folder directly onto the script.
2. If prompted, drag or paste one or more DDS files, or one folder, into the window and press Enter. Folder scanning is not recursive.
3. Review the source information printed by `texdiag`.
4. Choose the target size, non-square handling, smaller-image handling, output compression, sRGB handling, and mipmap behavior.
5. Review how many textures will be converted or ignored. Enter `1` to create the converted files, or `0` to cancel.
6. Converted files are first written to a timestamped `_DDS_Resized_...` folder inside the source folder. Originals are unchanged at this point.
7. Choose the final action: `0` keeps staging without changing originals, `1` installs replacements and deletes staging, and `2` deletes staging and restarts the tool.

Important options:

- Target size `0` preserves each source's dimensions.
- Compression `0` preserves each source's existing format, including existing BC2/BC4/BC5/BC6 files.
- Non-square mode `2` chooses the largest power-of-two width and height that do not exceed either the source dimensions or target-size cap.
- sRGB mode `0` preserves color-space labels, `1` treats stored values as linear, and `2` converts sRGB values to linear output.
- Mipmap mode `0` preserves whether each source had a chain, `1` generates chains for all, and `2` removes chains.

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

BC4, BC5, and BC6H have no sRGB output variant. When sRGB mode `0` is selected, incompatible sRGB sources are ignored rather than silently losing their color-space label.

BC1, BC2, and BC3 output always uses uniform RGB-channel error weighting (`texconv -bc u`) instead of the default perceptual weighting. This is more suitable for normal maps and other textures whose channels contain data. Other formats are left unchanged because DirectXTex does not support this option for them.

Keep a separate backup of important game files. Staging is safe, but final action `1` replaces the originals without creating `.bak` copies. After the final action, the tool waits 10 seconds and closes automatically.
