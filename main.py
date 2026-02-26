"""
TramTram Bot – Real-time GTT Turin monitoring
Updates every 15 seconds via Muoversi a Torino OTP API.

/start     → one message per trip, updated live
<number>   → live stop info for 15 min with auto-expiry
On startup → deletes ALL messages in the chat with the user
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
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

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tramtram")

# ─── Constants ──────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.json"
STATE_PATH = Path(__file__).parent / "state.json"
MAX_ARRIVALS = 3
STOP_TTL_SECONDS = 15 * 60             # 15 minutes
UPDATE_INTERVAL = 15                    # seconds


# ─── Italy timezone ────────────────────────────────────────────────────────

def now_rome() -> datetime:
    utc_now = datetime.now(timezone.utc)
    year = utc_now.year
    mar31 = datetime(year, 3, 31, tzinfo=timezone.utc)
    dst_start = mar31 - timedelta(days=(mar31.weekday() + 1) % 7)
    dst_start = dst_start.replace(hour=1, minute=0, second=0, microsecond=0)
    oct31 = datetime(year, 10, 31, tzinfo=timezone.utc)
    dst_end = oct31 - timedelta(days=(oct31.weekday() + 1) % 7)
    dst_end = dst_end.replace(hour=1, minute=0, second=0, microsecond=0)
    offset = timedelta(hours=2) if dst_start <= utc_now < dst_end else timedelta(hours=1)
    return utc_now.astimezone(timezone(offset))


# ─── Persistent state ──────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        "dashboard_msgs": [],
        "stop_msgs": {},              # {msg_id_str: {"stop_id": ..., "expires": unix_ts}}
        "all_msg_ids": [],            # every msg_id sent/received, for full cleanup
    }


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            # migration from old key
            if "fermata_msgs" in data:
                data["stop_msgs"] = data.pop("fermata_msgs")
            if "all_msg_ids" not in data:
                data["all_msg_ids"] = []
            if "stop_msgs" not in data:
                data["stop_msgs"] = {}
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return _default_state()


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ─── Configuration ──────────────────────────────────────────────────────────

def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        logger.error("config.json not found.")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for k in ("telegram", "otp_base_url", "trips"):
        if k not in cfg:
            logger.error("Missing key: '%s'", k)
            sys.exit(1)
    return cfg


def collect_all_stop_ids(trips: list[dict]) -> set[str]:
    ids: set[str] = set()
    for trip in trips:
        for combo in trip["combos"]:
            for leg in combo["legs"]:
                ids.add(str(leg["stop_id_boarding"]))
                ids.add(str(leg["stop_id_alighting"]))
    return ids


# ─── OTP API ────────────────────────────────────────────────────────────────

async def fetch_stop_name(client: httpx.AsyncClient, sid: str, base: str) -> str:
    try:
        r = await client.get(f"{base}/stops/gtt:{sid}", timeout=10)
        if r.status_code == 200:
            return r.json().get("name", sid)
    except Exception as e:
        logger.warning("Stop name %s: %s", sid, e)
    return sid


async def fetch_stoptimes(client: httpx.AsyncClient, sid: str, base: str) -> list[dict]:
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
    async with httpx.AsyncClient() as client:
        name, patterns = await asyncio.gather(
            fetch_stop_name(client, sid, base),
            fetch_stoptimes(client, sid, base),
        )
    return name, patterns


# ─── Arrival extraction ────────────────────────────────────────────────────

def _route_from_pattern(pid: str) -> str:
    parts = pid.split(":")
    if len(parts) < 2:
        return ""
    rp = parts[1]
    for sfx in ("CDU", "CSU", "SU", "U", "E"):
        if rp.upper().endswith(sfx):
            return rp[: -len(sfx)]
    return rp


def extract_arrivals(patterns: list[dict], line: str, now_ts: int) -> list[dict]:
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


# ─── Formatting ─────────────────────────────────────────────────────────────

def _fmt(a: dict) -> str:
    m = a["minutes"]
    base = "ora!" if m == 0 else f"{m}'"
    return f"🟢{base}" if a["realtime"] else base


def _esc(t: str) -> str:
    return t.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")


def format_trip(
    trip: dict,
    st_map: dict[str, list[dict]],
    nm_map: dict[str, str],
    updated: datetime,
) -> str:
    now_ts = int(updated.timestamp())
    parts: list[str] = []

    # Header
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
                times = "   ".join(_fmt(a) for a in arrs)
                parts.append(f"        ⏳  *{times}*")
            else:
                parts.append("        ⏳  _nessun passaggio_")

    return "\n".join(parts)


def format_stop(
    stop_name: str, stop_id: str,
    patterns: list[dict], updated: datetime,
    expires_in_min: int | None = None,
) -> str:
    now_ts = int(updated.timestamp())
    arrivals = extract_all_arrivals(patterns, now_ts)

    parts: list[str] = []
    parts.append(f"🚏  *{_esc(stop_name)}*  (`{stop_id}`)")
    parts.append(f"⏱  {updated.strftime('%H:%M:%S')}")
    if expires_in_min is not None and expires_in_min > 0:
        parts.append(f"⏳  _scade tra {expires_in_min} min_")
    parts.append("")

    if arrivals:
        by_line: dict[str, list[dict]] = {}
        for a in arrivals:
            by_line.setdefault(a["line"], []).append(a)
        for line, arrs in sorted(by_line.items()):
            top = arrs[:MAX_ARRIVALS]
            dest = top[0]["headsign"]
            times = "   ".join(_fmt(a) for a in top)
            parts.append(f"  🚌  *{line}*  ➜  {_esc(dest)}")
            parts.append(f"        ⏳  *{times}*")
            parts.append("")
    else:
        parts.append("_Nessun mezzo in arrivo_")
        parts.append("")

    return "\n".join(parts)


# ─── Tracking ───────────────────────────────────────────────────────────────

def _remember_msg(app: Application, msg_id: int) -> None:
    """Add a msg_id to the global list for cleanup on restart."""
    state = app.bot_data["state"]
    state.setdefault("all_msg_ids", [])
    if msg_id not in state["all_msg_ids"]:
        state["all_msg_ids"].append(msg_id)
    save_state(state)


def track_dashboard_msgs(app: Application, mids: list[int]) -> None:
    state = app.bot_data["state"]
    state["dashboard_msgs"] = mids
    for m in mids:
        state.setdefault("all_msg_ids", [])
        if m not in state["all_msg_ids"]:
            state["all_msg_ids"].append(m)
    save_state(state)


def track_stop(app: Application, msg_id: int, stop_id: str) -> None:
    state = app.bot_data["state"]
    state.setdefault("stop_msgs", {})[str(msg_id)] = {
        "stop_id": stop_id,
        "expires": time.time() + STOP_TTL_SECONDS,
    }
    _remember_msg(app, msg_id)
    save_state(state)


def untrack_stop(app: Application, msg_id: int) -> None:
    state = app.bot_data["state"]
    state.get("stop_msgs", {}).pop(str(msg_id), None)
    save_state(state)


# ─── Full chat cleanup ─────────────────────────────────────────────────────

async def _try_delete(app: Application, chat_id: int, mid: int) -> None:
    try:
        await app.bot.delete_message(chat_id=chat_id, message_id=mid)
    except (BadRequest, TimedOut, NetworkError):
        pass


async def nuke_chat(app: Application, chat_id: int) -> None:
    """Delete ALL known messages from the chat."""
    state = app.bot_data["state"]
    all_ids = state.get("all_msg_ids", [])
    for mid in all_ids:
        await _try_delete(app, chat_id, mid)
    # Also try dashboard and stop msgs for safety
    for mid in state.get("dashboard_msgs", []):
        if mid:
            await _try_delete(app, chat_id, mid)
    for key in list(state.get("stop_msgs", {}).keys()):
        await _try_delete(app, chat_id, int(key))
    # Reset state
    state["dashboard_msgs"] = []
    state["stop_msgs"] = {}
    state["all_msg_ids"] = []
    save_state(state)
    logger.info("Chat fully cleaned (%d messages).", len(all_ids))


# ─── Update loop ────────────────────────────────────────────────────────────

async def updater_loop(app: Application, cfg: dict) -> None:
    chat_id: int = cfg["telegram"]["chat_id"]
    base: str = cfg["otp_base_url"]
    night_s: int = cfg.get("night_pause", {}).get("start_hour", 2)
    night_e: int = cfg.get("night_pause", {}).get("end_hour", 7)

    while True:
        trips = app.bot_data["config"]["trips"]
        now = now_rome()
        if night_s <= now.hour < night_e:
            await asyncio.sleep(UPDATE_INTERVAL)
            continue

        state = app.bot_data["state"]

        # ── Update trip messages ──
        d_msgs = state.get("dashboard_msgs", [])
        if d_msgs:
            all_sids = collect_all_stop_ids(trips)
            st_map, nm_map = await fetch_all_stops(all_sids, base)
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
                        logger.error("Edit trip %d: %s", i, e)
                except (TimedOut, NetworkError) as e:
                    logger.warning("Network trip %d: %s", i, e)
            if changed:
                state["dashboard_msgs"] = d_msgs
                save_state(state)

        # ── Update stop messages + expiry ──
        s_msgs = dict(state.get("stop_msgs", {}))
        now_unix = time.time()
        expired: list[int] = []

        if s_msgs:
            unique_sids = set()
            for info in s_msgs.values():
                sid = info if isinstance(info, str) else info.get("stop_id", "")
                unique_sids.add(sid)

            fetched: dict[str, tuple[str, list[dict]]] = {}
            async with httpx.AsyncClient() as client:
                for sid in unique_sids:
                    name, pats = await asyncio.gather(
                        fetch_stop_name(client, sid, base),
                        fetch_stoptimes(client, sid, base),
                    )
                    fetched[sid] = (name, pats)

            for mid_s, info in s_msgs.items():
                mid = int(mid_s)
                if isinstance(info, str):
                    stop_id = info
                    exp = now_unix + STOP_TTL_SECONDS
                else:
                    stop_id = info.get("stop_id", "")
                    exp = info.get("expires", now_unix + STOP_TTL_SECONDS)

                # Expired?
                if now_unix >= exp:
                    expired.append(mid)
                    continue

                exp_min = max(1, int((exp - now_unix) / 60))
                stop_name, pats = fetched.get(stop_id, (stop_id, []))
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
                        logger.error("Edit stop %s: %s", mid, e)
                except (TimedOut, NetworkError):
                    pass

            # Remove expired
            for mid in expired:
                await _try_delete(app, chat_id, mid)
                untrack_stop(app, mid)
                logger.info("Stop msg %s expired, removed.", mid)

        await asyncio.sleep(UPDATE_INTERVAL)


# ─── Commands ───────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]
    base = cfg["otp_base_url"]
    trips = cfg["trips"]
    state = ctx.application.bot_data["state"]

    # Delete old trip messages
    for old in state.get("dashboard_msgs", []):
        if old:
            await _try_delete(ctx.application, chat_id, old)

    # Delete the user's /start message
    if update.message:
        _remember_msg(ctx.application, update.message.message_id)
        await _try_delete(ctx.application, chat_id, update.message.message_id)

    all_sids = collect_all_stop_ids(trips)
    st_map, nm_map = await fetch_all_stops(all_sids, base)
    now = now_rome()

    mids: list[int] = []
    for trip in trips:
        text = format_trip(trip, st_map, nm_map, now)
        msg = await ctx.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        mids.append(msg.message_id)

    track_dashboard_msgs(ctx.application, mids)
    logger.info("/start → %d messages.", len(mids))


async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]
    base = cfg["otp_base_url"]
    trips = cfg["trips"]
    state = ctx.application.bot_data["state"]

    # Delete the user's /refresh message
    if update.message:
        _remember_msg(ctx.application, update.message.message_id)
        await _try_delete(ctx.application, chat_id, update.message.message_id)

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
        track_dashboard_msgs(ctx.application, mids)

    logger.info("/refresh ok.")


async def handle_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A message containing only a number → interpreted as a stop query."""
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if not text.isdigit():
        return

    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]
    base = cfg["otp_base_url"]
    stop_id = text

    # Delete the user's message
    if update.message:
        _remember_msg(ctx.application, update.message.message_id)
        await _try_delete(ctx.application, chat_id, update.message.message_id)

    stop_name, patterns = await fetch_stop_data(stop_id, base)
    now = now_rome()
    exp_min = STOP_TTL_SECONDS // 60
    body = format_stop(stop_name, stop_id, patterns, now, exp_min)

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

    track_stop(ctx.application, msg.message_id, stop_id)
    logger.info("Stop %s → msg %s (15 min).", stop_id, msg.message_id)


async def callback_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
    untrack_stop(ctx.application, mid)
    logger.info("STOP → msg %s deleted.", mid)


# ─── Config: save and rebuild ───────────────────────────────────────────────

def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


async def rebuild_dashboard(app: Application) -> None:
    """Delete old trip messages and create new ones."""
    cfg = app.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]
    base = cfg["otp_base_url"]
    trips = cfg["trips"]
    state = app.bot_data["state"]

    for old in state.get("dashboard_msgs", []):
        if old:
            await _try_delete(app, chat_id, old)

    if not trips:
        state["dashboard_msgs"] = []
        save_state(state)
        return

    all_sids = collect_all_stop_ids(trips)
    st_map, nm_map = await fetch_all_stops(all_sids, base)
    now = now_rome()

    mids: list[int] = []
    for trip in trips:
        text = format_trip(trip, st_map, nm_map, now)
        msg = await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        mids.append(msg.message_id)
    track_dashboard_msgs(app, mids)


# ─── Wizard: Add / Remove routes ────────────────────────────────────────────

# Conversation states
WIZ_CHOOSE, WIZ_TRIP_NAME, WIZ_COMBO_NAME = range(3)
WIZ_LINE, WIZ_BOARDING, WIZ_ALIGHTING, WIZ_MORE = range(3, 7)
DEL_TRIP, DEL_WHAT = range(7, 9)

_CANCEL_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("❌ Annulla", callback_data="wiz_cancel")]]
)


async def _wiz_msg(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str,
                   kb: InlineKeyboardMarkup | None = None) -> None:
    """Send or edit the wizard's single message."""
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
    _remember_msg(ctx.application, msg.message_id)


async def _wiz_cleanup(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    wiz_mid = ctx.user_data.pop("wiz_mid", None)
    if wiz_mid:
        await _try_delete(ctx.application, chat_id, wiz_mid)


async def _del_user_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        cfg = ctx.application.bot_data["config"]
        _remember_msg(ctx.application, update.message.message_id)
        await _try_delete(ctx.application, cfg["telegram"]["chat_id"],
                          update.message.message_id)


def _wiz_summary(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """Generate a summary of legs added so far."""
    v = ctx.user_data.get("wiz_trip_name", "")
    c = ctx.user_data.get("wiz_combo_name", "")
    legs = ctx.user_data.get("wiz_legs", [])
    lines = [f"📝 *{_esc(v)}* ➜ _{_esc(c)}_", ""]
    if legs:
        lines.append("Tratte:")
        for leg in legs:
            lines.append(f"  🚌 *{leg['line']}*: `{leg['stop_id_boarding']}` → `{leg['stop_id_alighting']}`")
        lines.append("")
    return "\n".join(lines)


# ─── /aggiungi flow ─────────────────────────────────────────────────────────

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]
    await _del_user_msg(update, ctx)
    ctx.user_data["wiz_legs"] = []
    ctx.user_data["wiz_trip_idx"] = None
    ctx.user_data["wiz_trip_name"] = None

    trips = cfg["trips"]
    buttons = []
    for i, trip in enumerate(trips):
        buttons.append([InlineKeyboardButton(trip["name"], callback_data=f"addv_{i}")])
    buttons.append([InlineKeyboardButton("➕ Nuovo viaggio", callback_data="addv_new")])
    buttons.append([InlineKeyboardButton("❌ Annulla", callback_data="wiz_cancel")])

    await _wiz_msg(ctx, chat_id,
                   "📝 *Aggiungi combo*\n\nDove vuoi aggiungerla?",
                   InlineKeyboardMarkup(buttons))
    return WIZ_CHOOSE


async def wiz_choose_trip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return WIZ_CHOOSE
    await query.answer()
    data = query.data or ""
    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]

    if data == "wiz_cancel":
        await _wiz_cleanup(ctx, chat_id)
        return ConversationHandler.END

    if data == "addv_new":
        await _wiz_msg(ctx, chat_id,
                       "📝 *Nuovo viaggio*\n\nCome vuoi chiamarlo?\n_Es: Casa → Ufficio_",
                       _CANCEL_KB)
        return WIZ_TRIP_NAME

    idx = int(data.split("_")[1])
    ctx.user_data["wiz_trip_idx"] = idx
    ctx.user_data["wiz_trip_name"] = cfg["trips"][idx]["name"]
    await _wiz_msg(ctx, chat_id,
                   f"📝 Aggiungi combo a *{_esc(cfg['trips'][idx]['name'])}*\n\nNome della combo?\n_Es: Diretto 42_",
                   _CANCEL_KB)
    return WIZ_COMBO_NAME


async def wiz_trip_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        if (update.callback_query.data or "") == "wiz_cancel":
            cfg = ctx.application.bot_data["config"]
            await _wiz_cleanup(ctx, cfg["telegram"]["chat_id"])
            return ConversationHandler.END
        return WIZ_TRIP_NAME

    await _del_user_msg(update, ctx)
    if not update.message or not update.message.text:
        return WIZ_TRIP_NAME
    name = update.message.text.strip()
    if not name:
        return WIZ_TRIP_NAME

    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]
    ctx.user_data["wiz_trip_name"] = name
    ctx.user_data["wiz_trip_idx"] = None
    await _wiz_msg(ctx, chat_id,
                   f"📝 *{_esc(name)}*\n\nNome della combo?\n_Es: Diretto 42_",
                   _CANCEL_KB)
    return WIZ_COMBO_NAME


async def wiz_combo_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        if (update.callback_query.data or "") == "wiz_cancel":
            cfg = ctx.application.bot_data["config"]
            await _wiz_cleanup(ctx, cfg["telegram"]["chat_id"])
            return ConversationHandler.END
        return WIZ_COMBO_NAME

    await _del_user_msg(update, ctx)
    if not update.message or not update.message.text:
        return WIZ_COMBO_NAME
    name = update.message.text.strip()
    if not name:
        return WIZ_COMBO_NAME

    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]
    ctx.user_data["wiz_combo_name"] = name
    trip_name = ctx.user_data["wiz_trip_name"]
    await _wiz_msg(ctx, chat_id,
                   f"📝 *{_esc(trip_name)}* ➜ _{_esc(name)}_\n\nLinea?\n_Es: 42_",
                   _CANCEL_KB)
    return WIZ_LINE


async def wiz_line(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        if (update.callback_query.data or "") == "wiz_cancel":
            cfg = ctx.application.bot_data["config"]
            await _wiz_cleanup(ctx, cfg["telegram"]["chat_id"])
            return ConversationHandler.END
        return WIZ_LINE

    await _del_user_msg(update, ctx)
    if not update.message or not update.message.text:
        return WIZ_LINE
    line = update.message.text.strip()
    if not line:
        return WIZ_LINE

    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]
    ctx.user_data["wiz_current_line"] = line
    await _wiz_msg(ctx, chat_id,
                   _wiz_summary(ctx) + f"🚌 Linea *{_esc(line)}*\n\nStop ID salita (partenza)?",
                   _CANCEL_KB)
    return WIZ_BOARDING


async def wiz_boarding(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        if (update.callback_query.data or "") == "wiz_cancel":
            cfg = ctx.application.bot_data["config"]
            await _wiz_cleanup(ctx, cfg["telegram"]["chat_id"])
            return ConversationHandler.END
        return WIZ_BOARDING

    await _del_user_msg(update, ctx)
    if not update.message or not update.message.text:
        return WIZ_BOARDING
    sid = update.message.text.strip()
    if not sid.isdigit():
        cfg = ctx.application.bot_data["config"]
        chat_id = cfg["telegram"]["chat_id"]
        await _wiz_msg(ctx, chat_id,
                       _wiz_summary(ctx) + "⚠️ Inserisci un numero valido per lo stop ID salita.",
                       _CANCEL_KB)
        return WIZ_BOARDING

    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]
    ctx.user_data["wiz_current_boarding"] = sid
    line = ctx.user_data["wiz_current_line"]
    await _wiz_msg(ctx, chat_id,
                   _wiz_summary(ctx) + f"🚌 Linea *{_esc(line)}* da `{sid}`\n\nStop ID discesa (arrivo)?",
                   _CANCEL_KB)
    return WIZ_ALIGHTING


async def wiz_alighting(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        if (update.callback_query.data or "") == "wiz_cancel":
            cfg = ctx.application.bot_data["config"]
            await _wiz_cleanup(ctx, cfg["telegram"]["chat_id"])
            return ConversationHandler.END
        return WIZ_ALIGHTING

    await _del_user_msg(update, ctx)
    if not update.message or not update.message.text:
        return WIZ_ALIGHTING
    sid = update.message.text.strip()
    if not sid.isdigit():
        cfg = ctx.application.bot_data["config"]
        chat_id = cfg["telegram"]["chat_id"]
        await _wiz_msg(ctx, chat_id,
                       _wiz_summary(ctx) + "⚠️ Inserisci un numero valido per lo stop ID discesa.",
                       _CANCEL_KB)
        return WIZ_ALIGHTING

    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]
    ctx.user_data["wiz_legs"].append({
        "line": ctx.user_data["wiz_current_line"],
        "stop_id_boarding": ctx.user_data["wiz_current_boarding"],
        "stop_id_alighting": sid,
    })

    summary = _wiz_summary(ctx) + "Aggiungere un'altra tratta?"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Sì, altra tratta", callback_data="more_yes"),
         InlineKeyboardButton("💾 Salva", callback_data="more_save")],
        [InlineKeyboardButton("❌ Annulla", callback_data="wiz_cancel")],
    ])
    await _wiz_msg(ctx, chat_id, summary, kb)
    return WIZ_MORE


async def wiz_more(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return WIZ_MORE
    await query.answer()
    data = query.data or ""
    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]

    if data == "wiz_cancel":
        await _wiz_cleanup(ctx, chat_id)
        return ConversationHandler.END

    if data == "more_yes":
        await _wiz_msg(ctx, chat_id,
                       _wiz_summary(ctx) + "Linea?",
                       _CANCEL_KB)
        return WIZ_LINE

    # ── Save ──
    combo = {
        "name": ctx.user_data["wiz_combo_name"],
        "legs": ctx.user_data["wiz_legs"],
    }
    trip_idx = ctx.user_data["wiz_trip_idx"]
    if trip_idx is not None:
        cfg["trips"][trip_idx]["combos"].append(combo)
    else:
        cfg["trips"].append({
            "name": ctx.user_data["wiz_trip_name"],
            "combos": [combo],
        })

    save_config(cfg)
    ctx.application.bot_data["config"] = cfg
    await _wiz_msg(ctx, chat_id, "✅ *Salvato!* Ricostruisco il cruscotto…")
    await rebuild_dashboard(ctx.application)
    await _wiz_cleanup(ctx, chat_id)
    return ConversationHandler.END


# ─── /elimina flow ──────────────────────────────────────────────────────────

async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]
    await _del_user_msg(update, ctx)

    trips = cfg["trips"]
    if not trips:
        msg = await ctx.bot.send_message(chat_id=chat_id, text="Nessun viaggio configurato.")
        _remember_msg(ctx.application, msg.message_id)
        return ConversationHandler.END

    buttons = []
    for i, trip in enumerate(trips):
        n_combos = len(trip["combos"])
        buttons.append([InlineKeyboardButton(
            f"{trip['name']} ({n_combos} combo)", callback_data=f"delv_{i}")])
    buttons.append([InlineKeyboardButton("❌ Annulla", callback_data="wiz_cancel")])

    await _wiz_msg(ctx, chat_id,
                   "🗑 *Elimina percorso*\n\nQuale viaggio?",
                   InlineKeyboardMarkup(buttons))
    return DEL_TRIP


async def del_choose_trip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return DEL_TRIP
    await query.answer()
    data = query.data or ""
    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]

    if data == "wiz_cancel":
        await _wiz_cleanup(ctx, chat_id)
        return ConversationHandler.END

    idx = int(data.split("_")[1])
    ctx.user_data["del_trip_idx"] = idx
    trip = cfg["trips"][idx]

    buttons = []
    for j, combo in enumerate(trip["combos"]):
        n_legs = len(combo["legs"])
        buttons.append([InlineKeyboardButton(
            f"🗑 {combo['name']} ({n_legs} tratte)", callback_data=f"delc_{j}")])
    buttons.append([InlineKeyboardButton(
        "🗑🗑 Elimina TUTTO il viaggio", callback_data="delc_all")])
    buttons.append([InlineKeyboardButton("❌ Annulla", callback_data="wiz_cancel")])

    await _wiz_msg(ctx, chat_id,
                   f"🗑 *{_esc(trip['name'])}*\n\nCosa vuoi eliminare?",
                   InlineKeyboardMarkup(buttons))
    return DEL_WHAT


async def del_execute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return DEL_WHAT
    await query.answer()
    data = query.data or ""
    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]

    if data == "wiz_cancel":
        await _wiz_cleanup(ctx, chat_id)
        return ConversationHandler.END

    trip_idx = ctx.user_data["del_trip_idx"]

    if data == "delc_all":
        name = cfg["trips"][trip_idx]["name"]
        del cfg["trips"][trip_idx]
        msg_text = f"✅ Viaggio *{_esc(name)}* eliminato!"
    else:
        c_idx = int(data.split("_")[1])
        c_name = cfg["trips"][trip_idx]["combos"][c_idx]["name"]
        del cfg["trips"][trip_idx]["combos"][c_idx]
        if not cfg["trips"][trip_idx]["combos"]:
            trip_name = cfg["trips"][trip_idx]["name"]
            del cfg["trips"][trip_idx]
            msg_text = f"✅ Combo *{_esc(c_name)}* eliminata.\nViaggio *{_esc(trip_name)}* rimosso (vuoto)."
        else:
            msg_text = f"✅ Combo *{_esc(c_name)}* eliminata!"

    save_config(cfg)
    ctx.application.bot_data["config"] = cfg
    await _wiz_msg(ctx, chat_id, msg_text)
    await asyncio.sleep(1)
    await rebuild_dashboard(ctx.application)
    await _wiz_cleanup(ctx, chat_id)
    return ConversationHandler.END


# ─── Cancel wizard ──────────────────────────────────────────────────────────

async def wiz_cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cfg = ctx.application.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]
    await _del_user_msg(update, ctx)
    await _wiz_cleanup(ctx, chat_id)
    return ConversationHandler.END


# ─── Lifecycle ──────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    cfg = app.bot_data["config"]
    chat_id = cfg["telegram"]["chat_id"]
    state = load_state()
    app.bot_data["state"] = state

    # Full chat cleanup
    await nuke_chat(app, chat_id)
    logger.info("Clean start.")

    task = asyncio.create_task(updater_loop(app, cfg))
    app.bot_data["updater_task"] = task


async def post_shutdown(app: Application) -> None:
    task: asyncio.Task | None = app.bot_data.get("updater_task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    state = app.bot_data.get("state")
    if state:
        save_state(state)
    logger.info("Bot stopped.")


# ─── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = load_config()
    token = cfg["telegram"]["bot_token"]

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["config"] = cfg

    # Wizard: /aggiungi
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("aggiungi", cmd_add)],
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
            CommandHandler("annulla", wiz_cancel_cmd),
            MessageHandler(filters.COMMAND, wiz_cancel_cmd),
        ],
    )

    # Wizard: /elimina
    del_conv = ConversationHandler(
        entry_points=[CommandHandler("elimina", cmd_delete)],
        states={
            DEL_TRIP: [CallbackQueryHandler(del_choose_trip, pattern=r"^(delv_|wiz_cancel)")],
            DEL_WHAT: [CallbackQueryHandler(del_execute, pattern=r"^(delc_|wiz_cancel)")],
        },
        fallbacks=[
            CommandHandler("annulla", wiz_cancel_cmd),
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
