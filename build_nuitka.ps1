# Build script for Nuitka on Windows
# Usage: Right-click -> Run with PowerShell (or run from a PowerShell prompt)
# Output: build\widget.dist\widget.exe (standalone folder)

param(
    [switch]$OneFile = $false,
    [string]$Icon = '',
    [switch]$NoPause = $false,
    [string]$OutputDir = ''
)

$ErrorActionPreference = 'Stop'

# Ensure venv python
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path $venvPython)) {
    Write-Error "Virtual environment python not found: $venvPython"
}

# Ensure tooling and Nuitka are installed
# Note: 'ordered-set' (hyphen) is a required dependency for Nuitka. Do NOT use 'orderedset' (no hyphen),
# it is a different package with a C extension that fails to build on recent Python versions.
& $venvPython -m pip install -U pip setuptools wheel | Out-Host
& $venvPython -m pip install -U nuitka ordered-set zstandard | Out-Host

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
if ($OutputDir -ne '') {
    # Nuitka writes dist into --output-dir; the OneFile PE goes to that folder too
    $nargs = $nargs | ForEach-Object { $_ -replace '^--output-dir=build$', "--output-dir=$OutputDir" }
}

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
    if ($OutputDir -ne '') {
        $outRoot = $OutputDir
    } else {
        $outRoot = Join-Path $PSScriptRoot 'build'
    }
    $out = Join-Path $outRoot 'widget.exe'
    if (Test-Path $out) {
        Write-Host "Onefile build created: $out"
    } else {
        Write-Warning "Onefile build not found. Prüfe Antivirus/Windows Defender Ausschlüsse für den Build-Ordner und versuche es erneut."
        Write-Host "Tipp: Führe mit ausgeschaltetem AV oder anderem Output-Ordner (z.B. -OutputDir C:\\NuitkaBuild) aus."
    }
} else {
    if ($OutputDir -ne '') {
        $distRoot = $OutputDir
    } else {
        $distRoot = Join-Path $PSScriptRoot 'build'
    }
    $dist = Join-Path $distRoot 'widget.dist\widget.exe'
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
