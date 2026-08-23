@echo off
setlocal enabledelayedexpansion

echo Flattening one layer of subfolders...

for /d %%D in (*) do (
    if exist "%%D\" (
        echo Processing folder: %%D

        rem Move files directly inside this folder
        for %%F in ("%%D\*") do (
            if not "%%~aF"=="d" move "%%F" .
        )

        rem Move subfolders as whole units
        for /d %%S in ("%%D\*") do (
            move "%%S" .
        )
    )
)

echo Done.
pause
