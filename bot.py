import os
import re
import json
import uuid
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, List, Dict

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.utils import rowcol_to_a1

# ------------------ ENV ------------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ------------------ SHEETS SETUP ------------------
SHEET_NAME = "Bet Tracker"  # Google Sheet (file) name
HEADERS = [
    "ID", "Date Placed", "Event Date", "Tipster", "Selection",
    "Odds (dec)", "Bookmaker", "Stake", "Status", "Return",
    "Profit", "Cumulative Profit"
]
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Prefer GOOGLE_CREDS_JSON on hosts like Render. Fallback to local file on your Mac.
if os.getenv("GOOGLE_CREDS_JSON"):
    creds_dict = json.loads(os.getenv("GOOGLE_CREDS_JSON"))
    CREDS = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
else:
    CREDS = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", SCOPE)

client = gspread.authorize(CREDS)
ss = client.open(SHEET_NAME)
sheet = ss.worksheet("Bets")  # change if your tab name differs

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

# ------------------ SIMPLE PREFS (default tipster per chat) ------------------
PREFS_FILE = "prefs.json"  # best-effort file; on some hosts this is ephemeral
def load_prefs():
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}
def save_prefs(p):
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
EXCEL_EPOCH = datetime(1899, 12, 30, tzinfo=UK_TZ)  # Google/Excel serial epoch

def to_uk_string(dt: datetime) -> str:
    """Return UK-local timestamp with seconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UK_TZ)
    else:
        dt = dt.astimezone(UK_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def parse_odds(s: str) -> Optional[float]:
    """Decimal (2.5/2,50) or fractional (11/10)."""
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
    # decimal
    cleaned = re.sub(r"[^0-9,.\s]", "", s).replace(",", ".").strip()
    try:
        v = float(cleaned)
        return v if v > 1.0 else None
    except ValueError:
        return None

def parse_money(s: str) -> Optional[float]:
    """50, £50, 1,250, 50.00, etc."""
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

def calc_return_profit(result: str, dec_odds: float, stake: float) -> Tuple[float, float]:
    if result == "Win":
        ret = round(dec_odds * stake, 2);  return ret, round(ret - stake, 2)
    if result == "Loss":
        return 0.0, round(-stake, 2)
    return round(stake, 2), 0.0  # Void

def find_row_by_id(bet_id: str) -> Optional[int]:
    ids = sheet.col_values(1)
    for idx, v in enumerate(ids, start=1):
        if v == bet_id:
            return idx
    return None

def fmt_money(v: float) -> str:
    return f"£{v:,.2f}"

# Robust date reading from Sheets (handles strings & serials)
def to_datetime_uk(val) -> Optional[datetime]:
    if val is None or val == "":
        return None
    # numeric serial?
    try:
        f = float(val)
        return EXCEL_EPOCH + timedelta(days=f)
    except Exception:
        pass
    s = str(val).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=UK_TZ)
            return dt
        except ValueError:
            continue
    return None

# ------------------ BOT HANDLERS ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = get_default_tipster(update.effective_chat.id)
    msg = (
        "Send bets:\n"
        "• With tipster (5): `Tipster / Selection / Odds / Bookmaker / Stake`\n"
        "• No tipster (4): `Selection / Odds / Bookmaker / Stake`\n\n"
        "Other commands:\n"
        "• `/tipster <name>` — set default tipster for this chat\n"
        "• `/summary` — tipster P/L for the current month"
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
    parts = re.split(r"\s*/\s*", update.message.text.strip())
    if len(parts) not in (4, 5):
        await update.message.reply_text("❌ Format: Tipster / Selection / Odds / Bookmaker / Stake")
        return

    if len(parts) == 5:
        tipster, selection, odds_s, bookmaker, stake_s = parts
    else:
        tipster = get_default_tipster(update.effective_chat.id)
        selection, odds_s, bookmaker, stake_s = parts

    dec_odds = parse_odds(odds_s)
    stake = parse_money(stake_s)
    if dec_odds is None or stake is None or stake <= 0:
        await update.message.reply_text("❌ Check odds/stake. Examples: 2.1 or 11/10, stake 25 or £25")
        return

    if not tipster or tipster.strip() == "":
        tipster = "Unknown"

    bet_id = uuid.uuid4().hex[:8].upper()
    now = datetime.now(UK_TZ).strftime("%Y-%m-%d %H:%M:%S")

    try:
        sheet.append_row(
            [bet_id, now, "", tipster, selection, dec_odds, bookmaker, stake, "Pending", "", "", ""],
            value_input_option="USER_ENTERED"
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Could not write to Google Sheet: {e}")
        return

    keyboard = [[
        InlineKeyboardButton("✅ Win",  callback_data=f"res|{bet_id}|Win"),
        InlineKeyboardButton("⚪ Void", callback_data=f"res|{bet_id}|Void"),
        InlineKeyboardButton("❌ Loss", callback_data=f"res|{bet_id}|Loss"),
    ]]
    text = (
        f"✅ Bet logged (ID: {bet_id})\n"
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
        sheet.update_cell(row, 9, result)          # Status
        sheet.update_cell(row, 10, f"{ret:.2f}")   # Return
        sheet.update_cell(row, 11, f"{prof:.2f}")  # Profit

        new_text = (
            f"📝 Bet (ID: {bet_id})\n"
            f"[{values[3]}] {values[4]} @ {dec_odds:.2f} ({values[6]}) {fmt_money(stake)}\n"
            f"Result: {result} • Return: {fmt_money(ret)} • Profit: {fmt_money(prof)}"
        )
        await query.edit_message_text(new_text)
    except Exception as e:
        await query.edit_message_text(f"⚠️ Error: {e}")

# --------- /summary (P/L per tipster this month) ----------
def month_bounds_uk(today: Optional[date] = None) -> Tuple[datetime, datetime]:
    tz = UK_TZ
    if today is None:
        today = datetime.now(tz).date()
    start = datetime(today.year, today.month, 1, 0, 0, 0, tzinfo=tz)
    if today.month == 12:
        next_first = datetime(today.year + 1, 1, 1, tzinfo=tz)
    else:
        next_first = datetime(today.year, today.month + 1, 1, tzinfo=tz)
    end = next_first - timedelta(seconds=1)
    return start, end

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = sheet.get_all_values(value_render_option='UNFORMATTED_VALUE')
    if not rows or len(rows) <= 1:
        await update.message.reply_text("No data yet.")
        return
    data = rows[1:]  # skip header

    start_dt, end_dt = month_bounds_uk()
    stats: Dict[str, Dict[str, float]] = {}

    def to_num(x):
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).replace("£", "").replace(",", "").strip()
        try:
            return float(s)
        except Exception:
            return 0.0

    for r in data:
        if len(r) < 12:
            continue
        date_placed = to_datetime_uk(r[1])   # Column B
        tipster = (r[3] or "Unknown").strip()
        status = (r[8] or "").strip()
        stake_s, ret_s, prof_s = r[7], r[9], r[10]

        if status == "" or status.lower() == "pending":
            continue
        if not date_placed:
            continue
        if not (start_dt <= date_placed <= end_dt):
            continue

        stake = to_num(stake_s)
        ret = to_num(ret_s)
        prof = to_num(prof_s)

        t = stats.setdefault(tipster, {"bets": 0, "staked": 0.0, "ret": 0.0, "prof": 0.0})
        t["bets"] += 1
        t["staked"] += stake
        t["ret"] += ret
        t["prof"] += prof

    if not stats:
        month_name = start_dt.strftime("%B %Y")
        await update.message.reply_text(f"No settled bets for {month_name}.")
        return

    items = sorted(stats.items(), key=lambda kv: kv[1]["prof"], reverse=True)

    def fmt_money2(v): return f"£{v:,.2f}"
    lines: List[str] = []
    month_name = start_dt.strftime("%B %Y")
    lines.append(f"Tipster P/L — {month_name}")
    lines.append("")
    header = f"{'Tipster':<18} {'Bets':>4} {'Staked':>12} {'Return':>12} {'Profit':>12}"
    lines.append(header)
    lines.append("-" * len(header))
    total_bets = 0
    total_stake = total_ret = total_prof = 0.0
    for tip, s in items:
        lines.append(f"{tip:<18} {s['bets']:>4} {fmt_money2(s['staked']):>12} {fmt_money2(s['ret']):>12} {fmt_money2(s['prof']):>12}")
        total_bets += s["bets"]; total_stake += s["staked"]; total_ret += s["ret"]; total_prof += s["prof"]
    lines.append("-" * len(header))
    lines.append(f"{'Total':<18} {total_bets:>4} {fmt_money2(total_stake):>12} {fmt_money2(total_ret):>12} {fmt_money2(total_prof):>12}")

    await update.message.reply_text("```\n" + "\n".join(lines) + "\n```", parse_mode="Markdown")

# ------------------ MAIN ------------------
async def _post_init(app: Application):
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
    app.add_handler(CommandHandler("summary", summary_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, log_bet))
    app.add_handler(CallbackQueryHandler(button))
    print("Bot running…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
