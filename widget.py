# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import threading
import hashlib
import datetime as dt
from typing import Optional, Dict, Any, Tuple, List

import requests
import urllib3
from dotenv import load_dotenv
import dateutil.parser
import tkinter as tk
from tkinter import ttk, messagebox
import pandas as pd
import numpy as np

# Matplotlib embedding
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.dates as mdates
import matplotlib.patheffects as patheffects

# Reuse logic from dashboard for consistency
try:
    from dashboard import (
        fetch_entries as ds_fetch_entries,
        fetch_profile as ds_fetch_profile,
        plan_basal_from_profile as ds_plan_basal_from_profile,
        target_range_from_profile as ds_target_range_from_profile,
        split_events as ds_split_events,
        build_basal_series as ds_build_basal_series,
        latest_devicestatus as ds_latest_devicestatus,
        prefer_devicestatus_metrics as ds_prefer_devicestatus_metrics,
        direction_arrow_from_text as ds_direction_arrow_from_text,
        compute_arrow_from_slope as ds_compute_arrow_from_slope,
    )
    _DASHBOARD_AVAILABLE = True
except Exception:
    _DASHBOARD_AVAILABLE = False

# --- Setup env & HTTP ------------------------------------------------
def _app_base_dir() -> str:
    # When compiled (Nuitka/pyinstaller), use the executable location; otherwise file directory
    try:
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
    except Exception:
        pass
    return os.path.dirname(__file__)

# Load .env from the application directory (works for source and compiled)
load_dotenv(dotenv_path=os.path.join(_app_base_dir(), '.env'))
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = os.getenv("NIGHTSCOUT_URL")
SECRET = os.getenv("NS_API_SECRET") or os.getenv("NIGHTSCOUT_API_SECRET")
TOKEN = os.getenv("NS_TOKEN")
SECRET_SHA1 = hashlib.sha1(SECRET.encode("utf-8")).hexdigest() if SECRET else None

NOW_TZ = dt.timezone.utc

# Defaults
DEFAULT_LOW = 3.9
DEFAULT_HIGH = 10.0
# Chart window length (minutes) – default 60 (1h); override with env WIDGET_WINDOW_MIN
try:
    WINDOW_MIN = int(os.getenv("WIDGET_WINDOW_MIN", "60"))
except Exception:
    WINDOW_MIN = 60

# Optional manual timezone offset correction (minutes). Positive values shift data forward in time.
try:
    _FORCE_TZ_OFFSET_MINUTES = int(os.getenv("FORCE_TZ_OFFSET_MINUTES", "0"))
except Exception:
    _FORCE_TZ_OFFSET_MINUTES = 0

# Assume naive timestamps are UTC (Nightscout häufig) unless overridden
_ASSUME_NAIVE_IS_UTC = os.getenv("FORCE_TZ_ASSUME_UTC", "1").strip().lower() in ("1","true","yes","on")
_DEBUG_TIME = os.getenv("DEBUG_TIME", "0").strip().lower() in ("1","true","yes","on")
_FORCE_NAIVE_LOCAL = os.getenv("FORCE_NAIVE_LOCAL", "1").strip().lower() in ("1","true","yes","on")

# Debug age flag (sensor/site age) optional
DEBUG_AGE = os.getenv("DEBUG_AGE", "0").strip().lower() in ("1","true","yes","on")
# Dexcom debug flag (detailed auth + fetch logging)

DEBUG_DEXCOM = os.getenv("DEBUG_DEXCOM", "0").strip().lower() in ("1","true","yes","on")

# Optional explicit y-axis overrides for BG chart (in mmol/L)
try:
    BG_YMIN_OVERRIDE = float(os.getenv("BG_YMIN", "nan"))
except Exception:
    BG_YMIN_OVERRIDE = float('nan')
try:
    BG_YMAX_OVERRIDE = float(os.getenv("BG_YMAX", "nan"))
except Exception:
    BG_YMAX_OVERRIDE = float('nan')


from net import (
    get_json as ns_get_json,
    BASE as NS_BASE,
    refresh_env as ns_refresh_env,
    TOKEN as NS_TOKEN,
    SECRET_SHA1 as NS_SECRET_SHA1,
)
from net import _resolve_env_path  # reuse path resolution for saving .env


def mgdl_to_mmol(x: float) -> float:
    return round(x / 18.01559, 1)


def latest_entry() -> Dict[str, Any]:
    data = ns_get_json("/api/v1/entries.json", {"count": 1})
    if not data:
        raise RuntimeError("no entries")
    e = data[0]
    val = e.get("sgv") or e.get("mbg") or e.get("glucose")
    mmol = mgdl_to_mmol(float(val)) if isinstance(val, (int, float)) else None
    direction = (e.get("direction") or "").lower()
    ts_raw = e.get("dateString") or e.get("created_at")
    ts = None
    if ts_raw:
        try:
            ts = dateutil.parser.isoparse(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=NOW_TZ)
        except Exception:
            ts = None
    return {"mmol": mmol, "direction": direction, "time": ts}


def fetch_profile_range() -> Tuple[float, float]:
    try:
        prof = ns_get_json("/api/v1/profile.json", {"count": 1})[0]
        store = prof["store"][prof["defaultProfile"]]
        units = str(store.get("units", "mg/dL")).lower()
        lows = store.get("target_low") or store.get("targets") or store.get("targetLower")
        highs = store.get("target_high") or store.get("targetUpper")
        low_val = None
        high_val = None
        if isinstance(lows, list) and lows:
            low_val = lows[0].get("value") if isinstance(lows[0], dict) else lows[0]
        if isinstance(highs, list) and highs:
            high_val = highs[0].get("value") if isinstance(highs[0], dict) else highs[0]
        if (low_val is None or high_val is None) and isinstance(store.get("target"), list):
            t0 = store["target"][0]
            if isinstance(t0, dict):
                low_val = low_val or t0.get("low")
                high_val = high_val or t0.get("high")
        if isinstance(low_val, (int, float)) and isinstance(high_val, (int, float)):
            if units in ("mg/dl", "mgdl"):
                return (low_val / 18.01559, high_val / 18.01559)
            return (float(low_val), float(high_val))
    except Exception:
        pass
    return (DEFAULT_LOW, DEFAULT_HIGH)


def latest_devicestatus() -> Optional[Dict[str, Any]]:
    try:
        items = ns_get_json("/api/v1/devicestatus.json", {"count": 8})
        if not items:
            return None
        def _ts(x: Dict[str, Any]) -> dt.datetime:
            ts_raw = x.get("created_at") or x.get("dateString") or x.get("timestamp")
            d = None
            if ts_raw:
                try:
                    d = dateutil.parser.isoparse(ts_raw)
                except Exception:
                    d = None
            if d is None:
                ms = x.get("mills") or x.get("date")
                if isinstance(ms, (int, float)):
                    d = dt.datetime.fromtimestamp(ms / 1000.0, tz=NOW_TZ)
            if d is None:
                d = dt.datetime.fromtimestamp(0, tz=NOW_TZ)
            if d.tzinfo is None:
                d = d.replace(tzinfo=NOW_TZ)
            return d
        return sorted(items, key=_ts, reverse=True)[0]
    except Exception:
        return None


def metrics_from_status(latest: Optional[Dict[str, Any]]):
    cob_g = None
    bolus_iob = None
    basal_iob = None
    total_iob = None
    pump_batt = None
    reservoir = None
    uploader_batt = None
    sensor_age_min = None
    if latest:
        loop = latest.get("openaps") or latest.get("loop") or {}
        if isinstance(loop, dict):
            cob_obj = loop.get("cob")
            # Handle multiple shapes: {"cob": x}, {"grams": x}, {"amount": x}
            if isinstance(cob_obj, dict):
                if isinstance(cob_obj.get("cob"), (int, float)):
                    cob_g = float(cob_obj["cob"])
                elif isinstance(cob_obj.get("grams"), (int, float)):
                    cob_g = float(cob_obj["grams"])
                elif isinstance(cob_obj.get("amount"), (int, float)):
                    cob_g = float(cob_obj["amount"])
            # sometimes under openaps.suggested.cob
            suggested = loop.get("suggested") if isinstance(loop.get("suggested"), dict) else None
            if suggested and cob_g is None:
                s_cob = suggested.get("cob")
                s_COB = suggested.get("COB")
                if isinstance(s_cob, (int, float)):
                    cob_g = float(s_cob)
                elif isinstance(s_COB, (int, float)):
                    cob_g = float(s_COB)
            iob_obj = loop.get("iob")
            if isinstance(iob_obj, dict):
                it = iob_obj.get("iob")
                bi = iob_obj.get("basaliob") or iob_obj.get("basal_iob")
                if isinstance(it, (int, float)):
                    total_iob = float(it)
                if isinstance(it, (int, float)) and isinstance(bi, (int, float)):
                    bolus_iob = float(it) - float(bi)
                    basal_iob = float(bi)
        pump = latest.get("pump") or {}
        if isinstance(pump, dict):
            pb = pump.get("battery")
            if isinstance(pb, dict):
                if isinstance(pb.get("percent"), (int, float)):
                    pump_batt = f"{int(pb['percent'])}%"
            if isinstance(pump.get("reservoir"), (int, float)):
                reservoir = f"{float(pump['reservoir']):.0f} U"
        # Fallbacks for COB in other locations
        if cob_g is None:
            # sometimes appears top-level as number or object
            top_cob = latest.get("cob") or latest.get("COB")
            if isinstance(top_cob, (int, float)):
                cob_g = float(top_cob)
            elif isinstance(top_cob, dict) and isinstance(top_cob.get("cob"), (int, float)):
                cob_g = float(top_cob.get("cob"))
            elif isinstance(top_cob, dict) and isinstance(top_cob.get("grams"), (int, float)):
                cob_g = float(top_cob.get("grams"))

        # Deep search for COB in loop/openaps nested fields (enacted/mealData/etc.)
        if cob_g is None and isinstance(loop, dict):
            def _find_cob_in(obj, depth=0):
                try:
                    if depth > 3:
                        return None
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if isinstance(k, str) and k.lower() == "cob" and isinstance(v, (int, float)):
                                return float(v)
                        for v in obj.values():
                            res = _find_cob_in(v, depth + 1)
                            if res is not None:
                                return res
                    elif isinstance(obj, list):
                        for v in obj:
                            res = _find_cob_in(v, depth + 1)
                            if res is not None:
                                return res
                except Exception:
                    return None
                return None
            cob_deep = _find_cob_in(loop)
            if isinstance(cob_deep, (int, float)):
                cob_g = float(cob_deep)

        uploader = latest.get("uploader") or {}
        if isinstance(uploader, dict) and isinstance(uploader.get("battery"), (int, float)):
            uploader_batt = f"{int(uploader['battery'])}%"
        # More IOB fallbacks
        if total_iob is None:
            top_iob = latest.get("iob")
            if isinstance(top_iob, (int, float)):
                total_iob = float(top_iob)
            elif isinstance(top_iob, dict):
                it = top_iob.get("iob")
                if isinstance(it, (int, float)):
                    total_iob = float(it)
        # Extract sensor/site age recursively
        def _extract_ages(obj, depth=0):
            nonlocal sensor_age_min
            if depth > 6:
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    kl = str(k).lower()
                    if isinstance(v, str):
                        try:
                            v_num = float(v.replace(',', '.'))
                            v = v_num
                        except Exception:
                            pass
                    if sensor_age_min is None and kl in ("sage", "sensorage") and isinstance(v, (int, float)):
                        sensor_age_min = int(v)
                        if DEBUG_AGE:
                            print(f"[AGE DEBUG] Widget found sensor age {sensor_age_min} via key '{k}'")
                    if isinstance(v, (dict, list)):
                        _extract_ages(v, depth+1)
            elif isinstance(obj, list):
                for v in obj:
                    _extract_ages(v, depth+1)
        try:
            _extract_ages(latest)
        except Exception:
            pass
        if DEBUG_AGE and sensor_age_min is None:
            print("[AGE DEBUG] Widget: No sensor age field found in latest devicestatus")
    return cob_g, bolus_iob, basal_iob, total_iob, pump_batt, reservoir, uploader_batt, sensor_age_min


def _fetch_latest_sensor_change_ts() -> Optional[dt.datetime]:
    """Return UTC datetime of most recent 'Sensor Change' (or similar) Nightscout treatment.
    Uses a dedicated query not bounded by the widget chart window. Returns None if not found."""
    try:
        data = ns_get_json("/api/v1/treatments.json", {"find[eventType]": "Sensor Change", "count": 1})
        if not data:
            data = ns_get_json(
                "/api/v1/treatments.json",
                {"find[eventType][$regex]": "Sensor Change|Sensor Start", "count": 3},
            )
        if not data:
            return None
        rec = data[0]
        ts_raw = rec.get("created_at") or rec.get("timestamp") or rec.get("dateString")
        ts = None
        if ts_raw:
            try:
                ts = dateutil.parser.isoparse(ts_raw)
            except Exception:
                ts = None
        if ts is None:
            ms = rec.get("mills") or rec.get("date")
            if isinstance(ms, (int, float)):
                ts = dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc)
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return ts.astimezone(dt.timezone.utc)
    except Exception:
        return None


def direction_arrow(dir_text: str) -> str:
    d = (dir_text or "").lower()
    return {
        "flat": "→",
        "fortyfiveup": "↗",
        "fortyfivedown": "↘",
        "singleup": "↑",
        "singledown": "↓",
        "doubleup": "↑↑",
        "doubledown": "↓↓",
        "none": "",
    }.get(d, "")


def _fallback_bolus_iob(treatments: List[Dict[str, Any]]) -> float:
    """Rudimentary bolus IOB estimate using exponential decay, similar to dashboard fallback.
    DIA 5h, half-life 60 min. Ignores basal insulin.
    """
    DIA_MIN = 300  # minutes
    T_HALF = 60.0  # minutes

    def _units(t: Dict[str, Any]) -> Optional[float]:
        for k in ("insulin", "insulinInUnits", "amount", "units", "value"):
            v = t.get(k)
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                try:
                    return float(v.replace(",", "."))
                except ValueError:
                    pass
        if isinstance(t.get("bolus"), dict):
            for k in ("normal", "extended", "immediate"):
                v = t["bolus"].get(k)
                if isinstance(v, (int, float)):
                    return float(v)
        return None

    now_utc = dt.datetime.now(dt.timezone.utc)
    total = 0.0
    for t in treatments or []:
        u = _units(t)
        if not u or u <= 0:
            continue
        ts_raw = t.get("created_at") or t.get("timestamp") or t.get("dateString")
        if ts_raw:
            try:
                ts = dateutil.parser.isoparse(ts_raw)
            except Exception:
                ts = None
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
        else:
            ms = t.get("mills") or t.get("date")
            ts = dt.datetime.fromtimestamp(ms/1000.0, tz=dt.timezone.utc) if isinstance(ms, (int, float)) else None
        if ts is None:
            continue
        age_min = (now_utc - ts).total_seconds() / 60.0
        if 0 <= age_min <= DIA_MIN:
            rem = 0.5 ** (age_min / T_HALF)
            total += u * rem
    return total


# --- Tkinter widget --------------------------------------------------
class CGMWidget:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CGM Widget")
        # Always-on-top default; can be toggled from the context menu
        self._always_on_top = True
        self.root.attributes("-topmost", self._always_on_top)
        # small undecorated window; allow drag
        self.root.overrideredirect(True)
        # Dark + slightly transparent
        self.bg_color = "#121212"
        self.fg_color = "#e0e0e0"
        self.root.configure(bg=self.bg_color)
        try:
            # Overall window transparency (0..1)
            self.root.attributes("-alpha", 0.9)
        except Exception:
            pass

        # drag support
        self._drag_data = {"x": 0, "y": 0}
        self.root.bind("<ButtonPress-1>", self._start_move)
        self.root.bind("<B1-Motion>", self._on_move)

        # context menu (built dynamically depending on Dexcom/Nightscout mode)
        self.menu = None
        self._topmost_var = tk.BooleanVar(value=self._always_on_top)

        # content (BG + Subline + Pump + TempBasal) with consistent spacing
        self.lbl_bg = tk.Label(
            self.root,
            text="– mmol/L",
            font=("Segoe UI", 16, "bold"),
            bg=self.bg_color,
            fg=self.fg_color,
            anchor="w",
            justify="left",
            bd=0,
            highlightthickness=0,
        )
        self.lbl_bg.pack(padx=10, pady=(0, 0), anchor="w")
        # Tiny age label (last update age). Always visible in all modes.
        self.lbl_age = tk.Label(
            self.root,
            text="",
            font=("Segoe UI", 8),
            bg=self.bg_color,
            fg="#9e9e9e",
            anchor="w",
            justify="left",
            bd=0,
            highlightthickness=0,
        )
        self._lbl_age_pack = {"padx": 10, "pady": (0, 2), "anchor": "w"}
        self.lbl_age.pack(**self._lbl_age_pack)
        # Split Subline into two labels to control spacing of each line explicitly
        self.lbl_sub1 = tk.Label(
            self.root,
            text="",
            font=("Segoe UI", 9),
            bg=self.bg_color,
            fg=self.fg_color,
            anchor="w",
            justify="left",
            bd=0,
            highlightthickness=0,
        )
        # store pack options so we can hide/show in modes
        self._lbl_sub1_pack = {"padx": 10, "pady": (0, 0), "anchor": "w"}
        self.lbl_sub1.pack(**self._lbl_sub1_pack)
        self.lbl_sub2 = tk.Label(
            self.root,
            text="",
            font=("Segoe UI", 9),
            bg=self.bg_color,
            fg=self.fg_color,
            anchor="w",
            justify="left",
            bd=0,
            highlightthickness=0,
        )
        self._lbl_sub2_pack = {"padx": 10, "pady": (0, 0), "anchor": "w"}
        self.lbl_sub2.pack(**self._lbl_sub2_pack)
        self.lbl_pump = tk.Label(
            self.root,
            text="",
            font=("Segoe UI", 9),
            bg=self.bg_color,
            fg=self.fg_color,
            anchor="w",
            justify="left",
            bd=0,
            highlightthickness=0,
        )
        self._lbl_pump_pack = {"padx": 10, "pady": (0, 0), "anchor": "w"}
        self.lbl_pump.pack(**self._lbl_pump_pack)
        self.lbl_temp = tk.Label(
            self.root,
            text="",
            font=("Segoe UI", 9),
            bg=self.bg_color,
            fg=self.fg_color,
            anchor="w",
            justify="left",
            bd=0,
            highlightthickness=0,
        )
        self._lbl_temp_pack = {"padx": 10, "pady": (0, 0), "anchor": "w"}
        self.lbl_temp.pack(**self._lbl_temp_pack)

        # Chart area (Matplotlib inside Tk)
        self.chart_frame = tk.Frame(self.root, bg=self.bg_color, bd=0, highlightthickness=0)
        # Store pack options to restore later (minimized top padding)
        self._chart_pack = {"fill": "x", "expand": False, "padx": 8, "pady": (0, 4)}
        self.chart_frame.pack(**self._chart_pack)
        # Smaller, narrower figure (width reduced ~1/3); configurable via WIDGET_FIG_SCALE (1.0 default)
        try:
            _fig_scale = float(os.getenv("WIDGET_FIG_SCALE", "1.0"))
        except Exception:
            _fig_scale = 1.0
        base_w, base_h = 3.2, 2.0  # compromise width (previous 2.8 was too tight)
        self.fig = Figure(figsize=(base_w * _fig_scale, base_h), dpi=100, constrained_layout=False)
        self.fig.set_facecolor(self.bg_color)
        gs = self.fig.add_gridspec(2, 1, height_ratios=[3, 2])
        self.ax1 = self.fig.add_subplot(gs[0, 0])  # BG
        self.ax2 = self.fig.add_subplot(gs[1, 0], sharex=self.ax1)  # Basal
        self._style_axes_dark(self.ax1)
        self._style_axes_dark(self.ax2)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.chart_frame)
        self.canvas.get_tk_widget().configure(bg=self.bg_color, highlightthickness=0, bd=0)
        self.canvas.get_tk_widget().pack(fill="x", expand=False)
        self._plot_lock = threading.Lock()

        # Start updates
        self.low, self.high = DEFAULT_LOW, DEFAULT_HIGH
        self.refresh_profile()
        self.schedule_update(0)

        # compact mode flag
        self._compact = False
        # minimal mode flag (only BG + trend)
        self._minimal = False
        # dexcom-only flag
        self._dexcom_only = bool(os.getenv("USE_DEXCOM", "").strip().lower() in ("1", "true", "yes", "on"))
        # If dexcom-only, force minimal and hide dashboard menu
        if self._dexcom_only:
            self._minimal = True
            try:
                # Hide chart frame immediately
                self.chart_frame.pack_forget()
            except Exception:
                pass
            if DEBUG_DEXCOM:
                try:
                    u = os.getenv("DEXCOM_USERNAME")
                    p = os.getenv("DEXCOM_PASSWORD")
                    r = os.getenv("DEXCOM_REGION")
                    print(f"[DEXCOM] Startup dexcom-only mode. USER={'set' if u else 'missing'} PASS={'set' if p else 'missing'} REGION={r!r}")
                except Exception:
                    pass
        # Build context menu last (depends on self._dexcom_only)
        self._build_menu()

    def _build_menu(self):
        # (Re)build the right-click context menu according to current mode
        try:
            if self.menu is not None:
                self.menu.destroy()
        except Exception:
            pass
        self.menu = tk.Menu(self.root, tearoff=0, bg=self.bg_color, fg=self.fg_color,
                            activebackground="#333333", activeforeground="#ffffff")
        self.menu.add_command(label="Jetzt aktualisieren", command=lambda: self.schedule_update(0))
        # Toggle always-on-top (available in all modes)
        self.menu.add_checkbutton(label="Immer im Vordergrund", variable=self._topmost_var, command=self._toggle_topmost)
        if not self._dexcom_only:
            self.menu.add_command(label="Diagrammodus", command=self._enable_diagram_mode)
            self.menu.add_command(label="Kompaktmodus umschalten", command=self._toggle_compact)
            self.menu.add_command(label="Minimalmodus umschalten", command=self._toggle_minimal)
            self.menu.add_command(label="Einstellungen…", command=self._open_settings)
            self.menu.add_command(label="Dashboard öffnen", command=self._open_dashboard)
        else:
            # Dexcom-only: keep only settings and exit; do not expose compact/minimal/dashboard
            self.menu.add_command(label="Einstellungen…", command=self._open_settings)
        self.menu.add_separator()
        self.menu.add_command(label="Beenden", command=self.root.destroy)
        # Rebind right click to show this menu
        self.root.bind("<Button-3>", self._show_menu)

    def _toggle_topmost(self):
        # Apply the checkbutton state to the window attribute
        self._always_on_top = bool(self._topmost_var.get())
        try:
            self.root.attributes("-topmost", self._always_on_top)
        except Exception:
            pass

    def _start_move(self, event):
        self._drag_data["x"] = event.x
        self._drag_data["y"] = event.y

    def _on_move(self, event):
        x = event.x_root - self._drag_data["x"]
        y = event.y_root - self._drag_data["y"]
        self.root.geometry(f"+{x}+{y}")

    def _enable_diagram_mode(self):
        """Enable full detailed diagram mode (show chart and all info lines)."""
        if self._dexcom_only:
            messagebox.showinfo("Nicht verfügbar", "Diagrammodus ist im Dexcom-Only Modus deaktiviert.")
            return
        self._minimal = False
        self._compact = False
        # Show all labels
        try:
            self.lbl_sub1.pack(**self._lbl_sub1_pack)
            self.lbl_sub2.pack(**self._lbl_sub2_pack)
            self.lbl_pump.pack(**self._lbl_pump_pack)
            self.lbl_temp.pack(**self._lbl_temp_pack)
        except Exception:
            pass
        # Show chart
        try:
            self.chart_frame.pack(**self._chart_pack)
        except Exception:
            pass
        self.schedule_update(0)

    def schedule_update(self, delay_ms: int = 60000):
        self.root.after(delay_ms, self.update_async)

    def refresh_profile(self):
        try:
            self.low, self.high = fetch_profile_range()
        except Exception:
            self.low, self.high = DEFAULT_LOW, DEFAULT_HIGH

    def update_async(self):
        threading.Thread(target=self.update_data, daemon=True).start()
        # schedule next refresh in 60s
        self.schedule_update(60000)

    def update_data(self):
        if not BASE and not self._dexcom_only:
            self._set_status("NIGHTSCOUT_URL fehlt", sub="Bitte .env setzen")
            return
        if (not (TOKEN or SECRET_SHA1)) and (not self._dexcom_only):
            self._set_status(
                "Nightscout Auth fehlt",
                sub=(
                    "Bitte NS_TOKEN (empfohlen) oder NS_API_SECRET in .env setzen.\n"
                    "Token: Nightscout → Admin/Settings → Access Tokens (Scope: API Full)."
                ),
            )
            return
        try:
            # Pre-initialize all metric variables to avoid UnboundLocalError in Dexcom-only mode
            mmol = None; arrow = ""; ts = None
            cob_g = bolus_iob = basal_iob = total_iob = None
            pump_batt = reservoir = uploader_batt = None
            temp_text = None
            # Text metrics depending on data source
            if self._dexcom_only:
                if DEBUG_DEXCOM:
                    print("[DEXCOM] update_data(): fetching current glucose ...")
                try:
                    mmol, arrow, ts = self._fetch_dexcom_bg()
                    if DEBUG_DEXCOM:
                        print(f"[DEXCOM] Success value={mmol} mmol arrow={arrow!r} ts={ts}")
                except Exception as de:
                    if DEBUG_DEXCOM:
                        print(f"[DEXCOM] ERROR {de}")
                    # Present clearer status for Dexcom-only failures
                    self._set_status("Dexcom Fehler", sub=str(de))
                    return
            else:
                entry = latest_entry()
                status = latest_devicestatus()
                cob_g, bolus_iob, basal_iob, total_iob, pump_batt, reservoir, uploader_batt, sensor_age_min = metrics_from_status(status)
                if sensor_age_min is None:
                    if DEBUG_AGE:
                        print("[AGE DEBUG] Widget: No sensor age in devicestatus; dedicated Sensor Change lookup...")
                    sc_ts = _fetch_latest_sensor_change_ts()
                    if sc_ts is not None:
                        sensor_age_min = int((dt.datetime.now(dt.timezone.utc) - sc_ts).total_seconds() / 60.0)
                        if DEBUG_AGE:
                            print(f"[AGE DEBUG] Widget: Derived sensor age {sensor_age_min} min from Sensor Change at {sc_ts.isoformat()}")
                    elif DEBUG_AGE:
                        print("[AGE DEBUG] Widget: No Sensor Change treatment found via dedicated lookup")
                # Ensure values present for display lines
                if bolus_iob is None or basal_iob is None or total_iob is None or cob_g is None:
                    # lightweight fallback using treatments
                    try:
                        since_iso = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=8)).isoformat()
                        tr = ns_get_json("/api/v1/treatments.json", {"find[created_at][$gte]": since_iso, "count": 1000})
                    except Exception:
                        tr = []
                    if bolus_iob is None:
                        try:
                            bolus_iob = _fallback_bolus_iob(tr)
                        except Exception:
                            bolus_iob = None
                    if basal_iob is None:
                        basal_iob = 0.0
                    if total_iob is None and (isinstance(bolus_iob, (int, float)) or isinstance(basal_iob, (int, float))):
                        total_iob = (bolus_iob or 0.0) + (basal_iob or 0.0)
                    if cob_g is None:
                        cob_val = None
                        for t in tr or []:
                            v = t.get("carbs") or t.get("carb_input")
                            if isinstance(v, (int, float)) and v > 0:
                                cob_val = (cob_val or 0.0) + float(v)
                        cob_g = cob_val
                # Active temp basal (from treatments)
                temp_text = self._current_temp_basal_text()
                mmol = entry.get("mmol")
                arrow = direction_arrow(entry.get("direction"))
                ts = entry.get("time")
            # Prefer NS arrow or Dexcom trend; optionally derive later when charts are available
            tstr = dt.datetime.now().astimezone().strftime("%H:%M") if ts is None else ts.astimezone().strftime("%H:%M")

            # color by target range
            color = "#8bc34a" if (isinstance(mmol, (int, float)) and self.low <= mmol <= self.high) else ("#ef5350" if isinstance(mmol, (int, float)) and mmol > self.high else "#ab47bc")

            bg_text = f"{mmol:.1f} mmol/L {arrow}" if isinstance(mmol, (int, float)) else "– mmol/L"
            # Ensure COB always has a value in Nightscout mode BEFORE assembling text lines
            if not self._dexcom_only:
                try:
                    cob_g = float(cob_g) if isinstance(cob_g, (int, float, str)) else 0.0
                except Exception:
                    cob_g = 0.0
            # Build two-line subtext: line1 = Bolus-IOB | Basal-IOB, line2 = Gesamt-IOB | COB
            line1_parts = []
            line2_parts = []
            if isinstance(bolus_iob, (int, float)):
                line1_parts.append(f"Bolus-IOB {bolus_iob:.2f} IE")
            if isinstance(basal_iob, (int, float)):
                line1_parts.append(f"Basal-IOB {basal_iob:.2f} IE")
            if isinstance(bolus_iob, (int, float)) or isinstance(basal_iob, (int, float)):
                _ti = (bolus_iob or 0.0) + (basal_iob or 0.0)
                line2_parts.append(f"Gesamt-IOB {_ti:.2f} IE")
            elif isinstance(total_iob, (int, float)):
                line2_parts.append(f"Gesamt-IOB {total_iob:.2f} IE")
            if isinstance(cob_g, (int, float)):
                line2_parts.append(f"COB {cob_g:.0f} g")
            line1 = " | ".join(line1_parts)
            line2 = " | ".join(line2_parts)
            sub_text = line1 if line2 == "" else (f"{line1}\n{line2}" if line1 else line2)

            pump_parts = []
            if pump_batt:
                pump_parts.append(f"Pumpe {pump_batt}")
            if reservoir:
                pump_parts.append(f"Res {reservoir}")
            if uploader_batt:
                pump_parts.append(f"Uploader {uploader_batt}")
            pump_text = " | ".join(pump_parts)

            # Prepare chart data (6h window) — skip in Dexcom-only
            chart_payload = None
            try:
                end = dt.datetime.now(dt.timezone.utc)
                start = end - dt.timedelta(minutes=WINDOW_MIN)
                if _DASHBOARD_AVAILABLE and (not self._dexcom_only):
                    entries_df = ds_fetch_entries(start)
                    treatments = ns_get_json("/api/v1/treatments.json", {"find[created_at][$gte]": start.isoformat(), "count": 1000})
                    profile = ds_fetch_profile()
                    plan = ds_plan_basal_from_profile(profile)
                    bolus_big, bolus_mini, carbs, temps = ds_split_events(treatments)
                    basal_series = ds_build_basal_series(plan, temps, start, end)
                    low_t, high_t = ds_target_range_from_profile(profile)
                    # If arrow not set, try compute from slope
                    if not arrow:
                        arrow = ds_direction_arrow_from_text(entry.get("direction")) or ds_compute_arrow_from_slope(entries_df)
                    chart_payload = (entries_df, (low_t, high_t), basal_series, bolus_big, bolus_mini, carbs, temps, pump_batt, reservoir, uploader_batt)
                    # Derive temp basal text from these temps if not yet set
                    if temp_text is None and temps:
                        now_local = dt.datetime.now().astimezone()
                        for tb in temps:
                            try:
                                t_start = tb.get("start")
                                dur = int(tb.get("duration", 0)) if isinstance(tb.get("duration"), (int, float)) else 0
                                t_end = (t_start + dt.timedelta(minutes=dur)) if (t_start and isinstance(t_start, dt.datetime)) else None
                                if t_start and ((t_end and (t_start <= now_local < t_end)) or (dur == 0 and t_start <= now_local)):
                                    abs_v = tb.get("absolute")
                                    pct_v = tb.get("percent")
                                    rem = int(max(0, (t_end - now_local).total_seconds() // 60)) if t_end else 0
                                    if isinstance(abs_v, (int, float)):
                                        temp_text = f"Temp {float(abs_v):.2f} U/h" + (f" · {rem} min" if rem else "")
                                        break
                                    if isinstance(pct_v, (int, float)):
                                        sign = "+" if pct_v >= 0 else ""
                                        temp_text = f"Temp {sign}{int(pct_v)}%" + (f" · {rem} min" if rem else "")
                                        break
                            except Exception:
                                continue
                    # Secondary fallback: infer temp from basal_series if last samples differ from plan
                    if temp_text is None and basal_series is not None and not basal_series.empty:
                        try:
                            tail = basal_series.tail(5)
                            if (tail["is_temp"] > 0).any():
                                now_local = dt.datetime.now().astimezone()
                                # find last index with temp active
                                idx = tail[tail["is_temp"] > 0].index[-1]
                                act = float(basal_series.loc[idx, "actual_u_per_h"]) if not pd.isna(basal_series.loc[idx, "actual_u_per_h"]) else None
                                planv = float(basal_series.loc[idx, "plan_u_per_h"]) if not pd.isna(basal_series.loc[idx, "plan_u_per_h"]) else None
                                if act is not None and planv is not None:
                                    # Prefer absolute if explicitly different, else percent
                                    if planv != 0:
                                        pct = (act / planv) * 100.0
                                        delta_pct = int(round(pct - 100.0))
                                        sign = "+" if delta_pct >= 0 else ""
                                        temp_text = f"Temp {sign}{delta_pct}%"
                                    else:
                                        temp_text = f"Temp {act:.2f} U/h"
                        except Exception:
                            pass
            except Exception:
                chart_payload = None

            # COB already normalized above prior to text assembly

            # Compute 'age' string from timestamp (shown in all modes)
            age_text = self._format_age(ts)
            # Append sensor age if available
            if not self._dexcom_only and 'sensor_age_min' in locals() and isinstance(sensor_age_min, int):
                try:
                    d = sensor_age_min // 1440
                    h = (sensor_age_min % 1440) // 60
                    if d >= 1:
                        sensor_frag = f"Sensor {d}d {h}h"
                    else:
                        sensor_frag = f"Sensor {h}h"
                    age_text = (age_text + " · " if age_text else "") + sensor_frag
                except Exception:
                    pass
            # --- Delta (BG Änderungsrate) immer hinter Age anzeigen (Nightscout Modus) ---
            delta_fragment = None
            try:
                if not self._dexcom_only:
                    # Hole die letzten 2 Einträge direkt (schnell, count=2)
                    entries2 = ns_get_json("/api/v1/entries.json", {"count": 2})
                    if entries2 and len(entries2) >= 2:
                        cur_e, prev_e = entries2[0], entries2[1]
                        # Priorisierte Quellen für delta
                        raw_delta = cur_e.get("delta") or cur_e.get("trendDelta") or cur_e.get("tick")
                        d_val = None
                        if isinstance(raw_delta, (int, float)):
                            d_val = float(raw_delta)
                        elif isinstance(raw_delta, str):
                            try:
                                d_val = float(raw_delta.replace("+", ""))
                            except Exception:
                                d_val = None
                        if d_val is None:
                            # Fallback selbst berechnen
                            v_cur = cur_e.get("sgv") or cur_e.get("glucose") or cur_e.get("mbg")
                            v_prev = prev_e.get("sgv") or prev_e.get("glucose") or prev_e.get("mbg")
                            if isinstance(v_cur, (int, float)) and isinstance(v_prev, (int, float)):
                                d_val = float(v_cur) - float(v_prev)
                        if d_val is not None:
                            d_mmol = d_val / 18.01559
                            # Klein und mit Vorzeichen, auf eine Nachkommastelle gerundet
                            delta_fragment = f"Δ {d_mmol:+.1f}"
                else:
                    # Dexcom-only: pydexcom liefert Rate nicht direkt konsistent; einfache Ableitung aus letzter Liste wäre Zusatz-Aufwand
                    pass
            except Exception:
                delta_fragment = None
            if delta_fragment:
                age_text = (age_text + " · " if age_text else "") + delta_fragment

            # update UI on main thread (text + chart)
            def apply():
                self.lbl_bg.config(text=bg_text, fg=color)
                self.lbl_age.config(text=age_text)
                # Set two separate subline labels to control spacing
                self.lbl_sub1.config(text=line1)
                self.lbl_sub2.config(text=line2)
                self.lbl_pump.config(text=pump_text)
                self.lbl_temp.config(text=temp_text or "")
                # Render chart if data available
                if chart_payload is not None and (not self._compact) and (not self._minimal) and (not self._dexcom_only):
                    self._render_chart(*chart_payload)
                self.root.update_idletasks()

            self.root.after(0, apply)
        except Exception as e:
            # Improve message for common auth error
            msg = str(e)
            if "401" in msg or "Unauthorized" in msg:
                msg += "\nHinweis: Nightscout-Auth fehlt/ist ungültig. Setze NS_TOKEN (empfohlen) oder NS_API_SECRET in .env."
            if self._dexcom_only:
                self._set_status("Dexcom Fehler", sub=msg)
            else:
                self._set_status("Verbindung fehlt", sub=msg)

    def _fetch_dexcom_bg(self) -> Tuple[Optional[float], str, Optional[dt.datetime]]:
        """Fetch Dexcom BG via pydexcom using env DEXCOM_USERNAME/PASSWORD/REGION.
        Returns (mmol, arrow, timestamp) and raises informative error on failure.
        """
        try:
            from pydexcom import Dexcom
        except Exception as e:
            raise RuntimeError("pydexcom nicht installiert (requirements).") from e
        user = os.getenv("DEXCOM_USERNAME"); pwd = os.getenv("DEXCOM_PASSWORD"); region = os.getenv("DEXCOM_REGION")
        if not user or not pwd:
            raise RuntimeError("Dexcom Zugangsdaten fehlen: DEXCOM_USERNAME/DEXCOM_PASSWORD in .env setzen.")
        # Simplified region handling: only allow explicit 'US', 'OUS', 'JP' (case-insensitive).
        region_input = (region or '').strip().upper()
        valid_regions = {"US", "OUS", "JP"}
        attempt_regions: List[Optional[str]] = []
        if region_input in valid_regions:
            attempt_regions.append(region_input.lower())  # pydexcom commonly expects lowercase
        else:
            # If not provided or invalid → try common order (OUS then US)
            attempt_regions.extend(["ous", "us"])
        # We do NOT attempt without region anymore to avoid ambiguous server selection.
        dx = None
        last_err: Optional[Exception] = None
        if DEBUG_DEXCOM:
            print(f"[DEXCOM] Attempt login user={'*' * len(user)} attempts={attempt_regions}")
        for reg in attempt_regions:
            kwargs = {"username": user, "password": pwd}
            if reg:
                kwargs["region"] = reg
            try:
                if DEBUG_DEXCOM:
                    dbg_kwargs = {k: (v if k != 'password' else '***') for k, v in kwargs.items()}
                    print(f"[DEXCOM] Trying constructor {dbg_kwargs}")
                dx = Dexcom(**kwargs)
                if DEBUG_DEXCOM:
                    print(f"[DEXCOM] Constructor success with region={reg}")
                break
            except Exception as e:
                last_err = e
                if DEBUG_DEXCOM:
                    print(f"[DEXCOM] Constructor failed (region={reg}) -> {e}")
                continue
        if dx is None:
            hint = "Gültige Werte für DEXCOM_REGION sind: OUS, JP oder US. Beispiel: DEXCOM_REGION=OUS"
            raise RuntimeError(f"Dexcom Anmeldung fehlgeschlagen: {last_err}\n{hint}")
        bg = dx.get_current_glucose_reading()
        if not bg:
            raise RuntimeError("Keine Dexcom-Daten verfügbar.")
        mmol = mgdl_to_mmol(bg.value)
        # Try to map trend to arrow across different pydexcom versions
        arrow = ''
        desc_any = getattr(bg, 'trend_description', None) or getattr(bg, 'trend_direction', None)
        if isinstance(desc_any, str) and desc_any:
            desc = desc_any.strip().lower().replace('_', ' ').replace('-', ' ')
            mapping = {
                'flat': '→', 'forty five up': '↗', 'forty five down': '↘',
                'single up': '↑', 'single down': '↓', 'double up': '↑↑', 'double down': '↓↓'
            }
            arrow = mapping.get(desc, '')
        if not arrow:
            # Some versions expose trend as integer code (1..7)
            code = getattr(bg, 'trend', None)
            int_map = {
                4: '→', 3: '↗', 5: '↘', 2: '↑', 6: '↓', 1: '↑↑', 7: '↓↓'
            }
            if isinstance(code, int):
                arrow = int_map.get(code, '')
        ts = getattr(bg, 'datetime', None)
        if DEBUG_DEXCOM:
            print(f"[DEXCOM] Reading raw value={bg.value if hasattr(bg,'value') else '??'} mg/dL => {mmol} mmol arrow={arrow!r} ts={ts}")
        return mmol, arrow, ts

    def _set_status(self, text: str, sub: str = ""):
        def apply():
            self.lbl_bg.config(text=text, fg=self.fg_color)
            self.lbl_age.config(text="")
            # Split incoming 'sub' by first newline into up to two parts
            if sub:
                parts = sub.split("\n", 1)
                self.lbl_sub1.config(text=parts[0])
                self.lbl_sub2.config(text=(parts[1] if len(parts) > 1 else ""))
            else:
                self.lbl_sub1.config(text="")
                self.lbl_sub2.config(text="")
            self.lbl_pump.config(text="")
            self.lbl_temp.config(text="")
            self.root.update_idletasks()
        self.root.after(0, apply)

    def _current_temp_basal_text(self) -> Optional[str]:
        """Return a compact string for the currently active temp basal, if any.
        Examples:
          "Temp 0.60 U/h · 22 min" or "Temp +30% · 12 min"
        """
        try:
            # Look back 8 hours to catch recent temps
            since_iso = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=8)).isoformat()
            tr = ns_get_json("/api/v1/treatments.json", {"find[created_at][$gte]": since_iso, "count": 1000})
            now_local = dt.datetime.now().astimezone()
            active = None
            for t in tr:
                et = (t.get("eventType") or "").lower()
                if et not in ("temp basal", "temporary basal", "temp basal start", "temp basal end"):
                    continue
                ts_raw = t.get("created_at") or t.get("timestamp") or t.get("dateString")
                if ts_raw:
                    try:
                        ts = dateutil.parser.isoparse(ts_raw)
                    except Exception:
                        ts = None
                    if ts and ts.tzinfo is None:
                        ts = ts.replace(tzinfo=NOW_TZ)
                else:
                    ms = t.get("mills") or t.get("date")
                    ts = dt.datetime.fromtimestamp(ms / 1000.0, tz=NOW_TZ) if isinstance(ms, (int, float)) else None
                if ts is None:
                    continue
                start = ts.astimezone()
                dur = int(t.get("duration", 0)) if isinstance(t.get("duration"), (int, float)) else 0
                end = start + dt.timedelta(minutes=dur)
                if start <= now_local < end or (dur == 0 and et in ("temp basal", "temp basal start")):
                    percent = t.get("percent")
                    absolute = t.get("absolute")
                    rem = int(max(0, (end - now_local).total_seconds() // 60)) if dur > 0 else 0
                    if isinstance(absolute, (int, float)):
                        return f"Temp {float(absolute):.2f} U/h" + (f" · {rem} min" if rem else "")
                    if isinstance(percent, (int, float)):
                        sign = "+" if percent >= 0 else ""
                        return f"Temp {sign}{int(percent)}%" + (f" · {rem} min" if rem else "")
                    active = True
            return "Temp aktiv" if active else None
        except Exception:
            return None

    # ----------------- Chart rendering -----------------
    def _style_axes_dark(self, ax):
        ax.set_facecolor("#0f0f0f")
        ax.grid(True, which="both", axis="both", alpha=0.28, color="#444")
        for spine in ax.spines.values():
            spine.set_color("#555")
        ax.tick_params(colors="#cfcfcf", labelsize=7, pad=2)
        # Remove axis labels completely to save vertical space
        ax.set_xlabel("")
        ax.set_ylabel("")

    def _fmt_hhmm(self, x_any) -> str:
        try:
            return mdates.num2date(float(x_any)).strftime("%H:%M")
        except Exception:
            try:
                return pd.Timestamp(x_any).strftime("%H:%M")
            except Exception:
                return ""

    def _annotate_events_on_bg(self, ax, entries_df: pd.DataFrame, bolus_list: List[Dict[str, Any]], carbs_list: List[Dict[str, Any]]):
        """Annotate bolus and carbs with grouping rules:
        - At same timestamp: bolus label above, carbs below bolus (stacked vertically)
        - If top would clip, both go below (bolus first row below, carbs second below)
        - Dynamic y headroom extended only if needed.
        """
        if entries_df is None or entries_df.empty:
            return
        try:
            # Reset index; index name may be lost ('time') after reassigning DatetimeIndex -> handle generically
            bg_df = entries_df.reset_index()
            if "time" not in bg_df.columns:
                # assume first column holds timestamps
                first_col = bg_df.columns[0]
                bg_df = bg_df.rename(columns={first_col: "time"})
            bg_df = bg_df[["time", "mmol"]]
        except Exception:
            return
        groups: Dict[pd.Timestamp, Dict[str, List[Dict[str, Any]]]] = {}
        for b in bolus_list or []:
            ts = b.get("time")
            if isinstance(ts, dt.datetime):
                groups.setdefault(ts, {}).setdefault("bolus", []).append(b)
        for c in carbs_list or []:
            ts = c.get("time")
            if isinstance(ts, dt.datetime):
                groups.setdefault(ts, {}).setdefault("carb", []).append(c)
        if not groups:
            return
        if _DEBUG_TIME:
            try:
                print(f"[EVENT GROUPS] keys={len(groups)} sample={list(groups.keys())[:3]}")
            except Exception:
                pass
        # Merge glucose to find base y values
        # We'll annotate at the glucose value nearest/at timestamp; if none, skip group
        y_max_extra = None
        for ts in sorted(groups.keys()):
            row_glu = bg_df.loc[bg_df["time"] <= ts].tail(1)
            if row_glu.empty:
                continue
            y_val = float(row_glu.iloc[0]["mmol"])
            bolus_entries = groups[ts].get("bolus", [])
            carb_entries = groups[ts].get("carb", [])
            # Decide placement: assume bolus above (offset +8), carbs below (-10). If no room above, both below stacked.
            place_above = True
            try:
                cur_ymin, cur_ymax = ax.get_ylim()
                if (y_val + 0.9) > cur_ymax:
                    place_above = False
            except Exception:
                pass
            annotations_plan = []
            if place_above:
                for b in bolus_entries:
                    annotations_plan.append((b, 8, True))
                for idx, c in enumerate(carb_entries):
                    # stack carbs downward if multiple: -10, -22, -34 ...
                    annotations_plan.append((c, -10 - idx * 12, False))
            else:
                # All below: bolus still flagged as bolus (is_bolus=True) but placed below
                for idx, b in enumerate(bolus_entries):
                    annotations_plan.append((b, -10 - idx * 12, True))
                start_idx = len(bolus_entries)
                for j, c in enumerate(carb_entries):
                    annotations_plan.append((c, -10 - (start_idx + j) * 12, False))
            for item, off, is_bolus in annotations_plan:
                try:
                    units_v = item.get('units') if isinstance(item, dict) else None
                    grams_v = item.get('grams') if isinstance(item, dict) else None
                    # expand bolus unit key search
                    if units_v is None and isinstance(item, dict):
                        for alt in ('insulin','amount','value'):
                            v = item.get(alt)
                            if isinstance(v,(int,float)):
                                units_v = v; break
                    if grams_v is None and isinstance(item, dict):
                        # fallback alternate keys occasionally used
                        for alt in ('carbs','carb_input','amount','value'):
                            v = item.get(alt)
                            if isinstance(v, (int,float)):
                                grams_v = v; break
                    label = "?"
                    if is_bolus and isinstance(units_v, (int,float)):
                        label = f"B {float(units_v):.1f} IE"
                    elif (not is_bolus) and isinstance(grams_v, (int,float)):
                        label = f"C {int(round(grams_v))}g"
                    ann = ax.annotate(
                        label,
                        xy=(ts, y_val),
                        xytext=(0, off),
                        textcoords="offset points",
                        ha="center",
                        va="bottom" if off > 0 else "top",
                        fontsize=7,
                        fontweight="bold",
                        color="#f0f0f0",
                        zorder=6,
                        bbox=dict(
                            boxstyle="round,pad=0.32",
                            facecolor="#111111",
                            alpha=0.92,
                            edgecolor=("#d62728" if is_bolus else "#2ca02c"),
                            linewidth=0.8,
                        ),
                        arrowprops=dict(arrowstyle="->", color=("#d62728" if is_bolus else "#2ca02c"), lw=0.6, alpha=0.9, shrinkA=0, shrinkB=2),
                        annotation_clip=False,
                    )
                    try:
                        ann.get_bbox_patch().set_path_effects([patheffects.withSimplePatchShadow(offset=(1, -1), shadow_rgbFace=(0, 0, 0, 0.45))])
                    except Exception:
                        pass
                except Exception as e:
                    if _DEBUG_TIME:
                        print(f"[EVENT LABEL ERROR] {e} item={item}")
                    continue
                # Track needed headroom only for above annotations
                if off > 0:
                    top_needed = y_val + 0.8
                    y_max_extra = top_needed if y_max_extra is None else max(y_max_extra, top_needed)
        if y_max_extra is not None:
            try:
                cur_ymin, cur_ymax = ax.get_ylim()
                if y_max_extra > cur_ymax:
                    ax.set_ylim(cur_ymin, y_max_extra)
            except Exception:
                pass

    def _render_chart(self, entries: pd.DataFrame, target_range: Tuple[float, float], basal_series: pd.DataFrame,
                      bolus_big: List[Dict[str, Any]], bolus_mini: List[Dict[str, Any]], carbs: List[Dict[str, Any]], temps: List[Dict[str, Any]],
                      pump_batt: Optional[str], reservoir: Optional[str], uploader_batt: Optional[str]):
        if self._plot_lock.locked():
            return
        with self._plot_lock:
            self.ax1.clear(); self.ax2.clear()
            self._style_axes_dark(self.ax1); self._style_axes_dark(self.ax2)

            # Force fixed window: last WINDOW_MIN minutes ending at 'now'
            now_local = dt.datetime.now().astimezone()
            window_end = now_local  # ALWAYS anchor to current clock time
            if entries is not None and not entries.empty:
                # Normalize index
                try:
                    if entries.index.tzinfo is None:
                        if _ASSUME_NAIVE_IS_UTC:
                            entries.index = entries.index.tz_localize(dt.timezone.utc).tz_convert(now_local.tzinfo)
                        else:
                            entries.index = entries.index.tz_localize(now_local.tzinfo)
                    else:
                        entries.index = entries.index.tz_convert(now_local.tzinfo)
                except Exception:
                    pass
                # Forced offset shift
                if _FORCE_TZ_OFFSET_MINUTES:
                    try:
                        entries.index = entries.index + pd.to_timedelta(_FORCE_TZ_OFFSET_MINUTES, unit="m")
                    except Exception:
                        pass
                window_start = window_end - dt.timedelta(minutes=WINDOW_MIN)
                entries = entries.loc[entries.index >= window_start]
                # --- Basal series: apply same normalization & window filtering so it stays visible ---
                if basal_series is not None and not basal_series.empty:
                    try:
                        if basal_series.index.tzinfo is None:
                            if _ASSUME_NAIVE_IS_UTC:
                                basal_series.index = basal_series.index.tz_localize(dt.timezone.utc).tz_convert(now_local.tzinfo)
                            else:
                                basal_series.index = basal_series.index.tz_localize(now_local.tzinfo)
                        else:
                            basal_series.index = basal_series.index.tz_convert(now_local.tzinfo)
                    except Exception:
                        pass
                    if _FORCE_TZ_OFFSET_MINUTES:
                        try:
                            basal_series.index = basal_series.index + pd.to_timedelta(_FORCE_TZ_OFFSET_MINUTES, unit="m")
                        except Exception:
                            pass
                    try:
                        basal_series = basal_series.loc[basal_series.index >= window_start]
                    except Exception:
                        pass
                # Convert to naive local datetimes to prevent Matplotlib timezone double-shift
                if _FORCE_NAIVE_LOCAL:
                    try:
                        entries.index = pd.DatetimeIndex([ts.astimezone(now_local.tzinfo).replace(tzinfo=None) for ts in entries.index])
                        window_end_naive = window_end.replace(tzinfo=None)
                        window_start_naive = window_start.replace(tzinfo=None)
                        if basal_series is not None and not basal_series.empty:
                            try:
                                basal_series.index = pd.DatetimeIndex([ts.astimezone(now_local.tzinfo).replace(tzinfo=None) for ts in basal_series.index])
                            except Exception:
                                pass
                    except Exception:
                        window_end_naive = window_end
                        window_start_naive = window_start
                else:
                    window_end_naive = window_end
                    window_start_naive = window_start
            else:
                window_start = window_end - dt.timedelta(minutes=WINDOW_MIN)
                window_end_naive = window_end.replace(tzinfo=None) if _FORCE_NAIVE_LOCAL else window_end
                window_start_naive = window_start.replace(tzinfo=None) if _FORCE_NAIVE_LOCAL else window_start
            # BG curve
            if entries is not None and not entries.empty:
                self.ax1.plot(entries.index, entries["mmol"], color="#1f77b4", linewidth=1.6)
                self.ax1.scatter(entries.index[-1], entries["mmol"].iloc[-1], color="#1f77b4", s=22, zorder=3)
                # Dynamic y-limits with padding (unless overrides set)
                try:
                    data_min = float(np.nanmin(entries['mmol'].values))
                    data_max = float(np.nanmax(entries['mmol'].values))
                    pad_low = 0.4
                    pad_high = 0.6
                    y_min = data_min - pad_low
                    y_max = data_max + pad_high
                    # Include target range bounds
                    tr_low, tr_high = target_range
                    y_min = min(y_min, tr_low - 0.3)
                    y_max = max(y_max, tr_high + 0.3)
                    if not np.isnan(BG_YMIN_OVERRIDE):
                        y_min = BG_YMIN_OVERRIDE
                    if not np.isnan(BG_YMAX_OVERRIDE):
                        y_max = BG_YMAX_OVERRIDE
                    if y_min < 0:
                        y_min = 0
                    cur_ymin, cur_ymax = self.ax1.get_ylim()
                    if abs(cur_ymin - y_min) > 0.05 or abs(cur_ymax - y_max) > 0.05:
                        self.ax1.set_ylim(y_min, y_max)
                except Exception:
                    if _DEBUG_TIME:
                        print('[Y-LIM DEBUG] widget dynamic y-limit failed', flush=True)

            # Target range band
            low_t, high_t = target_range
            self.ax1.axhspan(low_t, high_t, color="#2a3b4d", alpha=0.35, zorder=0, label="Zielbereich")

            # Basal
            if basal_series is not None and not basal_series.empty:
                self.ax2.step(basal_series.index, basal_series["plan_u_per_h"], where="post", color="#555555", linestyle="--", linewidth=1.2, label="Plan-Basal")
                self.ax2.step(basal_series.index, basal_series["actual_u_per_h"], where="post", color="#2ca02c", linewidth=1.6, label="Basal")
                plan_vals = basal_series["plan_u_per_h"].values
                actual_vals = basal_series["actual_u_per_h"].values
                diff_mask = (np.abs(actual_vals - plan_vals) > 1e-6)
                if diff_mask.any():
                    self.ax2.fill_between(basal_series.index, basal_series["plan_u_per_h"], basal_series["actual_u_per_h"], where=diff_mask, step="post", color="#ff7f0e", alpha=0.28, label="Temp-Basal")
            self.ax2.set_ylabel("Basal (U/h)")

            # ---------------- Custom inner y tick labels (avoid clipping on left) ----------------
            try:
                # Remove outside labels
                self.ax1.tick_params(labelleft=False)
                self.ax2.tick_params(labelleft=False)
                # Get tick positions AFTER data plotted (so limits set)
                def _draw_inner_ticks(ax, fmt="{v:g}"):
                    ylim = ax.get_ylim()
                    ticks = ax.get_yticks()
                    # Filter ticks inside current view (avoid extremes half outside)
                    ticks = [t for t in ticks if ylim[0] - 1e-9 <= t <= ylim[1] + 1e-9]
                    # Remove previous custom labels if any (tag via custom attr)
                    for artist in getattr(ax, "_inner_tick_text", []):
                        try:
                            artist.remove()
                        except Exception:
                            pass
                    inner = []
                    for t in ticks:
                        txt = ax.text(
                            0.002,  # small inset from left inside axes fraction
                            (t - ylim[0]) / (ylim[1] - ylim[0]) if (ylim[1] - ylim[0]) else 0.0,
                            fmt.format(v=(round(t,2) if abs(t) < 10 else int(round(t)))),
                            transform=ax.transAxes,
                            ha="left",
                            va="center",
                            color="#cfcfcf",
                            fontsize=7,
                            zorder=10,
                        )
                        inner.append(txt)
                    ax._inner_tick_text = inner
                _draw_inner_ticks(self.ax1)
                _draw_inner_ticks(self.ax2)
            except Exception:
                pass

            # Event annotations
            # Normalize / shift events analogous to entries
            def _norm_events(ev_list: List[Dict[str, Any]]):
                out = []
                for ev in ev_list or []:
                    ts = ev.get("time") or ev.get("timestamp")
                    if not isinstance(ts, dt.datetime):
                        continue
                    try:
                        if ts.tzinfo is None:
                            if _ASSUME_NAIVE_IS_UTC:
                                ts = ts.replace(tzinfo=dt.timezone.utc).astimezone(now_local.tzinfo)
                            else:
                                ts = ts.replace(tzinfo=now_local.tzinfo)
                        else:
                            ts = ts.astimezone(now_local.tzinfo)
                        if _FORCE_TZ_OFFSET_MINUTES:
                            ts = ts + dt.timedelta(minutes=_FORCE_TZ_OFFSET_MINUTES)
                        if _FORCE_NAIVE_LOCAL:
                            ts = ts.replace(tzinfo=None)
                        ev_copy = dict(ev)
                        ev_copy["time"] = ts
                        # Filter to window
                        if window_start_naive <= ts <= window_end_naive:
                            out.append(ev_copy)
                    except Exception:
                        continue
                return out

            bolus_big_n = _norm_events(bolus_big)
            bolus_mini_n = _norm_events(bolus_mini)
            carbs_n = _norm_events(carbs)

            if _DEBUG_TIME:
                try:
                    def _rng(lst):
                        if not lst: return "[]"
                        ts = [x['time'] for x in lst if isinstance(x.get('time'), dt.datetime)]
                        if not ts: return "[]"
                        return f"[{min(ts)} .. {max(ts)}]"
                    print(f"[EVENT DEBUG] big={len(bolus_big_n)} mini={len(bolus_mini_n)} carbs={len(carbs_n)} window={window_start_naive}..{window_end_naive} big_rng={_rng(bolus_big_n)} carbs_rng={_rng(carbs_n)}")
                except Exception:
                    pass

            self._annotate_events_on_bg(self.ax1, entries, bolus_big_n, carbs_n)
            # Mini bolus triangles at bottom
            # SMB (mini) boluses: triangles only (list already contains only SMB via split_events logic)
            if bolus_mini_n and entries is not None and not entries.empty:
                ymin, ymax = self.ax1.get_ylim()
                y_tri = ymin + (ymax - ymin) * 0.02
                t = [b["time"] for b in bolus_mini_n]
                self.ax1.scatter(t, [y_tri] * len(t), marker='v', s=18, color="#1f77b4", alpha=0.9)

            # Recompute ylim headroom before annotations (so annotation expansion works)
            self.ax2.set_xlabel("Zeit")
            self.ax2.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=5))
            self.ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            # Apply x-limits using naive datetimes if converted
            self.ax2.set_xlim([window_start_naive, window_end_naive])
            try:
                self.ax1.set_xlim([window_start_naive, window_end_naive])
            except Exception:
                pass

            if _DEBUG_TIME:
                try:
                    last_ts = entries.index[-1] if entries is not None and not entries.empty else None
                    delta = (now_local - (last_ts if isinstance(last_ts, dt.datetime) and last_ts.tzinfo else now_local)) if last_ts else None
                    print(
                        f"[TIME DEBUG] now={now_local.isoformat()} start={window_start_naive} end={window_end_naive} last_ts={last_ts} "
                        f"delta_min={None if delta is None else round(delta.total_seconds()/60,2)} assume_naive_utc={_ASSUME_NAIVE_IS_UTC} "
                        f"offset_min={_FORCE_TZ_OFFSET_MINUTES} force_naive={_FORCE_NAIVE_LOCAL}"
                    )
                except Exception:
                    pass

            # Tight layout: reduce margins and remove vertical gap between plots
            # Hide x tick labels on upper plot to save vertical space
            try:
                self.ax1.tick_params(labelbottom=False)
            except Exception:
                pass
            # Slight gap reinstated (hspace=0.05)
            # Slightly larger vertical spacing between BG (ax1) and Basal (ax2)
            self.fig.subplots_adjust(left=0.04, right=0.995, top=0.985, bottom=0.10, hspace=0.08)
            self.canvas.draw_idle()

            # Redraw inner ticks after final draw (in case limits changed by annotations)
            def _redraw_inner(event=None):
                for ax in (self.ax1, self.ax2):
                    try:
                        ylim = ax.get_ylim()
                        ticks = ax.get_yticks()
                        ticks = [t for t in ticks if ylim[0] - 1e-9 <= t <= ylim[1] + 1e-9]
                        for artist in getattr(ax, "_inner_tick_text", []):
                            try:
                                artist.remove()
                            except Exception:
                                pass
                        inner = []
                        for t in ticks:
                            try:
                                label_txt = f"{round(t,2) if abs(t) < 10 else int(round(t))}"
                            except Exception:
                                label_txt = str(t)
                            txt = ax.text(
                                0.002,
                                (t - ylim[0]) / (ylim[1] - ylim[0]) if (ylim[1]-ylim[0]) else 0.0,
                                label_txt,
                                transform=ax.transAxes,
                                ha="left", va="center", color="#cfcfcf", fontsize=7, zorder=10,
                            )
                            inner.append(txt)
                        ax._inner_tick_text = inner
                    except Exception:
                        pass
                self.canvas.draw_idle()
            _redraw_inner()

            # Note: PNG auto-saving removed per request; keep rendering only

    # Context menu helpers and compact mode
    def _show_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _toggle_compact(self):
        # Toggle compact; if enabling, turn off minimal and restore text labels
        new_val = not self._compact
        if self._dexcom_only and new_val:
            # In Dexcom-only, compact is disabled
            messagebox.showinfo("Nicht verfügbar", "Kompaktmodus ist im Dexcom-Only Modus deaktiviert.")
            return
        self._compact = new_val
        try:
            if new_val:
                # Ensures modes are exclusive: disable minimal and restore its hidden labels
                if self._minimal:
                    self._minimal = False
                    try:
                        self.lbl_sub1.pack(**self._lbl_sub1_pack)
                        self.lbl_sub2.pack(**self._lbl_sub2_pack)
                        self.lbl_pump.pack(**self._lbl_pump_pack)
                        self.lbl_temp.pack(**self._lbl_temp_pack)
                    except Exception:
                        pass
                # Hide chart in compact mode
                try:
                    self.chart_frame.pack_forget()
                except Exception:
                    pass
            else:
                # Leaving compact: only restore chart if not in minimal mode
                if not self._minimal:
                    try:
                        self.chart_frame.pack(**self._chart_pack)
                        self.schedule_update(0)
                    except Exception:
                        pass
        except Exception:
            pass

    def _toggle_minimal(self):
        # Minimal mode shows only BG; make it exclusive with compact
        new_val = not self._minimal
        if self._dexcom_only and (not new_val):
            # In Dexcom-only, minimal must remain enabled
            messagebox.showinfo("Erforderlich", "Im Dexcom-Only Modus ist nur der Minimalmodus verfügbar.")
            return
        self._minimal = new_val
        try:
            if new_val:
                # Enabling minimal: hide sub/pump/temp labels and chart, and disable compact
                try:
                    self.lbl_sub1.pack_forget()
                    self.lbl_sub2.pack_forget()
                    self.lbl_pump.pack_forget()
                    self.lbl_temp.pack_forget()
                    # Keep age label visible in minimal mode
                    if not self.lbl_age.winfo_ismapped():
                        self.lbl_age.pack(**self._lbl_age_pack)
                except Exception:
                    pass
                try:
                    self.chart_frame.pack_forget()
                except Exception:
                    pass
                # Ensure compact is off
                self._compact = False
            else:
                # Disabling minimal: restore labels
                try:
                    self.lbl_sub1.pack(**self._lbl_sub1_pack)
                    self.lbl_sub2.pack(**self._lbl_sub2_pack)
                    self.lbl_pump.pack(**self._lbl_pump_pack)
                    self.lbl_temp.pack(**self._lbl_temp_pack)
                    if not self.lbl_age.winfo_ismapped():
                        self.lbl_age.pack(**self._lbl_age_pack)
                except Exception:
                    pass
                # Restore chart only if compact is off
                if not self._compact:
                    try:
                        self.chart_frame.pack(**self._chart_pack)
                        self.schedule_update(0)
                    except Exception:
                        pass
        except Exception:
            pass

    def _open_dashboard(self):
        """Launch the full dashboard in a separate Python process to avoid blocking the widget UI."""
        try:
            if self._dexcom_only:
                messagebox.showinfo("Nicht verfügbar", "Dashboard ist im Dexcom-Only Modus deaktiviert.")
                return
            script = os.path.join(os.path.dirname(__file__), "dashboard.py")
            # Use same interpreter (venv) to run the dashboard script
            subprocess.Popen([sys.executable, script], close_fds=False)
        except Exception:
            # Optional: we could show a brief status message
            pass

    # --------------- Settings Window ----------------
    def _open_settings(self):
        try:
            SettingsWindow(self.root, on_save=self._apply_settings)
        except Exception as e:
            messagebox.showerror("Fehler", f"Einstellungen konnten nicht geöffnet werden: {e}")

    def _apply_settings(self, data: Dict[str, str]):
        # Persist to .env file (create/update)
        # Save to the same .env path we load from (EXE dir preferred if existing)
        env_path = _resolve_env_path(prefer_existing=False)
        # Read existing lines to preserve unknown keys
        existing: Dict[str, str] = {}
        if os.path.exists(env_path):
            try:
                with open(env_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        if '=' in line and not line.strip().startswith('#'):
                            k, v = line.strip().split('=', 1)
                            existing[k] = v
            except Exception:
                pass
        existing.update({
            'NIGHTSCOUT_URL': data.get('NIGHTSCOUT_URL', '').strip(),
            'NS_TOKEN': data.get('NS_TOKEN', '').strip(),
            'NS_API_SECRET': data.get('NS_API_SECRET', '').strip(),
            'DEXCOM_USERNAME': data.get('DEXCOM_USERNAME', '').strip(),
            'DEXCOM_PASSWORD': data.get('DEXCOM_PASSWORD', '').strip(),
            'DEXCOM_REGION': data.get('DEXCOM_REGION', '').strip(),
            'USE_DEXCOM': '1' if data.get('USE_DEXCOM') else '0',
        })
        try:
            with open(env_path, 'w', encoding='utf-8') as f:
                for k, v in existing.items():
                    f.write(f"{k}={v}\n")
        except Exception as e:
            messagebox.showerror("Fehler", f".env konnte nicht geschrieben werden: {e}")
            return
        # Reload env for current process
        ns_refresh_env()
        # Update flags
        self._dexcom_only = bool(os.getenv("USE_DEXCOM", "").strip().lower() in ("1", "true", "yes", "on"))
        if self._dexcom_only:
            # Force minimal and hide chart
            self._minimal = True
            self._compact = False
            try:
                self.chart_frame.pack_forget()
            except Exception:
                pass
            # Also rebuild context menu to hide unsupported actions
            self._build_menu()
            # Hide informational labels in minimal mode
            try:
                self.lbl_sub1.pack_forget(); self.lbl_sub2.pack_forget(); self.lbl_pump.pack_forget(); self.lbl_temp.pack_forget()
            except Exception:
                pass
        else:
            # Leaving Dexcom-only: rebuild menu with full options and restore labels/chart as per modes
            self._build_menu()
            try:
                self.lbl_sub1.pack(**self._lbl_sub1_pack)
                self.lbl_sub2.pack(**self._lbl_sub2_pack)
                self.lbl_pump.pack(**self._lbl_pump_pack)
                self.lbl_temp.pack(**self._lbl_temp_pack)
            except Exception:
                pass
            if not self._compact and not self._minimal:
                try:
                    self.chart_frame.pack(**self._chart_pack)
                except Exception:
                    pass
        self.schedule_update(0)

    def _format_age(self, ts: Optional[dt.datetime]) -> str:
        """Return small 'vor X min' age text from a timestamp; empty if unknown."""
        try:
            if ts is None:
                return ""
            now = dt.datetime.now(dt.timezone.utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            delta = now - ts.astimezone(dt.timezone.utc)
            secs = int(max(0, delta.total_seconds()))
            mins = secs // 60
            hours = mins // 60
            if hours >= 1:
                return f"vor {hours} h"
            return f"vor {mins} min"
        except Exception:
            return ""


class SettingsWindow:
    def __init__(self, master: tk.Tk, on_save):
        self.on_save = on_save
        self.window = tk.Toplevel(master)
        self.window.title("Einstellungen")
        self.window.configure(bg="#1a1a1a")
        self.window.resizable(False, False)

        pad = {'padx': 8, 'pady': 4}
        labfg = "#e0e0e0"; bg = "#1a1a1a"

        row = 0
        # Nightscout (grouped in a frame for easy hide/show)
        self.ns_frame = tk.Frame(self.window, bg=bg)
        self.ns_frame.grid(row=row, column=0, columnspan=2, sticky='we', **pad); row += 1
        tk.Label(self.ns_frame, text="Nightscout URL", bg=bg, fg=labfg).grid(row=0, column=0, sticky='w', **pad)
        self.ns_url = tk.Entry(self.ns_frame, width=38)
        self.ns_url.grid(row=0, column=1, **pad)
        tk.Label(self.ns_frame, text="NS Token", bg=bg, fg=labfg).grid(row=1, column=0, sticky='w', **pad)
        self.ns_token = tk.Entry(self.ns_frame, width=38)
        self.ns_token.grid(row=1, column=1, **pad)
        tk.Label(self.ns_frame, text="NS API Secret", bg=bg, fg=labfg).grid(row=2, column=0, sticky='w', **pad)
        self.ns_secret = tk.Entry(self.ns_frame, width=38, show='*')
        self.ns_secret.grid(row=2, column=1, **pad)

        # Dexcom (grouped frame)
        self.dx_frame = tk.Frame(self.window, bg=bg)
        self.dx_frame.grid(row=row, column=0, columnspan=2, sticky='we', **pad); row += 1
        tk.Label(self.dx_frame, text="Dexcom Benutzer", bg=bg, fg=labfg).grid(row=0, column=0, sticky='w', **pad)
        self.dx_user = tk.Entry(self.dx_frame, width=38)
        self.dx_user.grid(row=0, column=1, **pad)
        tk.Label(self.dx_frame, text="Dexcom Passwort", bg=bg, fg=labfg).grid(row=1, column=0, sticky='w', **pad)
        self.dx_pwd = tk.Entry(self.dx_frame, width=38, show='*')
        self.dx_pwd.grid(row=1, column=1, **pad)
        tk.Label(self.dx_frame, text="Dexcom Region (us/eu/ous)", bg=bg, fg=labfg).grid(row=2, column=0, sticky='w', **pad)
        self.dx_region = tk.Entry(self.dx_frame, width=38)
        self.dx_region.grid(row=2, column=1, **pad)

        # Toggle
        self.use_dexcom_var = tk.BooleanVar(value=bool(os.getenv('USE_DEXCOM', '').strip().lower() in ('1','true','yes','on')))
        tk.Checkbutton(self.window, text="Nur Dexcom verwenden (ohne Nightscout)", variable=self.use_dexcom_var, bg=bg, fg=labfg, selectcolor="#2a2a2a").grid(row=row, column=0, columnspan=2, sticky='w', **pad); row += 1

        # Buttons
        btn_frame = tk.Frame(self.window, bg=bg)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=10)
        tk.Button(btn_frame, text="Abbrechen", command=self.window.destroy).pack(side='right', padx=6)
        tk.Button(btn_frame, text="Speichern", command=self._save).pack(side='right')

        # Prefill from env
        self._load_from_env()
        # Toggle fields visibility according to checkbox
        self.use_dexcom_var.trace_add('write', lambda *args: self._toggle_visibility())
        self._toggle_visibility()

    def _load_from_env(self):
        self.ns_url.insert(0, os.getenv('NIGHTSCOUT_URL',''))
        self.ns_token.insert(0, os.getenv('NS_TOKEN',''))
        self.ns_secret.insert(0, os.getenv('NS_API_SECRET','') or os.getenv('NIGHTSCOUT_API_SECRET',''))
        self.dx_user.insert(0, os.getenv('DEXCOM_USERNAME',''))
        self.dx_pwd.insert(0, os.getenv('DEXCOM_PASSWORD',''))
        self.dx_region.insert(0, os.getenv('DEXCOM_REGION',''))

    def _save(self):
        data = {
            'NIGHTSCOUT_URL': self.ns_url.get(),
            'NS_TOKEN': self.ns_token.get(),
            'NS_API_SECRET': self.ns_secret.get(),
            'DEXCOM_USERNAME': self.dx_user.get(),
            'DEXCOM_PASSWORD': self.dx_pwd.get(),
            'DEXCOM_REGION': self.dx_region.get(),
            'USE_DEXCOM': self.use_dexcom_var.get(),
        }
        try:
            self.on_save(data)
            self.window.destroy()
        except Exception as e:
            messagebox.showerror("Fehler", f"Speichern fehlgeschlagen: {e}")

    def _toggle_visibility(self):
        # When Dexcom-only is enabled, hide Nightscout fields; otherwise show them
        try:
            if self.use_dexcom_var.get():
                self.ns_frame.grid_remove()
                self.dx_frame.grid()
            else:
                self.ns_frame.grid()
                # Hide Dexcom fields when not in Dexcom-only mode
                self.dx_frame.grid_remove()
        except Exception:
            pass


# Helpers for legend handles without importing pyplot here
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

def plt_line_dummy(color="#fff", lw=1.0, ls="-"):
    return Line2D([0], [0], color=color, linewidth=lw, linestyle=ls)

def plt_patch_dummy(facecolor="#fff", alpha=0.3):
    return Patch(facecolor=facecolor, edgecolor="none", alpha=alpha)


def main():
    root = tk.Tk()
    app = CGMWidget(root)
    # initial position (top right-ish)
    root.geometry("+1200+40")
    root.mainloop()


if __name__ == "__main__":
    main()
