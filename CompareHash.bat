@echo off
setlocal

:main
cls
echo ==============================================
echo Duplicate File Checker (Size and SHA256 Hash)
echo ==============================================
echo.

:: Clear variables for the new loop
set "file1="
set "file2="

:: Get file paths via drag and drop
set /p "file1=Drag and drop the FIRST file here and press Enter: "
set /p "file2=Drag and drop the SECOND file here and press Enter: "

:: Remove accidental quotes from drag-and-drop
set "file1=%file1:"=%"
set "file2=%file2:"=%"

:: Verify files exist
if not exist "%file1%" (
    echo [ERROR] First file not found.
    goto ask_loop
)
if not exist "%file2%" (
    echo [ERROR] Second file not found.
    goto ask_loop
)

echo.
echo Checking file sizes...

:: Get file sizes
for %%I in ("%file1%") do set size1=%%~zI
for %%I in ("%file2%") do set size2=%%~zI

echo File 1: %size1% bytes
echo File 2: %size2% bytes

:: Compare sizes first
if not "%size1%"=="%size2%" (
    echo.
    echo [RESULT] FILES ARE DIFFERENT ^(Sizes do not match^).
    goto ask_loop
)

echo.
echo Sizes match. Calculating SHA256 hashes ^(this may take a moment for large files^)...

:: Get file hashes (filtering out the text lines from certutil output)
for /f "tokens=*" %%A in ('certutil -hashfile "%file1%" SHA256 ^| find /v ":"') do set "hash1=%%A"
for /f "tokens=*" %%B in ('certutil -hashfile "%file2%" SHA256 ^| find /v ":"') do set "hash2=%%B"

:: Remove spaces from hash strings just in case
set "hash1=%hash1: =%"
set "hash2=%hash2: =%"

echo File 1 Hash: %hash1%
echo File 2 Hash: %hash2%
echo.

:: Compare hashes
if "%hash1%"=="%hash2%" (
    echo [RESULT] EXACT MATCH! These files are duplicates.
) else (
    echo [RESULT] DIFFERENT! Sizes match, but content is different.
)

:ask_loop
echo.
echo Press ENTER to compare another pair, or SPACE to exit...
powershell -Command "$key = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown'); exit $key.VirtualKeyCode"
if %errorlevel% equ 13 goto main
if %errorlevel% equ 32 exit
goto ask_loop