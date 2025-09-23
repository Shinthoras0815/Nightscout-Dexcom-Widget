# Build script for Nuitka on Windows
# Usage: Right-click -> Run with PowerShell (or run from a PowerShell prompt)
# Output: build\widget.dist\widget.exe (standalone folder)

param(
    [switch]$OneFile = $false,
    [string]$Icon = '',
    [switch]$NoPause = $false
)

$ErrorActionPreference = 'Stop'

# Ensure venv python
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path $venvPython)) {
    Write-Error "Virtual environment python not found: $venvPython"
}

# Ensure Nuitka installed
& $venvPython -m pip install -U nuitka orderedset zstandard | Out-Host

# Common args
$nargs = @(
    '-m','nuitka',
    '--enable-plugin=tk-inter',
    '--enable-plugin=matplotlib',
    '--enable-plugin=numpy',
    '--include-package-data=pandas',
    '--windows-console-mode=disable',
    '--output-dir=build'
)

if ($OneFile) { $nargs += '--onefile' } else { $nargs += '--standalone' }
if ($Icon -ne '') { $nargs += @('--windows-icon-from-ico', $Icon) }

# Target script
$nargs += 'widget.py'

Write-Host "Python:"
& $venvPython -c "import sys; print(sys.version)" | Out-Host
Write-Host "Nuitka version:"
& $venvPython -m nuitka --version | Out-Host

Write-Host "Building with Nuitka: $($nargs -join ' ')"
& $venvPython @nargs | Out-Host

if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($OneFile) {
    $out = Join-Path $PSScriptRoot 'build\widget.exe'
    if (Test-Path $out) {
        Write-Host "Onefile build created: $out"
    } else {
        Write-Warning "Onefile build not found."
    }
} else {
    $dist = Join-Path $PSScriptRoot 'build\widget.dist\widget.exe'
    if (Test-Path $dist) {
        Write-Host "Standalone build created: $dist"
        Write-Host "Hint: place your .env next to this EXE for settings persistence."
    } else {
        Write-Warning "Standalone build not found."
    }
}

# Keep window open when launched by double-click unless -NoPause was passed
if (-not $NoPause) {
    try {
        if ($Host.Name -eq 'ConsoleHost') {
            Write-Host ""
            Read-Host "Press ENTER to close this window"
        }
    } catch {}
}
