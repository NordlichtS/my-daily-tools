@echo off
setlocal
title Folder Merge Safety Checker
cls
echo =====================================================
echo              Folder Merge Safety Checker
echo =====================================================
echo.
echo This checks files that would overwrite each other in a
echo recursive folder merge: same relative path in both folders.
echo It does not change, copy, move, or delete any files.
echo.
set /p "MERGE_LEFT=Paste the FIRST folder path, then press Enter: "
set /p "MERGE_RIGHT=Paste the SECOND folder path, then press Enter: "
set "MERGE_SCRIPT=%~f0"
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$c=Get-Content -Raw -LiteralPath $env:MERGE_SCRIPT;$c=$c.Substring($c.LastIndexOf(':POWERSHELL')+11);Invoke-Expression $c"
set "MERGE_EXIT=%ERRORLEVEL%"
echo.
if not "%MERGE_EXIT%"=="0" echo The check stopped because of an error. See the message above.
echo.
pause
exit /b %MERGE_EXIT%

:POWERSHELL
$ErrorActionPreference='Stop'
function Get-Folder($name) {
  $p=([string][Environment]::GetEnvironmentVariable($name)).Trim().Trim('"')
  if (!(Test-Path -LiteralPath $p -PathType Container)) { throw "Folder not found: $p" }
  return (Get-Item -LiteralPath $p).FullName.TrimEnd('\')
}
$a=Get-Folder 'MERGE_LEFT'
$b=Get-Folder 'MERGE_RIGHT'
$ap=$a+'\'
$bp=$b+'\'
$right=@{}
Get-ChildItem -LiteralPath $b -Recurse -File | ForEach-Object { $right[$_.FullName.Substring($bp.Length)]=$_ }
$total=0;$same=0;$different=0;$differentPaths=@()
Write-Host ''
Write-Host 'Comparing overwrite collisions (same relative path)...' -ForegroundColor Cyan
Get-ChildItem -LiteralPath $a -Recurse -File | Sort-Object FullName | ForEach-Object {
  $left=$_;$relative=$left.FullName.Substring($ap.Length)
  if ($right.ContainsKey($relative)) {
    $other=$right[$relative];$total++
    Write-Host ''
    Write-Host "[$total] $relative" -ForegroundColor Yellow
    Write-Host "  Folder 1: $($left.Length) bytes"
    Write-Host "  Folder 2: $($other.Length) bytes"
    $h1=(Get-FileHash -LiteralPath $left.FullName -Algorithm SHA256).Hash
    $h2=(Get-FileHash -LiteralPath $other.FullName -Algorithm SHA256).Hash
    Write-Host "  SHA256 1: $h1"
    Write-Host "  SHA256 2: $h2"
    if ($left.Length -ne $other.Length) {
      $different++;$differentPaths+=$relative;Write-Host '  Result: DIFFERENT (sizes do not match)' -ForegroundColor Red
    } elseif ($h1 -eq $h2) {$same++;Write-Host '  Result: IDENTICAL' -ForegroundColor Green}
    else {$different++;$differentPaths+=$relative;Write-Host '  Result: DIFFERENT (same size, different content)' -ForegroundColor Red}
  }
}
Write-Host ''
if ($total -eq 0) { Write-Host 'No overwrite collisions were found.' -ForegroundColor Green }
else { Write-Host "Finished: $total collision(s); $same identical, $different different." -ForegroundColor Cyan }
if ($differentPaths.Count -gt 0) {
  Write-Host ''
  Write-Host 'Different files (relative paths only):' -ForegroundColor Red
  $differentPaths | ForEach-Object { Write-Host $_ }
}
