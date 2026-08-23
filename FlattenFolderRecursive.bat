@echo off
echo Moving all files from subfolders into current folder...
for /r %%i in (*) do (
    if not "%%~dpi"=="%cd%\" move "%%i" "%cd%"
)

echo Removing empty folders...
for /f "delims=" %%d in ('dir /ad/b/s ^| sort /R') do rd "%%d" 2>nul

echo Done.
pause
