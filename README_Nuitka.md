# Nuitka Build Guide (Windows)

This project can be compiled into a native Windows executable using Nuitka.

## Prerequisites
- Windows with Visual Studio Build Tools 2022 (C++ workload) installed, or Mingw64 toolchain
- Project virtual environment created at `.venv`

## Build
Run the provided PowerShell script from this folder:

```
# Standalone folder (recommended: .env can live next to the EXE)
PowerShell -ExecutionPolicy Bypass -File .\build_nuitka.ps1

# Onefile EXE (extracts to temp on start)
PowerShell -ExecutionPolicy Bypass -File .\build_nuitka.ps1 -OneFile

# Optional icon
PowerShell -ExecutionPolicy Bypass -File .\build_nuitka.ps1 -Icon .\icon.ico

# Custom output directory (helps with AV exclusions)
PowerShell -ExecutionPolicy Bypass -File .\build_nuitka.ps1 -OneFile -OutputDir C:\\NuitkaBuild
```

Outputs:
- Standalone: `build\widget.dist\widget.exe`
- Onefile: `build\widget.exe`

Place your `.env` next to the executable when using the Standalone build. The app loads and saves `.env` from the application directory (works for Python and compiled builds).

## Autostart
- Press Win+R → `shell:startup` → place a shortcut to the EXE.
- Or create a Task Scheduler entry (At logon) pointing to the EXE.

## Notes
- If build tools are missing, install “Visual Studio Build Tools 2022” and select “C++ Build Tools”.
- The app uses `tk-inter`, `matplotlib`, `numpy`, `pandas`; Nuitka plugins are enabled in the build script.
- For Onefile builds, consider keeping settings in `%APPDATA%` if you prefer portability (current setup keeps `.env` next to EXE by design).

### Troubleshooting OneFile builds on Windows
- Symptom: Build ends with "Failed to add resources to file 'build\widget.exe'" or the resulting EXE is not functional.
- Cause: Windows Defender/Antivirus frequently locks the PE during resource embedding.
- Fixes:
	- Add an exclusion for the project/build folder in Windows Security → Virus & threat protection → Manage settings → Exclusions.
	- Or temporarily disable real-time protection during the build.
	- Or build into a different folder using `-OutputDir C:\\NuitkaBuild` and exclude that folder.
	- As a robust alternative, prefer the Standalone build (`build\\widget.dist\\widget.exe`). Place your `.env` next to the EXE.
