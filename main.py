"""
TramTram Bot – Multi-user real-time GTT Turin public transport monitoring.

Polls the Muoversi a Torino OpenTripPlanner API every 15 seconds and keeps
Telegram messages updated in-place with live arrival times.

Architecture
------------
- Global config (OTP URL, polling interval, night pause) lives in an optional
  config.json; sane defaults are used when the file is absent.
- Per-user data (trips + message state) is stored in  data/<chat_id>.json  so
  every Telegram user sees only their own dashboard.
- The bot token is read from the BOT_TOKEN environment variable (.env file).

Commands
--------
/start    – Clean up old messages and (re)create the live dashboard.
/add      – Guided wizard to add a trip or combo.
/remove   – Guided wizard to delete a trip or combo.
/cancel   – Abort the current wizard.
/refresh  – Force an immediate dashboard update.
<number>  – Show live arrivals at a stop for 15 minutes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, TimedOut, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

# ────────────────────────────────────────────────────────────────────────────
# Logging
# ────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tramtram")

# ────────────────────────────────────────────────────────────────────────────
# Constants & paths
# ────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
LEGACY_STATE_PATH = BASE_DIR / "state.json"

MAX_ARRIVALS = 3                       # arrivals shown per line
STOP_TTL_SECONDS = 15 * 60            # live stop messages last 15 min
DEFAULT_UPDATE_INTERVAL = 15           # seconds between dashboard refreshes

DEFAULT_OTP_URL = "https://plan.muoversiatorino.it/otp/routers/mato/index"

# ────────────────────────────────────────────────────────────────────────────
# Italy timezone  (CET / CEST, DST-aware without pytz)
# ────────────────────────────────────────────────────────────────────────────

def now_rome() -> datetime:
    """Return current wall-clock time in Europe/Rome.

    EU DST rule: clocks spring forward on the last Sunday of March at 01:00 UTC
    and fall back on the last Sunday of October at 01:00 UTC.
    """
    utc_now = datetime.now(timezone.utc)
    year = utc_now.year

    # Last Sunday of March
    mar31 = datetime(year, 3, 31, tzinfo=timezone.utc)
    dst_start = mar31 - timedelta(days=(mar31.weekday() + 1) % 7)
    dst_start = dst_start.replace(hour=1, minute=0, second=0, microsecond=0)

    # Last Sunday of October
    oct31 = datetime(year, 10, 31, tzinfo=timezone.utc)
    dst_end = oct31 - timedelta(days=(oct31.weekday() + 1) % 7)
    dst_end = dst_end.replace(hour=1, minute=0, second=0, microsecond=0)

    offset = timedelta(hours=2) if dst_start <= utc_now < dst_end else timedelta(hours=1)
    return utc_now.astimezone(timezone(offset))

# ────────────────────────────────────────────────────────────────────────────
# Per-user data persistence
#
# Each user's trips and runtime state (tracked message IDs) are stored in a
# single JSON file:  data/<chat_id>.json
# ────────────────────────────────────────────────────────────────────────────

def _user_path(chat_id: int) -> Path:
    return DATA_DIR / f"{chat_id}.json"


def _default_user_data() -> dict:
    return {
        "trips": [],
        "state": {
            "dashboard_msgs": [],      # message IDs for trip dashboard cards
            "stop_msgs": {},            # {msg_id_str: {stop_id, expires}}
            "all_msg_ids": [],          # every msg ID ever sent, for cleanup
        },
    }


def load_user_data(chat_id: int) -> dict:
    path = _user_path(chat_id)
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("trips", [])
            data.setdefault("state", {})
            data["state"].setdefault("dashboard_msgs", [])
            data["state"].setdefault("stop_msgs", {})
            data["state"].setdefault("all_msg_ids", [])
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return _default_user_data()


def save_user_data(chat_id: int, data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_user_path(chat_id), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_all_users() -> dict[int, dict]:
    """Scan the data/ directory and load every user's data into memory."""
    users: dict[int, dict] = {}
    if not DATA_DIR.exists():
        return users
    for path in DATA_DIR.glob("*.json"):
        try:
            chat_id = int(path.stem)
            users[chat_id] = load_user_data(chat_id)
        except (ValueError, json.JSONDecodeError):
            continue
    return users


def get_user(app: Application, chat_id: int) -> dict:
    """Return in-memory user data, loading from disk on first access."""
    users = app.bot_data.setdefault("users", {})
    if chat_id not in users:
        users[chat_id] = load_user_data(chat_id)
    return users[chat_id]

# ────────────────────────────────────────────────────────────────────────────
# Legacy migration  (single-user config.json + state.json → multi-user)
#
# The old format kept bot_token, chat_id, and trips together in config.json.
# This migrator moves trips + state into  data/<chat_id>.json  and rewrites
# config.json to contain only global settings.
# ────────────────────────────────────────────────────────────────────────────

def _maybe_migrate_legacy() -> None:
    if not CONFIG_PATH.exists():
        return
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            old_cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    # Only old-format configs have a nested "telegram" key with "chat_id"
    if "telegram" not in old_cfg or "chat_id" not in old_cfg.get("telegram", {}):
        return

    chat_id = old_cfg["telegram"]["chat_id"]
    trips = old_cfg.get("trips", [])

    old_state = _default_user_data()["state"]
    if LEGACY_STATE_PATH.exists():
        try:
            with open(LEGACY_STATE_PATH, encoding="utf-8") as f:
                sd = json.load(f)
            if "fermata_msgs" in sd:
                sd["stop_msgs"] = sd.pop("fermata_msgs")
            old_state = {
                "dashboard_msgs": sd.get("dashboard_msgs", []),
                "stop_msgs": sd.get("stop_msgs", {}),
                "all_msg_ids": sd.get("all_msg_ids", []),
            }
        except (json.JSONDecodeError, OSError):
            pass

    save_user_data(chat_id, {"trips": trips, "state": old_state})

    # Rewrite config.json with global-only keys
    new_cfg = {
        "otp_base_url": old_cfg.get("otp_base_url", DEFAULT_OTP_URL),
        "polling_interval_seconds": old_cfg.get("polling_interval_seconds", DEFAULT_UPDATE_INTERVAL),
        "night_pause": old_cfg.get("night_pause", {"start_hour": 2, "end_hour": 7}),
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(new_cfg, f, indent=2)

    if LEGACY_STATE_PATH.exists():
        LEGACY_STATE_PATH.unlink()

    logger.info("Migrated legacy single-user data for chat_id %d.", chat_id)

# ────────────────────────────────────────────────────────────────────────────
# Global configuration
#
# Optional config.json with OTP URL, polling interval, and night pause hours.
# Falls back to sensible defaults when the file is missing.
# ────────────────────────────────────────────────────────────────────────────

def load_global_config() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "otp_base_url": DEFAULT_OTP_URL,
        "polling_interval_seconds": DEFAULT_UPDATE_INTERVAL,
        "night_pause": {"start_hour": 2, "end_hour": 7},
    }
    if not CONFIG_PATH.exists():
        return defaults
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return defaults
    # night_pause: false or null in config means no pause (24/7 updates)
    night_pause = cfg.get("night_pause", defaults["night_pause"])
    if night_pause is False or night_pause is None:
        night_pause = None
    elif not isinstance(night_pause, dict):
        night_pause = None

    return {
        "otp_base_url": cfg.get("otp_base_url", defaults["otp_base_url"]),
        "polling_interval_seconds": cfg.get("polling_interval_seconds", defaults["polling_interval_seconds"]),
        "night_pause": night_pause,
    }

# ────────────────────────────────────────────────────────────────────────────
# OTP API helpers
#
# All HTTP calls go through httpx.AsyncClient and talk to the Muoversi a
# Torino OpenTripPlanner instance.
# ────────────────────────────────────────────────────────────────────────────

async def fetch_stop_name(client: httpx.AsyncClient, sid: str, base: str) -> str:
    """Fetch the human-readable name for a GTT stop ID."""
    try:
        r = await client.get(f"{base}/stops/gtt:{sid}", timeout=10)
        if r.status_code == 200:
            return r.json().get("name", sid)
    except Exception as e:
        logger.warning("Stop name %s: %s", sid, e)
    return sid


async def fetch_stoptimes(client: httpx.AsyncClient, sid: str, base: str) -> list[dict]:
    """Fetch upcoming arrival patterns for a GTT stop ID."""
    try:
        r = await client.get(f"{base}/stops/gtt:{sid}/stoptimes", timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.warning("Stoptimes %s: %s", sid, e)
    return []


async def fetch_all_stops(
    stop_ids: set[str], base: str,
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Batch-fetch names and stoptimes for many stop IDs in parallel.

    Returns (stoptimes_map, name_map) where keys are stop ID strings.
    """
    async with httpx.AsyncClient() as client:
        tasks = []
        for sid in stop_ids:
            tasks.append(("st_" + sid, fetch_stoptimes(client, sid, base)))
            tasks.append(("nm_" + sid, fetch_stop_name(client, sid, base)))
        keys = [t[0] for t in tasks]
        results = await asyncio.gather(*(t[1] for t in tasks))
        st_map: dict[str, list[dict]] = {}
        nm_map: dict[str, str] = {}
        for key, res in zip(keys, results):
            sid = key[3:]
            if key.startswith("st_"):
                st_map[sid] = res
            else:
                nm_map[sid] = res
    return st_map, nm_map


async def fetch_stop_data(sid: str, base: str) -> tuple[str, list[dict]]:
    """Fetch name + stoptimes for a single stop (used by quick stop queries)."""
    async with httpx.AsyncClient() as client:
        name, patterns = await asyncio.gather(
            fetch_stop_name(client, sid, base),
            fetch_stoptimes(client, sid, base),
        )
    return name, patterns

# ────────────────────────────────────────────────────────────────────────────
# Arrival extraction
#
# OTP returns "patterns" (route+direction) each containing "times" with
# scheduled/realtime arrival timestamps.  We filter by line name, compute
# minutes-from-now, and return the closest N arrivals.
# ────────────────────────────────────────────────────────────────────────────

def _route_from_pattern(pid: str) -> str:
    """Extract the bare route number from an OTP pattern ID.

    Pattern IDs look like  "gtt:42U"  or  "gtt:16CDU".  We strip the
    directional suffixes (U, E, SU, CSU, CDU) to get the route number.
    """
    parts = pid.split(":")
    if len(parts) < 2:
        return ""
    rp = parts[1]
    for sfx in ("CDU", "CSU", "SU", "U", "E"):
        if rp.upper().endswith(sfx):
            return rp[: -len(sfx)]
    return rp


def extract_arrivals(patterns: list[dict], line: str, now_ts: int) -> list[dict]:
    """Return the next MAX_ARRIVALS arrivals for a specific line at a stop."""
    ll = line.strip().lower()
    out: list[dict] = []
    for p in patterns:
        if _route_from_pattern(p.get("pattern", {}).get("id", "")).lower() != ll:
            continue
        for t in p.get("times", []):
            arr_ts = t.get("serviceDay", 0) + t.get("realtimeArrival", t.get("scheduledArrival", 0))
            if arr_ts <= now_ts:
                continue
            out.append({
                "minutes": (arr_ts - now_ts) // 60,
                "headsign": t.get("headsign", "?"),
                "realtime": t.get("realtime", False),
            })
    out.sort(key=lambda x: x["minutes"])
    return out[:MAX_ARRIVALS]


def extract_all_arrivals(patterns: list[dict], now_ts: int) -> list[dict]:
    """Return all upcoming arrivals across every line at a stop."""
    out: list[dict] = []
    for p in patterns:
        rn = _route_from_pattern(p.get("pattern", {}).get("id", ""))
        for t in p.get("times", []):
            arr_ts = t.get("serviceDay", 0) + t.get("realtimeArrival", t.get("scheduledArrival", 0))
            if arr_ts <= now_ts:
                continue
            out.append({
                "line": rn,
                "minutes": (arr_ts - now_ts) // 60,
                "headsign": t.get("headsign", "?"),
                "realtime": t.get("realtime", False),
            })
    out.sort(key=lambda x: x["minutes"])
    return out


def collect_all_stop_ids(trips: list[dict]) -> set[str]:
    """Gather every unique stop ID referenced across all trips."""
    ids: set[str] = set()
    for trip in trips:
        for combo in trip["combos"]:
            for leg in combo["legs"]:
                ids.add(str(leg["stop_id_boarding"]))
                ids.add(str(leg["stop_id_alighting"]))
    return ids

# ────────────────────────────────────────────────────────────────────────────
# Telegram message formatting
#
# Outputs Markdown (V1) strings for display in Telegram.  Stop/destination
# names come from the API and are never translated.
# ────────────────────────────────────────────────────────────────────────────

def _fmt_arrival(a: dict) -> str:
    """Format a single arrival: green dot for realtime, plain for scheduled."""
    m = a["minutes"]
    base = "now!" if m == 0 else f"{m}'"
    return f"🟢{base}" if a["realtime"] else base


def _esc(t: str) -> str:
    """Escape Markdown V1 special characters in user/API-provided text."""
    return t.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")


def format_trip(
    trip: dict,
    st_map: dict[str, list[dict]],
    nm_map: dict[str, str],
    updated: datetime,
) -> str:
    """Build the dashboard card for one trip (one Telegram message)."""
    now_ts = int(updated.timestamp())
    parts: list[str] = []
    parts.append(f"🚋  *{_esc(trip['name'])}*")
    parts.append(f"⏱  {updated.strftime('%H:%M:%S')}")

    for combo in trip["combos"]:
        parts.append("")
        parts.append(f"━━━  _{_esc(combo['name'])}_  ━━━")
        parts.append("")
        for leg in combo["legs"]:
            line = leg["line"]
            s_board = str(leg["stop_id_boarding"])
            s_alight = str(leg["stop_id_alighting"])
            n_board = nm_map.get(s_board, s_board)
            n_alight = nm_map.get(s_alight, s_alight)
            pats = st_map.get(s_board, [])
            arrs = extract_arrivals(pats, line, now_ts)

            parts.append(f"  🚌  *{line}*")
            parts.append(f"        {_esc(n_board)}  ➜  {_esc(n_alight)}")
            if arrs:
                times = "   ".join(_fmt_arrival(a) for a in arrs)
                parts.append(f"        ⏳  *{times}*")
            else:
                parts.append("        ⏳  _no upcoming arrivals_")

    return "\n".join(parts)


def format_stop(
    stop_name: str, stop_id: str,
    patterns: list[dict], updated: datetime,
    expires_in_min: int | None = None,
) -> str:
    """Build the live-stop info card (all lines at one stop)."""
    now_ts = int(updated.timestamp())
    arrivals = extract_all_arrivals(patterns, now_ts)

    parts: list[str] = []
    parts.append(f"🚏  *{_esc(stop_name)}*  (`{stop_id}`)")
    parts.append(f"⏱  {updated.strftime('%H:%M:%S')}")
    if expires_in_min is not None and expires_in_min > 0:
        parts.append(f"⏳  _expires in {expires_in_min} min_")
    parts.append("")

    if arrivals:
        by_line: dict[str, list[dict]] = {}
        for a in arrivals:
            by_line.setdefault(a["line"], []).append(a)
        for line, arrs in sorted(by_line.items()):
            top = arrs[:MAX_ARRIVALS]
            dest = top[0]["headsign"]
            times = "   ".join(_fmt_arrival(a) for a in top)
            parts.append(f"  🚌  *{line}*  ➜  {_esc(dest)}")
            parts.append(f"        ⏳  *{times}*")
            parts.append("")
    else:
        parts.append("_No arrivals_")
        parts.append("")

    return "\n".join(parts)

# ────────────────────────────────────────────────────────────────────────────
# Per-user message tracking
#
# Every message the bot sends or receives is recorded in the user's state so
# it can be cleaned up later (on /start or bot restart).
# ────────────────────────────────────────────────────────────────────────────

def _remember_msg(app: Application, chat_id: int, msg_id: int) -> None:
    user = get_user(app, chat_id)
    state = user["state"]
    if msg_id not in state["all_msg_ids"]:
        state["all_msg_ids"].append(msg_id)
    save_user_data(chat_id, user)


def track_dashboard_msgs(app: Application, chat_id: int, mids: list[int]) -> None:
    user = get_user(app, chat_id)
    state = user["state"]
    state["dashboard_msgs"] = mids
    for m in mids:
        if m not in state["all_msg_ids"]:
            state["all_msg_ids"].append(m)
    save_user_data(chat_id, user)


def track_stop(app: Application, chat_id: int, msg_id: int, stop_id: str) -> None:
    user = get_user(app, chat_id)
    state = user["state"]
    state["stop_msgs"][str(msg_id)] = {
        "stop_id": stop_id,
        "expires": time.time() + STOP_TTL_SECONDS,
    }
    if msg_id not in state["all_msg_ids"]:
        state["all_msg_ids"].append(msg_id)
    save_user_data(chat_id, user)


def untrack_stop(app: Application, chat_id: int, msg_id: int) -> None:
    user = get_user(app, chat_id)
    user["state"].get("stop_msgs", {}).pop(str(msg_id), None)
    save_user_data(chat_id, user)

# ────────────────────────────────────────────────────────────────────────────
# Telegram message helpers
# ────────────────────────────────────────────────────────────────────────────

async def _try_delete(app: Application, chat_id: int, mid: int) -> None:
    """Silently attempt to delete a message; ignore failures."""
    try:
        await app.bot.delete_message(chat_id=chat_id, message_id=mid)
    except (BadRequest, TimedOut, NetworkError):
        pass


async def nuke_user_chat(app: Application, chat_id: int) -> None:
    """Delete every known bot message for this user and reset their state."""
    user = get_user(app, chat_id)
    state = user["state"]
    all_ids = list(state.get("all_msg_ids", []))

    for mid in all_ids:
        await _try_delete(app, chat_id, mid)
    for mid in state.get("dashboard_msgs", []):
        if mid and mid not in all_ids:
            await _try_delete(app, chat_id, mid)
    for key in list(state.get("stop_msgs", {}).keys()):
        mid = int(key)
        if mid not in all_ids:
            await _try_delete(app, chat_id, mid)

    state["dashboard_msgs"] = []
    state["stop_msgs"] = {}
    state["all_msg_ids"] = []
    save_user_data(chat_id, user)
    logger.info("Chat %d cleaned (%d messages).", chat_id, len(all_ids))

# ────────────────────────────────────────────────────────────────────────────
# Background update loop
#
# Runs continuously in an asyncio task.  Every cycle it:
#   1. Collects all stop IDs needed across ALL users (deduplication).
#   2. Fetches stoptimes + names in one parallel batch.
#   3. Edits each user's dashboard and stop messages with fresh data.
#   4. Deletes expired stop messages.
# ────────────────────────────────────────────────────────────────────────────

async def updater_loop(app: Application) -> None:
    gcfg = app.bot_data["global_config"]
    base: str = gcfg["otp_base_url"]
    interval: int = gcfg.get("polling_interval_seconds", DEFAULT_UPDATE_INTERVAL)
    night_pause = gcfg.get("night_pause")

    while True:
        try:
            now = now_rome()
            # Skip updates during night_pause window (if configured)
            if night_pause:
                night_s = night_pause.get("start_hour", 2)
                night_e = night_pause.get("end_hour", 7)
                if night_s <= now.hour < night_e:
                    await asyncio.sleep(interval)
                    continue

            users: dict[int, dict] = app.bot_data.get("users", {})
            if not users:
                await asyncio.sleep(interval)
                continue

            # Collect every stop ID that any user needs updated
            all_sids: set[str] = set()
            active_users: list[tuple[int, dict]] = []

            for chat_id, udata in list(users.items()):
                state = udata.get("state", {})
                has_dashboard = bool(state.get("dashboard_msgs"))
                has_stops = bool(state.get("stop_msgs"))
                if not has_dashboard and not has_stops:
                    continue
                active_users.append((chat_id, udata))
                if has_dashboard:
                    all_sids |= collect_all_stop_ids(udata.get("trips", []))
                for info in state.get("stop_msgs", {}).values():
                    sid = info if isinstance(info, str) else info.get("stop_id", "")
                    if sid:
                        all_sids.add(sid)

            if not all_sids:
                await asyncio.sleep(interval)
                continue

            # Single batch fetch shared across all users
            st_map, nm_map = await fetch_all_stops(all_sids, base)

            for chat_id, udata in active_users:
                await _update_user(app, chat_id, udata, st_map, nm_map, now)

        except Exception as e:
            logger.error("Update loop error: %s", e)

        await asyncio.sleep(interval)


async def _update_user(
    app: Application, chat_id: int, udata: dict,
    st_map: dict[str, list[dict]], nm_map: dict[str, str],
    now: datetime,
) -> None:
    """Push fresh data into one user's dashboard and stop messages."""
    state = udata.get("state", {})
    trips = udata.get("trips", [])

    # ── Dashboard messages ──
    d_msgs = state.get("dashboard_msgs", [])
    if d_msgs:
        changed = False
        for i, mid in enumerate(d_msgs):
            if not mid or i >= len(trips):
                continue
            text = format_trip(trips[i], st_map, nm_map, now)
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id, message_id=mid,
                    text=text, parse_mode="Markdown",
                )
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    pass
                elif "not found" in str(e).lower():
                    d_msgs[i] = None
                    changed = True
                else:
                    logger.error("Edit trip %d for %d: %s", i, chat_id, e)
            except (TimedOut, NetworkError) as e:
                logger.warning("Network trip %d for %d: %s", i, chat_id, e)
        if changed:
            state["dashboard_msgs"] = d_msgs
            save_user_data(chat_id, udata)

    # ── Stop messages + expiry ──
    s_msgs = dict(state.get("stop_msgs", {}))
    now_unix = time.time()
    expired: list[int] = []

    for mid_s, info in s_msgs.items():
        mid = int(mid_s)
        if isinstance(info, str):
            stop_id = info
            exp = now_unix + STOP_TTL_SECONDS
        else:
            stop_id = info.get("stop_id", "")
            exp = info.get("expires", now_unix + STOP_TTL_SECONDS)

        if now_unix >= exp:
            expired.append(mid)
            continue

        exp_min = max(1, int((exp - now_unix) / 60))
        stop_name = nm_map.get(stop_id, stop_id)
        pats = st_map.get(stop_id, [])
        text = format_stop(stop_name, stop_id, pats, now, exp_min)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛑 STOP", callback_data=f"stop_{mid}")]
        ])
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id, message_id=mid,
                text=text, parse_mode="Markdown",
                reply_markup=kb,
            )
        except BadRequest as e:
            if "not modified" in str(e).lower():
                pass
            elif "not found" in str(e).lower():
                expired.append(mid)
            else:
                logger.error("Edit stop %s for %d: %s", mid, chat_id, e)
        except (TimedOut, NetworkError):
            pass

    for mid in expired:
        await _try_delete(app, chat_id, mid)
        untrack_stop(app, chat_id, mid)
        logger.info("Stop msg %s expired for %d, removed.", mid, chat_id)

# ────────────────────────────────────────────────────────────────────────────
# Telegram commands
# ────────────────────────────────────────────────────────────────────────────

# Introductory message when the user has no trips yet (shown on /start).
WELCOME_TEXT = (
    "🚋 *Welcome to TramTram!*\n\n"
    "This bot shows *real-time arrivals* for GTT buses and trams in Turin, Italy. "
    "Messages update automatically every 15 seconds.\n\n"
    "*Get started*\n"
    "Use /add to create your first trip: you choose a name (e.g. _Home → Office_), "
    "then add one or more routes (line + boarding and alighting stop IDs). "
    "You can also send a *stop number* in chat to see all arrivals at that stop for 15 minutes.\n\n"
    "*Commands*\n"
    "• /add — add a trip or route\n"
    "• /remove — remove a trip or route\n"
    "• /start — show this message or rebuild your dashboard\n"
    "• /refresh — update the dashboard now\n"
    "• Send a *number* — live arrivals at that stop (15 min)\n\n"
    "Stop IDs can be found on [Muoversi a Torino](https://www.muoversiatorino.it/) "
    "or by sending a number to the bot and checking the result."
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Clean up old messages and (re)create the dashboard for this user."""
    chat_id = update.effective_chat.id
    gcfg = ctx.application.bot_data["global_config"]
    base = gcfg["otp_base_url"]
    user = get_user(ctx.application, chat_id)
    trips = user["trips"]

    await nuke_user_chat(ctx.application, chat_id)

    if not trips:
        msg = await ctx.bot.send_message(
            chat_id=chat_id, text=WELCOME_TEXT, parse_mode="Markdown",
        )
        _remember_msg(ctx.application, chat_id, msg.message_id)
        logger.info("/start for %d (no trips yet).", chat_id)
        return

    all_sids = collect_all_stop_ids(trips)
    st_map, nm_map = await fetch_all_stops(all_sids, base)
    now = now_rome()

    mids: list[int] = []
    for trip in trips:
        text = format_trip(trip, st_map, nm_map, now)
        msg = await ctx.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        mids.append(msg.message_id)

    track_dashboard_msgs(ctx.application, chat_id, mids)
    logger.info("/start for %d → %d messages.", chat_id, len(mids))


async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-update the dashboard right now (edits existing messages)."""
    chat_id = update.effective_chat.id
    gcfg = ctx.application.bot_data["global_config"]
    base = gcfg["otp_base_url"]
    user = get_user(ctx.application, chat_id)
    trips = user["trips"]
    state = user["state"]

    if update.message:
        _remember_msg(ctx.application, chat_id, update.message.message_id)
        await _try_delete(ctx.application, chat_id, update.message.message_id)

    if not trips:
        return

    all_sids = collect_all_stop_ids(trips)
    st_map, nm_map = await fetch_all_stops(all_sids, base)
    now = now_rome()

    d_msgs = state.get("dashboard_msgs", [])
    if d_msgs:
        for i, mid in enumerate(d_msgs):
            if not mid or i >= len(trips):
                continue
            text = format_trip(trips[i], st_map, nm_map, now)
            try:
                await ctx.bot.edit_message_text(
                    chat_id=chat_id, message_id=mid,
                    text=text, parse_mode="Markdown",
                )
            except BadRequest:
                pass
    else:
        mids: list[int] = []
        for trip in trips:
            text = format_trip(trip, st_map, nm_map, now)
            msg = await ctx.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            mids.append(msg.message_id)
        track_dashboard_msgs(ctx.application, chat_id, mids)

    logger.info("/refresh for %d ok.", chat_id)


async def handle_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A plain number in chat → live stop query (auto-updated for 15 min)."""
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if not text.isdigit():
        return

    chat_id = update.effective_chat.id
    gcfg = ctx.application.bot_data["global_config"]
    base = gcfg["otp_base_url"]
    stop_id = text

    _remember_msg(ctx.application, chat_id, update.message.message_id)
    await _try_delete(ctx.application, chat_id, update.message.message_id)

    stop_name, patterns = await fetch_stop_data(stop_id, base)
    now = now_rome()
    exp_min = STOP_TTL_SECONDS // 60
    body = format_stop(stop_name, stop_id, patterns, now, exp_min)

    # Send with a placeholder callback, then patch with the real message ID
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 STOP", callback_data="stop_PLACEHOLDER")]
    ])
    msg = await ctx.bot.send_message(
        chat_id=chat_id, text=body, parse_mode="Markdown", reply_markup=kb,
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 STOP", callback_data=f"stop_{msg.message_id}")]
    ])
    await msg.edit_reply_markup(reply_markup=kb)

    track_stop(ctx.application, chat_id, msg.message_id, stop_id)
    logger.info("Stop %s for %d → msg %s (15 min).", stop_id, chat_id, msg.message_id)


async def callback_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the STOP button press: delete the stop message and untrack it."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    if not data.startswith("stop_"):
        return
    mid = int(data.split("_", 1)[1])
    if not query.message:
        return
    chat_id = query.message.chat.id
    await _try_delete(ctx.application, chat_id, mid)
    untrack_stop(ctx.application, chat_id, mid)
    logger.info("STOP → msg %s for %d deleted.", mid, chat_id)

# ────────────────────────────────────────────────────────────────────────────
# Dashboard rebuild  (used after adding/removing trips)
# ────────────────────────────────────────────────────────────────────────────

async def rebuild_dashboard(app: Application, chat_id: int) -> None:
    """Delete old dashboard messages and send fresh ones."""
    gcfg = app.bot_data["global_config"]
    base = gcfg["otp_base_url"]
    user = get_user(app, chat_id)
    trips = user["trips"]
    state = user["state"]

    for old in state.get("dashboard_msgs", []):
        if old:
            await _try_delete(app, chat_id, old)

    if not trips:
        state["dashboard_msgs"] = []
        save_user_data(chat_id, user)
        return

    all_sids = collect_all_stop_ids(trips)
    st_map, nm_map = await fetch_all_stops(all_sids, base)
    now = now_rome()

    mids: list[int] = []
    for trip in trips:
        text = format_trip(trip, st_map, nm_map, now)
        msg = await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        mids.append(msg.message_id)
    track_dashboard_msgs(app, chat_id, mids)

# ────────────────────────────────────────────────────────────────────────────
# Wizard: shared helpers
#
# Both /add and /remove use a single-message editing pattern: the wizard
# sends one message and edits it in place at each step.  All incoming user
# messages are deleted to keep the chat clean.
# ────────────────────────────────────────────────────────────────────────────

# Conversation states for /add
WIZ_CHOOSE, WIZ_TRIP_NAME, WIZ_COMBO_NAME = range(3)
WIZ_LINE, WIZ_BOARDING, WIZ_ALIGHTING, WIZ_MORE = range(3, 7)

# Conversation states for /remove
DEL_TRIP, DEL_WHAT = range(7, 9)

_CANCEL_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("❌ Cancel", callback_data="wiz_cancel")]]
)


async def _wiz_msg(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str,
                   kb: InlineKeyboardMarkup | None = None) -> None:
    """Send or edit the wizard's persistent message."""
    wiz_mid = ctx.user_data.get("wiz_mid")
    if wiz_mid:
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id, message_id=wiz_mid,
                text=text, parse_mode="Markdown", reply_markup=kb,
            )
            return
        except BadRequest:
            pass
    msg = await ctx.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb,
    )
    ctx.user_data["wiz_mid"] = msg.message_id
    _remember_msg(ctx.application, chat_id, msg.message_id)


async def _wiz_cleanup(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Delete the wizard message when the conversation ends."""
    wiz_mid = ctx.user_data.pop("wiz_mid", None)
    if wiz_mid:
        await _try_delete(ctx.application, chat_id, wiz_mid)


async def _del_user_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the user's incoming message to keep the chat tidy."""
    if update.message:
        chat_id = update.effective_chat.id
        _remember_msg(ctx.application, chat_id, update.message.message_id)
        await _try_delete(ctx.application, chat_id, update.message.message_id)


def _wiz_summary(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """Build a progress summary for the /add wizard showing legs added so far."""
    v = ctx.user_data.get("wiz_trip_name", "")
    c = ctx.user_data.get("wiz_combo_name", "")
    legs = ctx.user_data.get("wiz_legs", [])
    lines = [f"📝 *{_esc(v)}* ➜ _{_esc(c)}_", ""]
    if legs:
        lines.append("Legs:")
        for leg in legs:
            lines.append(f"  🚌 *{leg['line']}*: `{leg['stop_id_boarding']}` → `{leg['stop_id_alighting']}`")
        lines.append("")
    return "\n".join(lines)

# ────────────────────────────────────────────────────────────────────────────
# /add wizard  –  guided flow to create a new trip or add a combo
# ────────────────────────────────────────────────────────────────────────────

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user = get_user(ctx.application, chat_id)
    await _del_user_msg(update, ctx)
    ctx.user_data["wiz_legs"] = []
    ctx.user_data["wiz_trip_idx"] = None
    ctx.user_data["wiz_trip_name"] = None

    trips = user["trips"]
    buttons = []
    for i, trip in enumerate(trips):
        buttons.append([InlineKeyboardButton(trip["name"], callback_data=f"addv_{i}")])
    buttons.append([InlineKeyboardButton("➕ New trip", callback_data="addv_new")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="wiz_cancel")])

    await _wiz_msg(ctx, chat_id,
                   "📝 *Add combo*\n\nWhere do you want to add it?",
                   InlineKeyboardMarkup(buttons))
    return WIZ_CHOOSE


async def wiz_choose_trip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return WIZ_CHOOSE
    await query.answer()
    data = query.data or ""
    chat_id = update.effective_chat.id
    user = get_user(ctx.application, chat_id)

    if data == "wiz_cancel":
        await _wiz_cleanup(ctx, chat_id)
        return ConversationHandler.END

    if data == "addv_new":
        await _wiz_msg(ctx, chat_id,
                       "📝 *New trip*\n\nWhat do you want to call it?\n_e.g. Home → Office_",
                       _CANCEL_KB)
        return WIZ_TRIP_NAME

    idx = int(data.split("_")[1])
    ctx.user_data["wiz_trip_idx"] = idx
    ctx.user_data["wiz_trip_name"] = user["trips"][idx]["name"]
    await _wiz_msg(ctx, chat_id,
                   f"📝 Add combo to *{_esc(user['trips'][idx]['name'])}*\n\nCombo name?\n_e.g. Direct 42_",
                   _CANCEL_KB)
    return WIZ_COMBO_NAME


async def wiz_trip_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        if (update.callback_query.data or "") == "wiz_cancel":
            chat_id = update.effective_chat.id
            await _wiz_cleanup(ctx, chat_id)
            return ConversationHandler.END
        return WIZ_TRIP_NAME

    await _del_user_msg(update, ctx)
    if not update.message or not update.message.text:
        return WIZ_TRIP_NAME
    name = update.message.text.strip()
    if not name:
        return WIZ_TRIP_NAME

    chat_id = update.effective_chat.id
    ctx.user_data["wiz_trip_name"] = name
    ctx.user_data["wiz_trip_idx"] = None
    await _wiz_msg(ctx, chat_id,
                   f"📝 *{_esc(name)}*\n\nCombo name?\n_e.g. Direct 42_",
                   _CANCEL_KB)
    return WIZ_COMBO_NAME


async def wiz_combo_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        if (update.callback_query.data or "") == "wiz_cancel":
            chat_id = update.effective_chat.id
            await _wiz_cleanup(ctx, chat_id)
            return ConversationHandler.END
        return WIZ_COMBO_NAME

    await _del_user_msg(update, ctx)
    if not update.message or not update.message.text:
        return WIZ_COMBO_NAME
    name = update.message.text.strip()
    if not name:
        return WIZ_COMBO_NAME

    chat_id = update.effective_chat.id
    ctx.user_data["wiz_combo_name"] = name
    trip_name = ctx.user_data["wiz_trip_name"]
    await _wiz_msg(ctx, chat_id,
                   f"📝 *{_esc(trip_name)}* ➜ _{_esc(name)}_\n\nLine?\n_e.g. 42_",
                   _CANCEL_KB)
    return WIZ_LINE


async def wiz_line(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        if (update.callback_query.data or "") == "wiz_cancel":
            chat_id = update.effective_chat.id
            await _wiz_cleanup(ctx, chat_id)
            return ConversationHandler.END
        return WIZ_LINE

    await _del_user_msg(update, ctx)
    if not update.message or not update.message.text:
        return WIZ_LINE
    line = update.message.text.strip()
    if not line:
        return WIZ_LINE

    chat_id = update.effective_chat.id
    ctx.user_data["wiz_current_line"] = line
    await _wiz_msg(ctx, chat_id,
                   _wiz_summary(ctx) + f"🚌 Line *{_esc(line)}*\n\nBoarding stop ID?",
                   _CANCEL_KB)
    return WIZ_BOARDING


async def wiz_boarding(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        if (update.callback_query.data or "") == "wiz_cancel":
            chat_id = update.effective_chat.id
            await _wiz_cleanup(ctx, chat_id)
            return ConversationHandler.END
        return WIZ_BOARDING

    await _del_user_msg(update, ctx)
    if not update.message or not update.message.text:
        return WIZ_BOARDING
    sid = update.message.text.strip()
    if not sid.isdigit():
        chat_id = update.effective_chat.id
        await _wiz_msg(ctx, chat_id,
                       _wiz_summary(ctx) + "⚠️ Please enter a valid number for the boarding stop ID.",
                       _CANCEL_KB)
        return WIZ_BOARDING

    chat_id = update.effective_chat.id
    ctx.user_data["wiz_current_boarding"] = sid
    line = ctx.user_data["wiz_current_line"]
    await _wiz_msg(ctx, chat_id,
                   _wiz_summary(ctx) + f"🚌 Line *{_esc(line)}* from `{sid}`\n\nAlighting stop ID?",
                   _CANCEL_KB)
    return WIZ_ALIGHTING


async def wiz_alighting(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        if (update.callback_query.data or "") == "wiz_cancel":
            chat_id = update.effective_chat.id
            await _wiz_cleanup(ctx, chat_id)
            return ConversationHandler.END
        return WIZ_ALIGHTING

    await _del_user_msg(update, ctx)
    if not update.message or not update.message.text:
        return WIZ_ALIGHTING
    sid = update.message.text.strip()
    if not sid.isdigit():
        chat_id = update.effective_chat.id
        await _wiz_msg(ctx, chat_id,
                       _wiz_summary(ctx) + "⚠️ Please enter a valid number for the alighting stop ID.",
                       _CANCEL_KB)
        return WIZ_ALIGHTING

    chat_id = update.effective_chat.id
    ctx.user_data["wiz_legs"].append({
        "line": ctx.user_data["wiz_current_line"],
        "stop_id_boarding": ctx.user_data["wiz_current_boarding"],
        "stop_id_alighting": sid,
    })

    summary = _wiz_summary(ctx) + "Add another leg?"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, add leg", callback_data="more_yes"),
         InlineKeyboardButton("💾 Save", callback_data="more_save")],
        [InlineKeyboardButton("❌ Cancel", callback_data="wiz_cancel")],
    ])
    await _wiz_msg(ctx, chat_id, summary, kb)
    return WIZ_MORE


async def wiz_more(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return WIZ_MORE
    await query.answer()
    data = query.data or ""
    chat_id = update.effective_chat.id

    if data == "wiz_cancel":
        await _wiz_cleanup(ctx, chat_id)
        return ConversationHandler.END

    if data == "more_yes":
        await _wiz_msg(ctx, chat_id,
                       _wiz_summary(ctx) + "Line?",
                       _CANCEL_KB)
        return WIZ_LINE

    # ── Save the new combo ──
    user = get_user(ctx.application, chat_id)
    combo = {
        "name": ctx.user_data["wiz_combo_name"],
        "legs": ctx.user_data["wiz_legs"],
    }
    trip_idx = ctx.user_data["wiz_trip_idx"]
    if trip_idx is not None:
        user["trips"][trip_idx]["combos"].append(combo)
    else:
        user["trips"].append({
            "name": ctx.user_data["wiz_trip_name"],
            "combos": [combo],
        })

    save_user_data(chat_id, user)
    await _wiz_msg(ctx, chat_id, "✅ *Saved!* Rebuilding dashboard…")
    await rebuild_dashboard(ctx.application, chat_id)
    await _wiz_cleanup(ctx, chat_id)
    return ConversationHandler.END

# ────────────────────────────────────────────────────────────────────────────
# /remove wizard  –  guided flow to delete a trip or combo
# ────────────────────────────────────────────────────────────────────────────

async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user = get_user(ctx.application, chat_id)
    await _del_user_msg(update, ctx)

    trips = user["trips"]
    if not trips:
        msg = await ctx.bot.send_message(chat_id=chat_id, text="No trips configured.")
        _remember_msg(ctx.application, chat_id, msg.message_id)
        return ConversationHandler.END

    buttons = []
    for i, trip in enumerate(trips):
        n_combos = len(trip["combos"])
        label = f"{trip['name']} ({n_combos} combo{'s' if n_combos != 1 else ''})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"delv_{i}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="wiz_cancel")])

    await _wiz_msg(ctx, chat_id,
                   "🗑 *Remove trip*\n\nWhich trip?",
                   InlineKeyboardMarkup(buttons))
    return DEL_TRIP


async def del_choose_trip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return DEL_TRIP
    await query.answer()
    data = query.data or ""
    chat_id = update.effective_chat.id
    user = get_user(ctx.application, chat_id)

    if data == "wiz_cancel":
        await _wiz_cleanup(ctx, chat_id)
        return ConversationHandler.END

    idx = int(data.split("_")[1])
    ctx.user_data["del_trip_idx"] = idx
    trip = user["trips"][idx]

    buttons = []
    for j, combo in enumerate(trip["combos"]):
        n_legs = len(combo["legs"])
        label = f"🗑 {combo['name']} ({n_legs} leg{'s' if n_legs != 1 else ''})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"delc_{j}")])
    buttons.append([InlineKeyboardButton(
        "🗑🗑 Remove ENTIRE trip", callback_data="delc_all")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="wiz_cancel")])

    await _wiz_msg(ctx, chat_id,
                   f"🗑 *{_esc(trip['name'])}*\n\nWhat do you want to remove?",
                   InlineKeyboardMarkup(buttons))
    return DEL_WHAT


async def del_execute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return DEL_WHAT
    await query.answer()
    data = query.data or ""
    chat_id = update.effective_chat.id
    user = get_user(ctx.application, chat_id)
    trips = user["trips"]

    if data == "wiz_cancel":
        await _wiz_cleanup(ctx, chat_id)
        return ConversationHandler.END

    trip_idx = ctx.user_data["del_trip_idx"]

    if data == "delc_all":
        name = trips[trip_idx]["name"]
        del trips[trip_idx]
        msg_text = f"✅ Trip *{_esc(name)}* removed!"
    else:
        c_idx = int(data.split("_")[1])
        c_name = trips[trip_idx]["combos"][c_idx]["name"]
        del trips[trip_idx]["combos"][c_idx]
        if not trips[trip_idx]["combos"]:
            trip_name = trips[trip_idx]["name"]
            del trips[trip_idx]
            msg_text = f"✅ Combo *{_esc(c_name)}* removed.\nTrip *{_esc(trip_name)}* also removed (empty)."
        else:
            msg_text = f"✅ Combo *{_esc(c_name)}* removed!"

    save_user_data(chat_id, user)
    await _wiz_msg(ctx, chat_id, msg_text)
    await asyncio.sleep(1)
    await rebuild_dashboard(ctx.application, chat_id)
    await _wiz_cleanup(ctx, chat_id)
    return ConversationHandler.END

# ────────────────────────────────────────────────────────────────────────────
# Wizard cancel
# ────────────────────────────────────────────────────────────────────────────

async def wiz_cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel command or any command that should abort the wizard."""
    chat_id = update.effective_chat.id
    await _del_user_msg(update, ctx)
    await _wiz_cleanup(ctx, chat_id)
    return ConversationHandler.END

# ────────────────────────────────────────────────────────────────────────────
# Application lifecycle
# ────────────────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    """Called once after the bot connects.  Loads user data and starts the
    background update loop."""
    users = load_all_users()
    app.bot_data["users"] = users
    logger.info("Loaded %d user(s).", len(users))

    task = asyncio.create_task(updater_loop(app))
    app.bot_data["updater_task"] = task


async def post_shutdown(app: Application) -> None:
    """Called on graceful shutdown.  Cancels the update loop and persists
    every user's state to disk."""
    task: asyncio.Task | None = app.bot_data.get("updater_task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    for chat_id, udata in app.bot_data.get("users", {}).items():
        save_user_data(chat_id, udata)
    logger.info("Bot stopped.")

# ────────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────────

def main() -> None:
    _maybe_migrate_legacy()

    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        logger.error("BOT_TOKEN not set. Create a .env file with BOT_TOKEN=<your-token> or export it.")
        sys.exit(1)

    gcfg = load_global_config()

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["global_config"] = gcfg

    # /add wizard (multi-step conversation)
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            WIZ_CHOOSE: [CallbackQueryHandler(wiz_choose_trip, pattern=r"^(addv_|wiz_cancel)")],
            WIZ_TRIP_NAME: [
                CallbackQueryHandler(wiz_trip_name, pattern=r"^wiz_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_trip_name),
            ],
            WIZ_COMBO_NAME: [
                CallbackQueryHandler(wiz_combo_name, pattern=r"^wiz_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_combo_name),
            ],
            WIZ_LINE: [
                CallbackQueryHandler(wiz_line, pattern=r"^wiz_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_line),
            ],
            WIZ_BOARDING: [
                CallbackQueryHandler(wiz_boarding, pattern=r"^wiz_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_boarding),
            ],
            WIZ_ALIGHTING: [
                CallbackQueryHandler(wiz_alighting, pattern=r"^wiz_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_alighting),
            ],
            WIZ_MORE: [CallbackQueryHandler(wiz_more, pattern=r"^(more_|wiz_cancel)")],
        },
        fallbacks=[
            CommandHandler("cancel", wiz_cancel_cmd),
            MessageHandler(filters.COMMAND, wiz_cancel_cmd),
        ],
    )

    # /remove wizard (two-step conversation)
    del_conv = ConversationHandler(
        entry_points=[CommandHandler("remove", cmd_remove)],
        states={
            DEL_TRIP: [CallbackQueryHandler(del_choose_trip, pattern=r"^(delv_|wiz_cancel)")],
            DEL_WHAT: [CallbackQueryHandler(del_execute, pattern=r"^(delc_|wiz_cancel)")],
        },
        fallbacks=[
            CommandHandler("cancel", wiz_cancel_cmd),
            MessageHandler(filters.COMMAND, wiz_cancel_cmd),
        ],
    )

    app.add_handler(add_conv)
    app.add_handler(del_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CallbackQueryHandler(callback_stop, pattern=r"^stop_\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_number))

    logger.info("Starting TramTram Bot…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
