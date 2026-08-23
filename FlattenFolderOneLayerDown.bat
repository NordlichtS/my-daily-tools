@echo off
setlocal enabledelayedexpansion

echo Flattening one layer down in "%cd%"

rem Loop over each immediate folder in current directory (A, B, C, ...)
for /d %%P in (*) do (
    if exist "%%P\" (
        echo.
        echo Processing parent folder: %%P

        rem For each immediate subfolder inside the parent (A1, A2, ...)
        for /d %%S in ("%%P\*") do (
            rem Move files that are directly inside %%S up into the parent %%P
            for %%F in ("%%S\*") do (
                if not exist "%%~fF\" (
                    move "%%F" "%%P\" >nul 2>&1
                )
            )

            rem Move any subfolders inside %%S as whole folders into the parent %%P
            for /d %%T in ("%%S\*") do (
                move "%%T" "%%P\" >nul 2>&1
            )

            rem Remove the now-empty subfolder %%S (if empty)
            rd "%%S" 2>nul
        )
    )
)

echo.
echo Done.
endlocal
pause
