import os
import re
import json
import uuid
import requests
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Tuple

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.utils import rowcol_to_a1

# ------------------ ENV ------------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
APISPORTS_KEY = os.getenv("APISPORTS_KEY")  # API-Football (direct) key

# ------------------ SHEETS SETUP ------------------
SHEET_NAME = "Bet Tracker"
HEADERS = [
    "ID", "Date Placed", "Event Date", "Tipster", "Selection",
    "Odds (dec)", "Bookmaker", "Stake", "Status", "Return",
    "Profit", "Cumulative Profit"
]
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

if os.getenv("GOOGLE_CREDS_JSON"):
    creds_dict = json.loads(os.getenv("GOOGLE_CREDS_JSON"))
    CREDS = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
else:
    CREDS = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", SCOPE)

client = gspread.authorize(CREDS)
ss = client.open(SHEET_NAME)
sheet = ss.worksheet("Bets")  # change if your tab has a different name

def ensure_headers():
    try:
        _ = sheet.row_values(1)
    except Exception:
        _ = []
    if sheet.col_count < len(HEADERS):
        sheet.add_cols(len(HEADERS) - sheet.col_count)
    end_a1 = rowcol_to_a1(1, len(HEADERS))
    sheet.update(values=[HEADERS], range_name=f"A1:{end_a1}")

ensure_headers()

# ------------------ SIMPLE PREFS ------------------
PREFS_FILE = "prefs.json"
def load_prefs():
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}
def save_prefs(p):  # disk may be ephemeral on hosts; best effort is fine
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(p, f, indent=2)
    except Exception:
        pass

PREFS = load_prefs()
def get_default_tipster(chat_id: int) -> str:
    return PREFS.get(str(chat_id), {}).get("tipster", "Unknown")
def set_default_tipster(chat_id: int, name: str):
    PREFS[str(chat_id)] = {"tipster": name}
    save_prefs(PREFS)

# ------------------ HELPERS ------------------
UK_TZ = ZoneInfo("Europe/London")

def parse_odds(s: str) -> Optional[float]:
    """
    Accepts decimal (2.5, 2,50) or fractional (11/10).
    Ignores stray/non-breaking spaces; returns decimal > 1.0.
    """
    if not s:
        return None
    s = s.strip().replace("\u00A0", " ").replace("\u202F", " ")
    # fractional?
    frac = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", s)
    if frac:
        num, den = int(frac.group(1)), int(frac.group(2))
        if den == 0:
            return None
        return 1.0 + (num / den)
    # decimal: keep digits/comma/dot → normalize to dot
    cleaned = re.sub(r"[^0-9,.\s]", "", s).replace(",", ".").strip()
    try:
        v = float(cleaned)
        return v if v > 1.0 else None
    except ValueError:
        return None

def parse_money(s: str) -> Optional[float]:
    """
    Accepts 50, £50, 1,250, 50.00, etc. Ignores symbols/spaces.
    """
    if not s:
        return None
    s = s.strip().replace("\u00A0", " ").replace("\u202F", " ")
    cleaned = re.sub(r"[^0-9.]", "", s)
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        v = float(cleaned)
        return round(v, 2) if v > 0 else None
    except ValueError:
        return None

def to_uk_string(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UK_TZ)
    else:
        dt = dt.astimezone(UK_TZ)
    return dt.strftime("%Y-%m-%d %H:%M")

def parse_event_dt(s: str) -> Optional[str]:
    """Accepts many UK-friendly formats; returns 'YYYY-MM-DD HH:MM' UK time."""
    if not s:
        return None
    raw = s.strip().lower()
    now = datetime.now(UK_TZ)

    # today/tomorrow HH:MM
    m = re.fullmatch(r"(today|tomorrow)\s+(\d{1,2}):(\d{2})", raw)
    if m:
        day_word, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
        base = now.date() if day_word == "today" else (now + timedelta(days=1)).date()
        dt = datetime(base.year, base.month, base.day, hh, mm, tzinfo=UK_TZ)
        return to_uk_string(dt)

    # Try common explicit patterns (DATE FIRST)
    fmts = [
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M",
        "%d-%m-%Y %H:%M",
        "%d %b %Y %H:%M",
        "%d %B %Y %H:%M",
        "%d/%m %H:%M",      # assume current year
    ]
    for fmt in fmts:
        try:
            dt_naive = datetime.strptime(raw, fmt)
            if fmt == "%d/%m %H:%M":
                dt_naive = dt_naive.replace(year=now.year)
            dt = dt_naive.replace(tzinfo=UK_TZ)
            return to_uk_string(dt)
        except ValueError:
            pass

    # Try TIME FIRST (e.g., '20:00 05/09/2025' or '20:00 05/09')
    fmts_time_first = [
        "%H:%M %d/%m/%Y",
        "%H:%M %d-%m-%Y",
        "%H:%M %d %b %Y",
        "%H:%M %d %B %Y",
        "%H:%M %d/%m",      # assume current year
    ]
    for fmt in fmts_time_first:
        try:
            dt_naive = datetime.strptime(raw, fmt)
            if fmt == "%H:%M %d/%m":
                dt_naive = dt_naive.replace(year=now.year)
            dt = dt_naive.replace(tzinfo=UK_TZ)
            return to_uk_string(dt)
        except ValueError:
            pass

    return None

# ---------- Football event date lookup (API-Football direct) ----------
API_FOOTBALL_HOST = "v3.football.api-sports.io"

def _api_get(path: str, params: dict) -> Optional[dict]:
    if not APISPORTS_KEY:
        return None
    try:
        r = requests.get(
            f"https://{API_FOOTBALL_HOST}{path}",
            headers={"x-apisports-key": APISPORTS_KEY},
            params=params,
            timeout=10,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None

def guess_event_datetime_from_selection(selection: str) -> Optional[str]:
    """
    If 'Team A v Team B' → try to find their next head-to-head.
    Else → find Team and take next fixture.
    Return 'YYYY-MM-DD HH:MM' UK or None.
    """
    if not selection or not APISPORTS_KEY:
        return None

    # Split "team1 vs team2"
    m = re.split(r"\s+v(?:s|\.)?\s+", selection, flags=re.IGNORECASE)
    team1 = team2 = None
    if len(m) == 2:
        team1, team2 = m[0].strip(), m[1].strip()

    def find_team_id(name: str) -> Optional[int]:
        data = _api_get("/teams", {"search": name})
        if not data or "response" not in data or not data["response"]:
            return None
        return data["response"][0]["team"]["id"]

    if team1 and team2:
        id1 = find_team_id(team1)
        id2 = find_team_id(team2)
        if id1 and id2:
            data = _api_get("/fixtures", {"team": id1, "next": 5})
            if data and data.get("response"):
                for fx in data["response"]:
                    home = fx["teams"]["home"]["id"]
                    away = fx["teams"]["away"]["id"]
                    if id2 in (home, away):
                        iso = fx["fixture"]["date"]
                        try:
                            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                            return to_uk_string(dt.astimezone(UK_TZ))
                        except Exception:
                            continue
        team_to_use = team1
    else:
        team_to_use = selection

    team_id = find_team_id(team_to_use)
    if not team_id:
        return None
    data = _api_get("/fixtures", {"team": team_id, "next": 1})
    if not data or not data.get("response"):
        return None
    try:
        iso = data["response"][0]["fixture"]["date"]
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return to_uk_string(dt.astimezone(UK_TZ))
    except Exception:
        return None

def calc_return_profit(result: str, dec_odds: float, stake: float) -> Tuple[float, float]:
    if result == "Win":
        ret = round(dec_odds * stake, 2); return ret, round(ret - stake, 2)
    if result == "Loss":
        return 0.0, round(-stake, 2)
    return round(stake, 2), 0.0

def find_row_by_id(bet_id: str) -> Optional[int]:
    ids = sheet.col_values(1)
    for idx, v in enumerate(ids, start=1):
        if v == bet_id:
            return idx
    return None

def fmt_money(v: float) -> str:
    return f"£{v:,.2f}"

# ------------------ BOT HANDLERS ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = get_default_tipster(update.effective_chat.id)
    msg = (
        "Send bets:\n"
        "• With tipster (5): `Tipster / Selection / Odds / Bookmaker / Stake`\n"
        "• No tipster (4): `Selection / Odds / Bookmaker / Stake`\n"
        "• Optional event date last: `... / 05/09/2025 20:00` or `... / 20:00 05/09/2025` or `... / tomorrow 19:45`\n\n"
        f"Set default tipster: `/tipster <name>` (current: *{current}*)\n"
        "If you omit event date and an API key is set, I’ll try to auto-fill it for *football*."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def tipster_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        current = get_default_tipster(update.effective_chat.id)
        await update.message.reply_text(
            f"Current default tipster: {current}\nSet it with `/tipster <name>`.",
            parse_mode="Markdown"
        )
        return
    name = " ".join(context.args).strip()
    set_default_tipster(update.effective_chat.id, name)
    await update.message.reply_text(f"✅ Default tipster set to: {name}")

async def log_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    parts = [p.strip() for p in update.message.text.split("/")]
    tipster = None
    event_str = None

    if len(parts) == 6:
        tipster, selection, odds_s, bookmaker, stake_s, event_raw = parts
        event_str = parse_event_dt(event_raw)
        if event_raw and event_str is None:
            await update.message.reply_text("❌ Couldn't read the event date/time. Try '2025-09-05 20:00', '20:00 05/09/2025', or 'tomorrow 19:45'.")
            return
    elif len(parts) == 5:
        selection, odds_s, bookmaker, stake_s, event_raw = parts
        tipster = get_default_tipster(update.effective_chat.id)
        event_str = parse_event_dt(event_raw)
        if event_raw and event_str is None:
            await update.message.reply_text("❌ Couldn't read the event date/time. Try '2025-09-05 20:00', '20:00 05/09/2025', or 'tomorrow 19:45'.")
            return
    elif len(parts) == 4:
        tipster = get_default_tipster(update.effective_chat.id)
        selection, odds_s, bookmaker, stake_s = parts
    else:
        await update.message.reply_text(
            "❌ Format error. Use one of:\n"
            "• Tipster / Selection / Odds / Bookmaker / Stake\n"
            "• Selection / Odds / Bookmaker / Stake\n"
            "• Tipster / Selection / Odds / Bookmaker / Stake / EventDateTime\n"
            "• Selection / Odds / Bookmaker / Stake / EventDateTime"
        )
        return

    dec_odds = parse_odds(odds_s)
    stake = parse_money(stake_s)
    if dec_odds is None or stake is None or stake <= 0:
        await update.message.reply_text("❌ Check odds/stake. Examples: 2.1 or 11/10, stake 25 or £25")
        return

    if not tipster or tipster.strip() == "":
        tipster = "Unknown"

    if not event_str:
        guessed = guess_event_datetime_from_selection(selection)
        event_str = guessed or ""

    bet_id = uuid.uuid4().hex[:8].upper()
    now = datetime.now(UK_TZ).strftime("%Y-%m-%d %H:%M")

    try:
        sheet.append_row([bet_id, now, event_str, tipster, selection, dec_odds, bookmaker, stake, "Pending", "", "", ""])
    except Exception as e:
        await update.message.reply_text(f"⚠️ Could not write to Google Sheet: {e}")
        return

    keyboard = [[
        InlineKeyboardButton("✅ Win",  callback_data=f"res|{bet_id}|Win"),
        InlineKeyboardButton("⚪ Void", callback_data=f"res|{bet_id}|Void"),
        InlineKeyboardButton("❌ Loss", callback_data=f"res|{bet_id}|Loss"),
    ]]
    shown_event = f"\nEvent: {event_str}" if event_str else ""
    text = (
        f"✅ Bet logged (ID: {bet_id}){shown_event}\n"
        f"[{tipster}] {selection} @ {dec_odds:.2f} ({bookmaker}) {fmt_money(stake)}\n"
        f"Status: Pending"
    )
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, bet_id, result = query.data.split("|")
        row = find_row_by_id(bet_id)
        if not row:
            await query.edit_message_text("⚠️ Could not find this bet in the sheet.")
            return

        values = sheet.row_values(row, value_render_option='UNFORMATTED_VALUE')
        dec_odds = float(values[5])
        stake = float(values[7])

        ret, prof = calc_return_profit(result, dec_odds, stake)
        sheet.update_cell(row, 9, result)          # I: Status
        sheet.update_cell(row, 10, f"{ret:.2f}")   # J: Return
        sheet.update_cell(row, 11, f"{prof:.2f}")  # K: Profit

        new_text = (
            f"📝 Bet (ID: {bet_id})\n"
            f"[{values[3]}] {values[4]} @ {dec_odds:.2f} ({values[6]}) {fmt_money(stake)}\n"
            f"Result: {result} • Return: {fmt_money(ret)} • Profit: {fmt_money(prof)}"
        )
        await query.edit_message_text(new_text)
    except Exception as e:
        await query.edit_message_text(f"⚠️ Error: {e}")

# ------------------ MAIN ------------------
async def _post_init(app: Application):
    # Kill any leftover webhook so polling never conflicts
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

def main():
    if not TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in your .env or Render env vars.")
    app = Application.builder().token(TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tipster", tipster_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, log_bet))
    app.add_handler(CallbackQueryHandler(button))
    print("Bot running…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
