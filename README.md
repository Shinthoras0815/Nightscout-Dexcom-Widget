# Nightscout / Dexcom Desktop Widget & Mini Dashboard

A lightweight Python desktop widget (Tkinter + embedded Matplotlib) plus an optional full dashboard view for visualizing recent glucose data from a Nightscout instance and/or live Dexcom readings (via `pydexcom`). The focus is a compact always‑on‑top glucose window with trend, insulin and carb context, and a secondary richer dashboard for historical context (6h), basal delivery, and events.

> Disclaimer: This project is for personal / educational use only and is **not** a medical device. Always verify therapy decisions with approved tools.

## Key Features
* Always‑on‑top desktop widget (movable, borderless, dark theme)
* Full dashboard (6h window) with:
  * Dynamic glucose axis (auto‑padding + optional overrides)
  * Target range shading
  * Basal vs. temp basal step plot + fill highlighting deviations
  * Event annotations (Bolus, Carbs) without label clipping (grouped placement logic)
  * SMB (Super Micro Bolus) shown as small blue triangles (no text clutter)
  * Shared vertical hover cursor (time, BG, basal, nearest event)
* Widget modes: normal / compact / minimal / Dexcom‑only
* Trend arrow (Nightscout direction or computed slope fallback)
* Delta (Δ) in mmol/L derived or native (Nightscout delta / trendDelta / tick)
* IOB breakdown: Bolus‑IOB, Basal‑IOB, Total‑IOB (with decay fallback if status incomplete)
* COB parsing with multi‑location fallback
* Temp basal detection (absolute or %), remaining minutes
* Sensor age (SAGE) detection from `devicestatus` or fallback single treatment lookup (`Sensor Change`) – cached in memory
* Robust timezone normalization (flags for: assume naive UTC, force offset, convert to naive local)
* Dynamic annotation headroom so labels are not truncated at plot top
* Optional Dexcom direct mode (suppresses Nightscout‑specific UI, minimal display only)
* Environment‑driven configuration (.env) + safe `.env.example`
* Nuitka build script for producing a standalone executable

## Folder Overview
| File | Purpose |
|------|---------|
| `widget.py` | Tkinter overlay widget + embedded 1h chart (configurable) |
| `dashboard.py` | 6h Matplotlib dashboard with hover cursor + events |
| `net.py` | Network helpers (Nightscout requests + env refresh) |
| `build_nuitka.ps1` | Windows PowerShell script to build an optimized executable |
| `.env.example` | Template for local secrets (copy to `.env`) |
| `requirements.txt` | Project dependencies |

## Quick Start
1. Create & activate a virtual environment (recommended):
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate
   ```
2. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
3. Copy the template and configure credentials:
   ```powershell
   copy .env.example .env
   ```
   Fill in at minimum:
   * `NIGHTSCOUT_URL`
   * `NS_TOKEN` (preferred) or `NS_API_SECRET`
   Optionally (for Dexcom direct mode): `DEXCOM_USERNAME`, `DEXCOM_PASSWORD`, `DEXCOM_REGION` (`us` / `eu` / `ous`).
4. Run the widget:
   ```powershell
   python widget.py
   ```
5. (Optional) Launch the dashboard from the widget context menu (unless in Dexcom‑only mode) or directly:
   ```powershell
   python dashboard.py
   ```

## Usage Notes
### Widget Modes
Right‑click the widget for a context menu (Nightscout mode):
* Toggle always‑on‑top
* Switch Compact / Minimal / Full diagram mode
* Open Dashboard window
* Open Settings (edit and persist `.env` values)
* Quit

In Dexcom‑only mode (`USE_DEXCOM=1`), only minimal essentials are shown (BG + trend + age). Other Nightscout‑specific lines and the chart are hidden for a cleaner “glance” view.

### Event Rendering
* Bolus (non‑SMB): red label `B 1.2 IE` at/near glucose point
* Carbs: green label `C 25g`
* SMB: blue downward triangle near x‑axis (no label)
* Grouping prevents overlap at identical timestamps; labels move below if top would clip

### Basal / Temp Basal
The dashboard and widget derive a minute‑resolution basal series from profile + temp basal treatments. Deviations are filled (orange shading). Active temp text shows absolute U/h or %+ and remaining minutes.

### Delta & Trend
Delta uses (in order): Nightscout `delta`, `trendDelta`, `tick`, else computed difference of last two entries. Converted to mmol/L `Δ +0.3` with one decimal.

## Environment Variables
| Variable | Default | Description |
|----------|---------|-------------|
| `NIGHTSCOUT_URL` | – | Base URL of your Nightscout instance (https://...) |
| `NS_TOKEN` | – | API token (preferred) |
| `NS_API_SECRET` | – | Legacy API secret (only if token not used) |
| `USE_DEXCOM` | 0 | Enable Dexcom direct minimal mode (suppresses dashboard) |
| `DEXCOM_USERNAME` / `DEXCOM_PASSWORD` | – | Dexcom credentials |
| `DEXCOM_REGION` | US | Region (`US`, `OUS`, `JP`) |
| `WIDGET_WINDOW_MIN` | 60 | Minutes displayed in widget chart window |
| `BG_YMIN` / `BG_YMAX` | dynamic | Force glucose axis bounds (mmol/L) |
| `FORCE_TZ_OFFSET_MINUTES` | 0 | Manual minute offset added to all timestamps (drift compensation) |
| `FORCE_TZ_ASSUME_UTC` | 1 | Treat naive timestamps as UTC |
| `FORCE_NAIVE_LOCAL` | 1 | Convert aware → naive local (prevents Matplotlib double conversion) |
| `DEBUG_TIME` | 0 | Verbose time normalization debug prints |
| `DEBUG_AGE` | 0 | Sensor age extraction debug prints |

Example `.env` snippet:
```env
NIGHTSCOUT_URL=https://your-nightscout.example
NS_TOKEN=yourToken
USE_DEXCOM=0
WIDGET_WINDOW_MIN=90
BG_YMIN=3.5
BG_YMAX=11.0
FORCE_TZ_ASSUME_UTC=1
FORCE_NAIVE_LOCAL=1
DEBUG_TIME=0
DEBUG_AGE=0
DEBUG_DEXCOM=0
```

## Dexcom Direct Mode (pydexcom)

This project can fetch live readings directly from the Dexcom Share service using the `pydexcom` library (no Nightscout required) when `USE_DEXCOM=1` is set. The widget then switches into a minimal view (glucose + trend + age) and hides Nightscout‑specific insulin / carb / basal context.

Reference implementation & FAQ: https://github.com/gagebenne/pydexcom

### Prerequisites (per Dexcom / Share requirements)
1. The official Dexcom mobile app (G7 / G6 / G5 etc.) is running and paired with your sensor.
2. The Share feature is enabled inside the app.
3. At least one follower is configured (Share stays disabled until a follower exists), but you still use **your own** credentials here, not the follower’s.
4. Your account region matches the region you configure (US / OUS / JP).

### Environment Variables (Direct Mode)
```env
USE_DEXCOM=1
DEXCOM_USERNAME=your_dexcom_login
DEXCOM_PASSWORD=your_dexcom_password
# Allowed values: US, OUS, JP (case‑insensitive)
DEXCOM_REGION=OUS
# Optional verbose logging of login attempts and reading mapping
DEBUG_DEXCOM=0
```

### Region Selection
| Value | Meaning |
|-------|---------|
| US    | United States Dexcom Share servers |
| OUS   | “Outside US” (International / Europe / Rest of World) |
| JP    | Japan servers (supported in recent pydexcom versions) |

If `DEXCOM_REGION` is missing or invalid, the code will try `OUS` first, then `US`. For best reliability set it explicitly.

If you rely purely on Dexcom mode but want IOB/COB context: consider running a personal Nightscout and switch off `USE_DEXCOM` to regain enhanced metrics.


## Building a Standalone Executable (Windows / Nuitka)
You can build a self‑contained EXE (no Python install required):
```powershell
pwsh ./build_nuitka.ps1
```
The script will:
* Use your virtual environment interpreter
* Compile `widget.py` (and dependencies) into an optimized binary
* Produce output under `build/` and/or a distributable `.exe`

> Tip: Keep the `.env` file next to the executable so settings can still be adjusted without rebuilding.

