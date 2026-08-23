@echo off
setlocal enabledelayedexpansion

:: Step 1: Collect all file names (except this script) into memory
set count=0
for %%F in (*) do (
    if /i not "%%F"=="%~nx0" (
        set /a count+=1
        set "file[!count!]=%%F"
    )
)

if %count%==0 (
    echo No files found to replace.
    pause
    exit /b
)

echo Found %count% files. Moving originals to Recycle Bin and creating empty replacements...

:: Step 2 & 3: Recycle the original and immediately create the empty replacement
for /l %%i in (1,1,%count%) do (
    set "current_file=!file[%%i]!"
    
    :: Move the original file to the Recycle Bin using PowerShell
    powershell -NoProfile -Command "Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile((Get-Item '!current_file!').FullName, 'OnlyErrorDialogs', 'SendToRecycleBin')"
    
    :: Create the empty file with the exact same name
    type nul > "!current_file!"
)

echo Done! All original files are now in your Recycle Bin, and replaced with empty copies.
pause