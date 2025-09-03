import os
import re
import json
import uuid
from datetime import datetime
from typing import Optional, Tuple

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ------------------ ENV ------------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ------------------ SHEETS SETUP ------------------
SHEET_NAME = "Bet Tracker"  # Google spreadsheet file name
HEADERS = [
    "ID", "Date Placed", "Tipster", "Selection", "Odds (dec)",
    "Bookmaker", "Stake", "Status", "Return", "Profit", "Cumulative Profit"
]
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Prefer GOOGLE_CREDS_JSON (Render). Fallback to local credentials.json on your Mac.
if os.getenv("GOOGLE_CREDS_JSON"):
    creds_dict = json.loads(os.getenv("GOOGLE_CREDS_JSON"))
    CREDS = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
else:
    CREDS = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", SCOPE)

client = gspread.authorize(CREDS)
ss = client.open(SHEET_NAME)
sheet = ss.worksheet("Bets")  # change if your tab has a different name

def ensure_headers():
    existing = sheet.row_values(1)
    if existing != HEADERS:
        if existing:
            sheet.delete_rows(1)
        sheet.insert_row(HEADERS, 1)

ensure_headers()

# ------------------ SIMPLE PREFS (default tipster per chat) ------------------
PREFS_FILE = "prefs.json"

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
        pass  # on some hosts, disk may be ephemeral; it's okay

PREFS = load_prefs()  # { "<chat_id>": {"tipster": "Name"} }

def get_default_tipster(chat_id: int) -> str:
    return PREFS.get(str(chat_id), {}).get("tipster", "Unknown")

def set_default_tipster(chat_id: int, name: str):
    PREFS[str(chat_id)] = {"tipster": name}
    save_prefs(PREFS)

# ------------------ HELPERS ------------------
def parse_odds(s: str) -> Optional[float]:
    """Accept decimal (2.1) or fractional (11/10), return decimal > 1.0."""
    s = s.strip()
    frac = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", s)
    if frac:
        num, den = int(frac.group(1)), int(frac.group(2))
        if den == 0:
            return None
        return 1.0 + (num / den)
    try:
        v = float(s)
        return v if v > 1.0 else None
    except ValueError:
        return None

def parse_money(s: str) -> Optional[float]:
    """Accept 50, 50.00, £50, 1,250 etc., return float with 2 dp."""
    s = s.strip().replace(",", "").replace("£", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None

def calc_return_profit(result: str, dec_odds: float, stake: float) -> Tuple[float, float]:
    if result == "Win":
        ret = round(dec_odds * stake, 2)
        return ret, round(ret - stake, 2)
    if result == "Loss":
        return 0.0, round(-stake, 2)
    # Void
    return round(stake, 2), 0.0

def find_row_by_id(bet_id: str) -> Optional[int]:
    """Find row index (1-based) by ID in column A."""
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
        "Send bets in either format:\n"
        "1) With tipster (5 parts):\n"
        "`Tipster / Selection / Odds / Bookmaker / Stake`\n"
        "e.g. `John / Liverpool / 2.1 / Bet365 / 50`\n\n"
        "2) Without tipster (4 parts): uses your saved default tipster\n"
        "`Selection / Odds / Bookmaker / Stake`\n"
        "e.g. `Liverpool / 2.1 / Bet365 / 50`\n\n"
        f"Set your default tipster with: `/tipster <name>` (current: *{current}*)"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def tipster_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        current = get_default_tipster(update.effective_chat.id)
        await update.message.reply_text(f"Current default tipster: {current}\nSet it with `/tipster <name>`.", parse_mode="Markdown")
        return
    name = " ".join(context.args).strip()
    set_default_tipster(update.effective_chat.id, name)
    await update.message.reply_text(f"✅ Default tipster set to: {name}")

async def log_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    parts = [p.strip() for p in update.message.text.split("/")]
    # Decide whether the user included tipster explicitly
    if len(parts) == 5:
        tipster, selection, odds_s, bookmaker, stake_s = parts
    elif len(parts) == 4:
        tipster = get_default_tipster(update.effective_chat.id)
        selection, odds_s, bookmaker, stake_s = parts
    else:
        await update.message.reply_text("❌ Use one of:\n• Tipster / Selection / Odds / Bookmaker / Stake\n• Selection / Odds / Bookmaker / Stake")
        return

    dec_odds = parse_odds(odds_s)
    stake = parse_money(stake_s)
    if dec_odds is None or stake is None or stake <= 0:
        await update.message.reply_text("❌ Check odds/stake. Examples: odds 2.1 or 11/10, stake 25 or £25")
        return

    bet_id = uuid.uuid4().hex[:8].upper()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Append 10 values (A..J). Column K (Cumulative Profit) is computed by the Sheet formula.
    try:
        sheet.append_row([bet_id, now, tipster, selection, dec_odds, bookmaker, stake, "Pending", "", ""])
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

        # Read unformatted numbers to avoid currency parsing issues
        values = sheet.row_values(row, value_render_option='UNFORMATTED_VALUE')
        # Indexes: 0 ID, 1 Date, 2 Tipster, 3 Selection, 4 Odds, 5 Bookmaker, 6 Stake, 7 Status, 8 Return, 9 Profit, 10 CumProfit
        dec_odds = float(values[4])
        stake = float(values[6])

        ret, prof = calc_return_profit(result, dec_odds, stake)

        # Update Status (H col=8), Return (I col=9), Profit (J col=10)
        sheet.update_cell(row, 8, result)
        sheet.update_cell(row, 9, f"{ret:.2f}")
        sheet.update_cell(row, 10, f"{prof:.2f}")

        new_text = (
            f"📝 Bet (ID: {bet_id})\n"
            f"[{values[2]}] {values[3]} @ {dec_odds:.2f} ({values[5]}) {fmt_money(stake)}\n"
            f"Result: {result} • Return: {fmt_money(ret)} • Profit: {fmt_money(prof)}"
        )
        await query.edit_message_text(new_text)
    except Exception as e:
        await query.edit_message_text(f"⚠️ Error: {e}")

# ------------------ MAIN ------------------
def main():
    if not TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in your .env or Render env vars.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tipster", tipster_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, log_bet))
    app.add_handler(CallbackQueryHandler(button))
    print("Bot running…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
