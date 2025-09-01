import json
import os
import re
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
SHEET_NAME = "Bet Tracker"  # the spreadsheet file name
HEADERS = [
    "ID", "Date Placed", "Selection", "Odds (dec)",
    "Bookmaker", "Stake", "Status", "Return", "Profit",
    "Cumulative Profit"
]

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

if os.getenv("GOOGLE_CREDS_JSON"):
    # Read the whole JSON from the env var on Render
    creds_dict = json.loads(os.getenv("GOOGLE_CREDS_JSON"))
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
else:
    # Fallback for running on your Mac
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)

client = gspread.authorize(creds)

# Open the spreadsheet by name, then the tab named "Bets"
ss = client.open(SHEET_NAME)
sheet = ss.worksheet("Bets")  # change to ss.get_worksheet(0) if you prefer "first tab" logic

def ensure_headers():
    existing = sheet.row_values(1)
    if existing != HEADERS:
        if existing:
            sheet.delete_rows(1)
        sheet.insert_row(HEADERS, 1)

ensure_headers()

# ------------------ HELPERS ------------------
def parse_odds(s: str) -> Optional[float]:
    """Accept decimal odds (e.g. 2.1) or fractional (e.g. 11/10), return decimal."""
    s = s.strip()
    # fractional like 11/10
    frac = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", s)
    if frac:
        num, den = int(frac.group(1)), int(frac.group(2))
        if den == 0:
            return None
        return 1.0 + (num / den)
    # decimal like 2.10
    try:
        v = float(s)
        return v if v > 1.0 else None
    except ValueError:
        return None

def parse_money(s: str) -> Optional[float]:
    """Accept 50, 50.00, £50, 1,250 etc."""
    s = s.strip().replace(",", "").replace("£", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None

def calc_return_profit(result: str, dec_odds: float, stake: float) -> Tuple[float, float]:
    """Return (return_amount, profit) based on result."""
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
    msg = (
        "Send bets as:\n"
        "`Selection / Odds / Bookmaker / Stake`\n\n"
        "Examples:\n"
        "`Liverpool / 2.1 / Bet365 / 50`\n"
        "`Chelsea / 11/10 / SkyBet / 25`\n\n"
        "I'll save it to your *Bets* sheet and show buttons to settle it."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def log_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    parts = [p.strip() for p in update.message.text.split("/")]
    if len(parts) != 4:
        await update.message.reply_text("❌ Use: Selection / Odds / Bookmaker / Stake")
        return

    selection, odds_s, bookmaker, stake_s = parts
    dec_odds = parse_odds(odds_s)
    stake = parse_money(stake_s)

    if dec_odds is None or stake is None or stake <= 0:
        await update.message.reply_text("❌ Check odds/stake. Examples: odds 2.1 or 11/10, stake 25 or £25")
        return

    bet_id = uuid.uuid4().hex[:8].upper()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Append 9 values (A..I). Column J (Cumulative Profit) is calculated by your Sheet formula.
    try:
        sheet.append_row([bet_id, now, selection, dec_odds, bookmaker, stake, "Pending", "", ""])
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
        f"{selection} @ {dec_odds:.2f} ({bookmaker}) {fmt_money(stake)}\n"
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

        # Read unformatted numbers to avoid "£50.00" parsing issues
        values = sheet.row_values(row, value_render_option='UNFORMATTED_VALUE')
        # Columns: [0]ID [1]Date [2]Selection [3]Odds [4]Bookmaker [5]Stake [6]Status [7]Return [8]Profit [9]CumProfit
        dec_odds = float(values[3])
        stake = float(values[5])

        ret, prof = calc_return_profit(result, dec_odds, stake)

        # Update Status, Return, Profit
        sheet.update_cell(row, 7, result)       # G: Status
        sheet.update_cell(row, 8, f"{ret:.2f}") # H: Return
        sheet.update_cell(row, 9, f"{prof:.2f}")# I: Profit

        new_text = (
            f"📝 Bet (ID: {bet_id})\n"
            f"{values[2]} @ {dec_odds:.2f} ({values[4]}) {fmt_money(stake)}\n"
            f"Result: {result} • Return: {fmt_money(ret)} • Profit: {fmt_money(prof)}"
        )
        await query.edit_message_text(new_text)
    except Exception as e:
        await query.edit_message_text(f"⚠️ Error: {e}")

# ------------------ MAIN ------------------
def main():
    if not TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in your .env file first.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, log_bet))
    app.add_handler(CallbackQueryHandler(button))
    print("Bot running…")
    app.run_polling()

if __name__ == "__main__":
    main()
