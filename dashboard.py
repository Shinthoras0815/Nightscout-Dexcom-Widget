# -*- coding: utf-8 -*-

import os
import sys
import hashlib
import datetime as dt
from typing import Optional, Dict, Any, List, Tuple

import requests
import urllib3
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import matplotlib.dates as mdates
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.offsetbox import TextArea, VPacker, AnchoredOffsetbox
import matplotlib.patheffects as patheffects
mplcursors = None  # hover removed; we'll use a shared cursor instead
import dateutil.parser
from dotenv import load_dotenv
from net import get_json as ns_get_json, BASE as NS_BASE


# --- Setup -----------------------------------------------------------
# Load .env from the application directory (EXE path when frozen, file dir otherwise)
def _app_base_dir() -> str:
    try:
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
    except Exception:
        pass
    return os.path.dirname(__file__)

load_dotenv(dotenv_path=os.path.join(_app_base_dir(), '.env'))
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = os.getenv("NIGHTSCOUT_URL")
SECRET = os.getenv("NS_API_SECRET") or os.getenv("NIGHTSCOUT_API_SECRET")
TOKEN = os.getenv("NS_TOKEN")
SECRET_SHA1 = hashlib.sha1(SECRET.encode("utf-8")).hexdigest() if SECRET else None
NOW = dt.datetime.now(dt.timezone.utc)

# Optional BG axis overrides (mmol/L)
try:
    BG_YMIN_OVERRIDE = float(os.getenv("BG_YMIN", "nan"))
except Exception:
    BG_YMIN_OVERRIDE = float('nan')
try:
    BG_YMAX_OVERRIDE = float(os.getenv("BG_YMAX", "nan"))
except Exception:
    BG_YMAX_OVERRIDE = float('nan')

# Debug flag for sensor/site age extraction
DEBUG_AGE = str(os.getenv("DEBUG_AGE", "0")).strip().lower() in ("1","true","yes","on")

# Time window for the dashboard
WINDOW_MIN = 360  # last 6h

# Simple PK params for fallback IOB
DIA_MINUTES = 300
T_HALF = 60


get_json = ns_get_json


def mgdl_to_mmol(x: float) -> float:
    return round(x / 18.01559, 1)


def fetch_entries(since: dt.datetime) -> pd.DataFrame:
    # Prefer server-side filter by epoch ms
    since_ms = int(since.timestamp() * 1000)
    data = get_json("/api/v1/entries.json", {"find[date][$gte]": since_ms, "count": 1000})
    rows = []
    for e in data:
        # Nightscout entries can have sgv (mg/dL)
        val = e.get("sgv") or e.get("mbg") or e.get("glucose")
        if not isinstance(val, (int, float)):
            continue
        direction = e.get("direction")
        ts_raw = e.get("dateString") or e.get("created_at")
        if ts_raw:
            try:
                ts = dateutil.parser.isoparse(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
            except Exception:
                ts = None
        else:
            ms = e.get("date")
            ts = dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc) if isinstance(ms, (int, float)) else None
        if ts is None:
            continue
        rows.append({"time": ts.astimezone(), "mgdl": float(val), "direction": direction})
    if not rows:
        return pd.DataFrame(columns=["time", "mgdl", "mmol", "direction"]).set_index("time")
    df = pd.DataFrame(rows).dropna(subset=["time", "mgdl"]).sort_values("time")
    df["mmol"] = df["mgdl"].apply(mgdl_to_mmol)
    df = df.set_index("time")
    return df


def fetch_profile() -> Dict[str, Any]:
    prof = get_json("/api/v1/profile.json", {"count": 1})[0]
    return prof


def plan_basal_from_profile(prof: Dict[str, Any]) -> Dict[int, float]:
    store = prof["store"][prof["defaultProfile"]]
    segs = store["basal"]
    return {int(s["timeAsSeconds"]): float(s["value"]) for s in segs}


def target_range_from_profile(prof: Dict[str, Any]) -> Tuple[float, float]:
    # Defaults in mmol/L
    default_low, default_high = 3.9, 10.0
    try:
        store = prof["store"][prof["defaultProfile"]]
        units = store.get("units", "mg/dL")
        # targets can be in 'target_low'/'target_high' lists or a combined 'target'
        lows = store.get("target_low") or store.get("targets") or store.get("targetLower")
        highs = store.get("target_high") or store.get("targetUpper")
        # If Nightscout schema stores list of dicts with start times
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
            if str(units).lower() in ("mg/dl", "mgdl"):
                return (low_val / 18.01559, high_val / 18.01559)
            return (float(low_val), float(high_val))
    except Exception:
        pass
    return (default_low, default_high)


def direction_arrow_from_text(dir_text: Optional[str]) -> Optional[str]:
    if not dir_text:
        return None
    d = str(dir_text).lower()
    mapping = {
        "flat": "→",
        "fortyfiveup": "↗",
        "fortyfivedown": "↘",
        "singleup": "↑",
        "singledown": "↓",
        "doubleup": "↑↑",
        "doubledown": "↓↓",
        "none": "",
    }
    return mapping.get(d)


def compute_arrow_from_slope(entries: pd.DataFrame) -> Optional[str]:
    # Fallback: derive simple arrow from slope of last ~15 minutes
    if entries.empty or len(entries) < 3:
        return None
    # Take last 15 minutes window
    end = entries.index[-1]
    start = end - dt.timedelta(minutes=15)
    df = entries.loc[entries.index >= start]
    if len(df) < 2:
        df = entries.tail(5)
    x = df.index.astype(np.int64) / 1e9  # seconds
    y = df["mmol"].astype(float).values
    if len(x) < 2:
        return None
    # Simple linear fit
    try:
        slope_per_s = np.polyfit(x - x[0], y, 1)[0]
        slope_15min = slope_per_s * (15 * 60)
        # thresholds (mmol/L per 15 min)
        if slope_15min >= 0.5:
            return "↑"
        if 0.2 <= slope_15min < 0.5:
            return "↗"
        if -0.2 < slope_15min < 0.2:
            return "→"
        if -0.5 < slope_15min <= -0.2:
            return "↘"
        if slope_15min <= -0.5:
            return "↓"
    except Exception:
        return None
    return None


def current_plan_basal(plan: Dict[int, float], t: dt.datetime) -> Optional[float]:
    if not plan:
        return None
    local = t.astimezone()
    secs = local.hour * 3600 + local.minute * 60 + local.second
    keys = sorted(plan)
    current: Optional[float] = None
    for k in keys:
        if k <= secs:
            current = plan[k]
        else:
            break
    if current is None:
        current = plan[keys[-1]]
    return float(current)


def fetch_treatments(since: dt.datetime) -> List[Dict[str, Any]]:
    since_iso = since.isoformat()
    data = get_json("/api/v1/treatments.json", {"find[created_at][$gte]": since_iso, "count": 1000})
    return data


def fetch_latest_sensor_change() -> Optional[dt.datetime]:
    """Fetch the timestamp of the most recent 'Sensor Change' (or similar) treatment without
    being limited to the current dashboard time window. Returns timezone-aware UTC datetime or None.
    We first try an exact match on eventType 'Sensor Change'. If none found, we attempt a small
    regex fallback that may catch variants like 'Sensor Start'."""
    try:
        # Exact match first
        data = get_json("/api/v1/treatments.json", {"find[eventType]": "Sensor Change", "count": 1})
        if not data:
            # Regex fallback (Nightscout supports $regex); keep lightweight
            data = get_json(
                "/api/v1/treatments.json",
                {"find[eventType][$regex]": "Sensor Change|Sensor Start", "count": 3},
            )
        if not data:
            return None
        t0 = data[0]
        ts_raw = t0.get("created_at") or t0.get("timestamp") or t0.get("dateString")
        ts = None
        if ts_raw:
            try:
                ts = dateutil.parser.isoparse(ts_raw)
            except Exception:
                ts = None
        if ts is None:
            ms = t0.get("mills") or t0.get("date")
            if isinstance(ms, (int, float)):
                ts = dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc)
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return ts.astimezone(dt.timezone.utc)
    except Exception:
        return None


def split_events(treatments: List[Dict[str, Any]]):
    bolus_big: List[Dict[str, Any]] = []
    bolus_mini: List[Dict[str, Any]] = []
    carbs: List[Dict[str, Any]] = []
    temps: List[Dict[str, Any]] = []
    for t in treatments:
        et = (t.get("eventType") or "").lower()
        t_type = (t.get("type") or "").lower()
        tags = [str(x).lower() for x in (t.get("tags") or [])]
        ts_raw = t.get("created_at") or t.get("timestamp") or t.get("dateString")
        if not ts_raw:
            ms = t.get("mills") or t.get("date")
            ts = dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc) if isinstance(ms, (int, float)) else None
        else:
            try:
                ts = dateutil.parser.isoparse(ts_raw)
            except Exception:
                ts = None
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
        if ts is None:
            continue
        ts = ts.astimezone()

        if et == "temp basal":
            temps.append({
                "start": ts,
                "duration": int(t.get("duration", 0)),
                "percent": float(t.get("percent", np.nan)) if t.get("percent") is not None else np.nan,
                "absolute": float(t.get("absolute", np.nan)) if t.get("absolute") is not None else np.nan,
            })
        else:
            # carbs
            carbs_val = t.get("carbs") or t.get("carb_input")
            if isinstance(carbs_val, (int, float)) and carbs_val > 0:
                carbs.append({"time": ts, "grams": float(carbs_val)})
            # bolus
            units = None
            for k in ("insulin", "insulinInUnits", "amount", "units", "value"):
                v = t.get(k)
                if isinstance(v, (int, float)):
                    units = float(v)
                    break
                if isinstance(v, str):
                    try:
                        units = float(v.replace(",", "."))
                        break
                    except ValueError:
                        pass
            if units and units > 0:
                # SMB Definition: explicit type 'smb' OR tag contains smb OR type field contains smb
                is_smb = (t_type == "smb") or ("smb" in t_type) or any("smb" in tg for tg in tags) or ("smb" in et)
                if is_smb:
                    bolus_mini.append({"time": ts, "units": units, "is_smb": True})
                else:
                    bolus_big.append({"time": ts, "units": units, "is_smb": False})
    return bolus_big, bolus_mini, carbs, temps


def build_basal_series(plan: Dict[int, float], temps: List[Dict[str, Any]], start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    # Minute grid in local tz
    idx = pd.date_range(start=start.astimezone(), end=end.astimezone(), freq="1min")
    df = pd.DataFrame(index=idx)
    # plan rate per minute
    def plan_at(t: pd.Timestamp) -> float:
        return current_plan_basal(plan, t.to_pydatetime()) or 0.0
    df["plan_u_per_h"] = [plan_at(ts) for ts in df.index]
    df["actual_u_per_h"] = df["plan_u_per_h"].values

    # apply temps
    for temp in temps:
        start_local = temp["start"]
        end_local = start_local + pd.to_timedelta(temp["duration"], unit="m")
        mask = (df.index >= start_local) & (df.index < end_local)
        if mask.any():
            if not np.isnan(temp.get("absolute", np.nan)):
                df.loc[mask, "actual_u_per_h"] = float(temp["absolute"])
            elif not np.isnan(temp.get("percent", np.nan)):
                df.loc[mask, "actual_u_per_h"] = df.loc[mask, "plan_u_per_h"] * (float(temp["percent"]) / 100.0)
    df["is_temp"] = (df["actual_u_per_h"] != df["plan_u_per_h"]).astype(int)
    return df


def latest_devicestatus(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
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
                d = dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc)
        if d is None:
            d = dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    return sorted(items, key=_ts, reverse=True)[0]


def prefer_devicestatus_metrics(latest: Optional[Dict[str, Any]]):
    cob_g = None
    bolus_iob = None
    basal_iob = None
    pump_batt = None
    reservoir = None
    uploader_batt = None
    sensor_age_min = None  # SAGE (minutes)

    if latest:
        loop = latest.get("openaps") or latest.get("loop") or {}
        if isinstance(loop, dict):
            cob_obj = loop.get("cob")
            if isinstance(cob_obj, dict) and isinstance(cob_obj.get("cob"), (int, float)):
                cob_g = float(cob_obj["cob"])
            iob_obj = loop.get("iob")
            if isinstance(iob_obj, dict):
                it = iob_obj.get("iob")
                bi = iob_obj.get("basaliob")
                if isinstance(it, (int, float)) and isinstance(bi, (int, float)):
                    bolus_iob = float(it) - float(bi)
                    basal_iob = float(bi)
        pump = latest.get("pump") or {}
        if isinstance(pump, dict):
            pb = pump.get("battery")
            if isinstance(pb, dict):
                if isinstance(pb.get("percent"), (int, float)):
                    pump_batt = f"{pb['percent']}%"
                elif isinstance(pb.get("voltage"), (int, float)):
                    pump_batt = f"{pb['voltage']} V"
            if isinstance(pump.get("reservoir"), (int, float)):
                reservoir = f"{float(pump['reservoir']):.1f} U"
        uploader = latest.get("uploader") or {}
        if isinstance(uploader, dict) and isinstance(uploader.get("battery"), (int, float)):
            uploader_batt = f"{uploader['battery']}%"

        # Try sensor/site age (varies by uploader). Common keys: SAGE/IAGE, sage/iage inside pump or uploader status.
        # Search a few likely containers.
        def _extract_ages(obj, depth=0):
            nonlocal sensor_age_min
            if depth > 6:
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    kl = str(k).lower()
                    # Accept numbers as strings
                    if isinstance(v, str):
                        try:
                            v_num = float(v.replace(',', '.'))
                            v = v_num
                        except Exception:
                            pass
                    if kl in ("sage", "sensorage") and sensor_age_min is None and isinstance(v, (int, float)):
                        sensor_age_min = int(v)
                        if DEBUG_AGE:
                            print(f"[AGE DEBUG] Found sensor age {sensor_age_min} via key '{k}'")
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
            print("[AGE DEBUG] No sensor age field found in latest devicestatus")
    return cob_g, bolus_iob, basal_iob, pump_batt, reservoir, uploader_batt, sensor_age_min


def fallback_bolus_iob(treatments: List[Dict[str, Any]]) -> float:
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

    total = 0.0
    for t in treatments:
        u = _units(t)
        if u is None or u <= 0:
            continue
        ts_raw = t.get("created_at") or t.get("timestamp") or t.get("dateString")
        if not ts_raw:
            ms = t.get("mills") or t.get("date")
            ts = dt.datetime.fromtimestamp(ms/1000.0, tz=dt.timezone.utc) if isinstance(ms, (int, float)) else None
        else:
            try:
                ts = dateutil.parser.isoparse(ts_raw)
            except Exception:
                ts = None
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
        if ts is None:
            continue
        age_min = (NOW - ts).total_seconds() / 60.0
        if 0 <= age_min <= DIA_MINUTES:
            rem = 0.5 ** (age_min / T_HALF)
            total += u * rem
    return total


def main():
    use_dexcom = str(os.getenv("USE_DEXCOM", "")).strip().lower() in ("1", "true", "yes", "on")
    if use_dexcom:
        print("Dashboard ist im Dexcom-Only Modus deaktiviert. Öffne das Widget für die Minimalanzeige.")
        return
    if not BASE:
        raise RuntimeError("NIGHTSCOUT_URL not configured in .env")

    # Dark theme for better contrast
    plt.style.use("dark_background")
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8.5), sharex=True, gridspec_kw={"height_ratios": [3, 2]})

    # Track cursor connection id to avoid stacking handlers on refresh
    fig.canvas._cursor_cid = None

    def render_once():
        now = dt.datetime.now(dt.timezone.utc)
        start = now - dt.timedelta(minutes=WINDOW_MIN)

        # Fetch data fresh on each refresh
        entries = fetch_entries(start)
        treatments = fetch_treatments(start)
        latest_status = latest_devicestatus(get_json("/api/v1/devicestatus.json"))
        profile = fetch_profile()
        plan = plan_basal_from_profile(profile)

        bolus_big, bolus_mini, carbs, temps = split_events(treatments)
        basal_series = build_basal_series(plan, temps, start, now)

        cob_g, bolus_iob, basal_iob, pump_batt, reservoir, uploader_batt, sensor_age_min = prefer_devicestatus_metrics(latest_status)
        # Fallback: dedicated lookup for latest 'Sensor Change' not restricted by 6h window
        if sensor_age_min is None:
            if DEBUG_AGE:
                print("[AGE DEBUG] No sensor age in devicestatus; attempting dedicated Sensor Change lookup...")
            sc_ts = fetch_latest_sensor_change()
            if sc_ts is not None:
                sensor_age_min = int((now - sc_ts).total_seconds() / 60.0)
                if DEBUG_AGE:
                    print(f"[AGE DEBUG] Derived sensor age {sensor_age_min} min from latest 'Sensor Change' at {sc_ts.isoformat()}")
            else:
                if DEBUG_AGE:
                    print("[AGE DEBUG] No Sensor Change treatment found via dedicated lookup")
        if bolus_iob is None:
            bolus_iob = fallback_bolus_iob(treatments)
        if basal_iob is None:
            basal_iob = 0.0
        if cob_g is None:
            cob_g = 0.0

        # Current BG
        current_bg = entries["mmol"].iloc[-1] if not entries.empty else None
        # Age string from last entry time
        age_text = ""
        if not entries.empty:
            try:
                last_ts = entries.index[-1]
                if last_ts.tzinfo is None:
                    last_ts = last_ts.tz_localize(dt.timezone.utc)
                delta = now - last_ts.astimezone(dt.timezone.utc)
                secs = max(0, int(delta.total_seconds()))
                mins = secs // 60; hours = mins // 60
                age_text = (f"vor {hours} h" if hours >= 1 else f"vor {mins} min")
            except Exception:
                age_text = ""
        current_direction = None
        if not entries.empty and "direction" in entries.columns:
            last_dir = entries["direction"].dropna().iloc[-1] if not entries["direction"].dropna().empty else None
            current_direction = direction_arrow_from_text(last_dir)
        if current_direction is None:
            current_direction = compute_arrow_from_slope(entries)

        # Clear axes for a clean redraw
        ax1.clear(); ax2.clear()
        ax1.grid(True, which="both", axis="both", alpha=0.3, color="#444")
        ax1.set_ylabel("Glukose (mmol/L)")

        # BG line
        if not entries.empty:
            ax1.plot(entries.index, entries["mmol"], color="#1f77b4", label="Glukose", linewidth=2.0)
            ax1.scatter(entries.index[-1], entries["mmol"].iloc[-1], color="#1f77b4", s=30, zorder=3)

        # Target range band on BG axis
        low_t, high_t = target_range_from_profile(profile)
        ax1.axhspan(low_t, high_t, color="#2a3b4d", alpha=0.35, zorder=0, label="Zielbereich")
        # Dynamic BG y-limits with padding unless explicitly overridden
        try:
            if not entries.empty:
                data_min = float(np.nanmin(entries['mmol'].values))
                data_max = float(np.nanmax(entries['mmol'].values))
                pad_low = 0.4
                pad_high = 0.6
                y_min = data_min - pad_low
                y_max = data_max + pad_high
                y_min = min(y_min, low_t - 0.3)
                y_max = max(y_max, high_t + 0.3)
                if not np.isnan(BG_YMIN_OVERRIDE):
                    y_min = BG_YMIN_OVERRIDE
                if not np.isnan(BG_YMAX_OVERRIDE):
                    y_max = BG_YMAX_OVERRIDE
                if y_min < 0:
                    y_min = 0
                cur_ymin, cur_ymax = ax1.get_ylim()
                if abs(cur_ymin - y_min) > 0.05 or abs(cur_ymax - y_max) > 0.05:
                    ax1.set_ylim(y_min, y_max)
        except Exception:
            pass

        # Annotate metrics in a box
        metrics = []
        if current_bg is not None:
            arrow = f" {current_direction}" if current_direction else ""
            metrics.append(f"Aktuell: {current_bg:.1f} mmol/L{arrow}")
        metrics.append(f"Gesamt-IOB: {bolus_iob + basal_iob:.2f} IE")
        metrics.append(f"COB: {cob_g:.0f} g")
        if age_text:
            metrics.append(f"(letzte Daten {age_text})")
        ax1.text(0.01, 0.98, "\n".join(metrics), transform=ax1.transAxes, va="top", ha="left",
                 fontsize=11, bbox=dict(boxstyle="round,pad=0.3", facecolor="#111111", edgecolor="#333", alpha=0.85))

        # Bottom: basal and events
        temp_legend_added = False
        if not basal_series.empty:
            ax2.step(basal_series.index, basal_series["plan_u_per_h"], where="post", color="#555555", linestyle="--", linewidth=1.4, label="Plan-Basal")
            ax2.step(basal_series.index, basal_series["actual_u_per_h"], where="post", color="#2ca02c", linewidth=2.0, label="Basal")
            plan_vals = basal_series["plan_u_per_h"].values
            actual_vals = basal_series["actual_u_per_h"].values
            diff_mask = (np.abs(actual_vals - plan_vals) > 1e-6)
            if diff_mask.any():
                ax2.fill_between(basal_series.index, basal_series["plan_u_per_h"], basal_series["actual_u_per_h"], where=diff_mask, step="post", color="#ff7f0e", alpha=0.30, label="Temp-Basal")
                temp_legend_added = True
        ax2.set_ylabel("Basal (U/h)")
        ax2.grid(True, axis="y", alpha=0.3, color="#444")

        # Events on BG
        def annotate_events_on_bg(ax, entries_df: pd.DataFrame, bolus_list, carbs_list):
            if entries_df.empty:
                return
            bg_df = entries_df.reset_index()[["time", "mmol"]]
            ev_rows = []
            for b in bolus_list or []:
                # Only non-SMB boluses get labels
                if not b.get("is_smb"):
                    ev_rows.append({"time": b["time"], "label": f"B {b['units']:.1f} IE", "color": "#d62728"})
            for c in carbs_list or []:
                ev_rows.append({"time": c["time"], "label": f"C {int(round(c['grams']))}g", "color": "#2ca02c"})
            if not ev_rows:
                return
            ev_df = pd.DataFrame(ev_rows).sort_values("time")
            merged = pd.merge_asof(ev_df, bg_df, on="time")
            offsets = [16, -18]
            for i, row in merged.iterrows():
                if pd.isna(row.get("mmol")):
                    continue
                ann = ax.annotate(
                    row["label"],
                    xy=(row["time"], row["mmol"]),
                    xytext=(0, offsets[i % 2]),
                    textcoords="offset points",
                    ha="center",
                    va="bottom" if offsets[i % 2] > 0 else "top",
                    fontsize=11,
                    fontweight="bold",
                    color="#f0f0f0",
                    zorder=6,
                    bbox=dict(boxstyle="round,pad=0.35", facecolor="#111111", alpha=0.92, edgecolor=row["color"], linewidth=1.2),
                    arrowprops=dict(arrowstyle="->", color=row["color"], lw=1.0, alpha=0.9, shrinkA=0, shrinkB=2, connectionstyle="arc3,rad=0.1"),
                )
                try:
                    box = ann.get_bbox_patch()
                    box.set_path_effects([patheffects.withSimplePatchShadow(offset=(1.2, -1.2), shadow_rgbFace=(0, 0, 0, 0.55))])
                except Exception:
                    pass

        annotate_events_on_bg(ax1, entries, bolus_big, carbs)

        # Mini boluses
        if bolus_mini and not entries.empty:
            ymin, ymax = ax1.get_ylim()
            y_tri = ymin + (ymax - ymin) * 0.02
            t = [b["time"] for b in bolus_mini]
            ax1.scatter(t, [y_tri] * len(t), marker='v', s=30, color="#1f77b4", alpha=0.9)

        # Shared vertical cursor
        vline_color = "#888"
        vline1 = ax1.axvline(x=now, color=vline_color, alpha=0.6, lw=0.8)
        vline2 = ax2.axvline(x=now, color=vline_color, alpha=0.6, lw=0.8)
        time_ta = TextArea("", textprops=dict(color="#e0e0e0", fontsize=10))
        bg_ta = TextArea("", textprops=dict(color="#e0e0e0", fontsize=10))
        basal_ta = TextArea("", textprops=dict(color="#e0e0e0", fontsize=10))
        event_ta = TextArea("", textprops=dict(color="#e0e0e0", fontsize=10))
        vpack = VPacker(children=[time_ta, bg_ta, basal_ta, event_ta], align="left", pad=0, sep=2)
        info_box = AnchoredOffsetbox(loc='lower left', child=vpack, pad=0.4, frameon=True, bbox_to_anchor=(0.02, 0.02), bbox_transform=ax1.transAxes, borderpad=0.4)
        info_box.patch.set_facecolor('#222'); info_box.patch.set_edgecolor('#666'); info_box.patch.set_alpha(0.9)
        ax1.add_artist(info_box)

        def _update_cursor(event):
            if not event.inaxes:
                return
            try:
                xdt = mdates.num2date(event.xdata)
            except Exception:
                return
            for vl in (vline1, vline2):
                vl.set_xdata([event.xdata, event.xdata])
            try:
                if xdt.tzinfo is None:
                    xdt = xdt.replace(tzinfo=dt.timezone.utc)
                t_local_dt = xdt.astimezone()
            except Exception:
                return
            t = pd.Timestamp(t_local_dt)
            y_bg = None
            if not entries.empty:
                try:
                    pos = entries.index.get_indexer([t], method='nearest')[0]
                    if pos != -1:
                        y_bg = float(entries['mmol'].iloc[pos])
                except Exception:
                    pass
            y_plan = y_act = None
            if not basal_series.empty:
                try:
                    pos2 = basal_series.index.get_indexer([t], method='nearest')[0]
                    if pos2 != -1:
                        y_plan = float(basal_series['plan_u_per_h'].iloc[pos2])
                        y_act = float(basal_series['actual_u_per_h'].iloc[pos2])
                except Exception:
                    pass
            time_ta.set_text(f"Zeit: {t.strftime('%H:%M')}")
            bg_ta.set_text(f"BG: {y_bg:.1f} mmol/L" if y_bg is not None else "")
            basal_ta.set_text(f"Basal: {y_act:.2f} U/h (Plan {y_plan:.2f})" if (y_plan is not None and y_act is not None) else "")
            try:
                window_s = 5 * 60
                t_naive = pd.Timestamp(t).tz_localize(None)
                def _nearest_event():
                    cand: List[Tuple[float, Tuple[str, str, str]]] = []
                    for b in bolus_big or []:
                        dt_s = abs((pd.Timestamp(b['time']).tz_localize(None) - t_naive).total_seconds())
                        if dt_s <= window_s:
                            cand.append((dt_s, ("bolus", f"Bolus {b['units']:.2f} IE", "#d62728")))
                    for m in bolus_mini or []:
                        dt_s = abs((pd.Timestamp(m['time']).tz_localize(None) - t_naive).total_seconds())
                        if dt_s <= window_s:
                            cand.append((dt_s, ("smb", f"SMB {m['units']:.2f} IE", "#1f77b4")))
                    for c in carbs or []:
                        dt_s = abs((pd.Timestamp(c['time']).tz_localize(None) - t_naive).total_seconds())
                        if dt_s <= window_s:
                            grams = int(round(c.get('grams') or 0))
                            cand.append((dt_s, ("carbs", f"Carbs {grams} g", "#2ca02c")))
                    if not cand:
                        return None
                    cand.sort(key=lambda x: x[0])
                    return cand[0][1]
                ev = _nearest_event()
                if ev:
                    _type, _text, _color = ev
                    event_ta.set_text(_text)
                    event_ta._text.set_color(_color)
                else:
                    event_ta.set_text("")
            except Exception:
                pass
            fig.canvas.draw_idle()

        # (Re)connect cursor handler
        try:
            if getattr(fig.canvas, "_cursor_cid", None) is not None:
                fig.canvas.mpl_disconnect(fig.canvas._cursor_cid)
        except Exception:
            pass
        fig.canvas._cursor_cid = fig.canvas.mpl_connect('motion_notify_event', _update_cursor)

        # Keep a thin secondary axis only for layout consistency (no labels)
        ax2r = ax2.twinx(); ax2r.set_yticks([]); ax2r.set_ylabel("")

        # Legends
        legend_handles: List[Any] = []
        legend_labels: List[str] = []
        if not entries.empty:
            legend_handles.append(Line2D([0], [0], color="#1f77b4", linewidth=2.0))
            legend_labels.append("Glukose")
            legend_handles.append(Patch(facecolor="#2a3b4d", edgecolor="none", alpha=0.35))
            legend_labels.append("Zielbereich")
        if not basal_series.empty:
            legend_handles.append(Line2D([0], [0], color="#555555", linestyle="--", linewidth=1.0))
            legend_labels.append("Plan-Basal")
            legend_handles.append(Line2D([0], [0], color="#2ca02c", linewidth=1.8))
            legend_labels.append("Basal")
            if temp_legend_added:
                legend_handles.append(Patch(facecolor="#ffdd80", edgecolor="none", alpha=0.25))
                legend_labels.append("Temp-Basal")
        if legend_handles:
            ax1.legend(legend_handles, legend_labels, loc="upper right", framealpha=0.9)

        # X formatting
        ax2.set_xlabel("Zeit")
        ax2.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        x_start = entries.index[0] if not entries.empty else (now - dt.timedelta(minutes=WINDOW_MIN)).astimezone()
        ax2.set_xlim([x_start, now.astimezone()])
        fig.autofmt_xdate(rotation=0)

        # Footer info
        info = []
        if reservoir:
            info.append(f"Reservoir: {reservoir}")
        if pump_batt:
            info.append(f"Pumpe: {pump_batt}")
        if uploader_batt:
            info.append(f"Uploader: {uploader_batt}")
        # Append sensor/site age info if available
        def _fmt_age(mins: Optional[int]) -> Optional[str]:
            if mins is None or mins < 0:
                return None
            d = mins // 1440
            h = (mins % 1440) // 60
            if d >= 1:
                return f"{d}d {h}h"
            return f"{h}h"
        sa = _fmt_age(sensor_age_min)
        if sa:
            info.append(f"Sensor: {sa}")
        if info:
            fig.text(0.01, 0.01, " | ".join(info), ha="left", va="bottom", fontsize=9)

        fig.suptitle("Nightscout Dashboard (6h)")
        plt.tight_layout(rect=[0, 0.03, 1, 0.95])

        # Note: PNG saving removed per request

    # Initial render
    render_once()

    # Auto-refresh every minute using Matplotlib animation
    anim = animation.FuncAnimation(fig, lambda _frame: render_once(), interval=60000, cache_frame_data=False)

    plt.show()

    # HTML/Plotly Export wurde entfernt: Interaktivität jetzt direkt in Matplotlib (mplcursors)


if __name__ == "__main__":
    main()
