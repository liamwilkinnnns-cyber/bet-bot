# bot.py
# Sheets-powered Telegram bot: log bets + settle + summaries
# Commands:
#   /health
#   /summary [from] [to]
#   /log Tipster / Selection / Odds / Bookmaker / Stake
#   /win <ID>   /void <ID>   /loss <ID>
# Free-text logging in DMs:
#   Tipster / Selection / Odds / Bookmaker / Stake
#
# Env vars:
#   TELEGRAM_BOT_TOKEN
#   GOOGLE_CREDS_JSON
#   SHEET_NAME (default: Bet Tracker)
#   SHEET_TAB  (default: Bets)

import os, json, re, logging, time, secrets
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from dateutil import parser as dtparser
import pytz
import pandas as pd
import telebot
from telebot.apihelper import ApiTelegramException
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import gspread
from google.oauth2.service_account import Credentials

# --------------------------
# Config & clients
# --------------------------
TZ = pytz.timezone("Europe/London")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SHEET_NAME = os.getenv("SHEET_NAME", "Bet Tracker").strip()
SHEET_TAB  = os.getenv("SHEET_TAB",  "Bets").strip()

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")

creds_json = os.getenv("GOOGLE_CREDS_JSON", "")
if not creds_json:
    raise RuntimeError("GOOGLE_CREDS_JSON missing (full JSON string)")
creds_dict = json.loads(creds_json)

scopes = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
gc = gspread.authorize(creds)

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")
telebot.logger.setLevel(logging.INFO)

# --------------------------
# Helpers
# --------------------------
_NUM_RE = re.compile(r"[^\d.\-]")  # keep digits, dot, minus

def now_london_with_seconds():
    return datetime.now(TZ).replace(microsecond=0)

def gen_id():
    return secrets.token_hex(4).upper()

def parse_money(x):
    if x is None: return 0.0
    if isinstance(x, (int, float)): return float(x)
    s = str(x).replace("\u00a0", " ").strip()
    if not s: return 0.0
    s = s.replace(",", "").replace("£", "")
    parts = s.split(".")
    if len(parts) > 2: s = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(s)
    except ValueError:
        s2 = _NUM_RE.sub("", s)
        return float(s2) if s2 else 0.0

def parse_odds_to_decimal(s):
    raw = str(s).strip()
    if "/" in raw:
        try:
            a, b = raw.split("/", 1)
            dec = 1.0 + (float(a.strip()) / float(b.strip()))
            if dec <= 1.0: raise ValueError
            return dec
        except Exception:
            raise ValueError(f"Bad fractional odds: {s}")
    raw = raw.replace(",", ".")
    try:
        dec = float(raw)
        if dec <= 1.0: raise ValueError
        return dec
    except Exception:
        raise ValueError(f"Bad decimal odds: {s}")

def parse_datetime_london(s):
    if isinstance(s, datetime):
        dt = s
    else:
        s = str(s).strip(); dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S","%Y-%m-%d %H:%M",
                    "%d/%m/%Y %H:%M:%S","%d/%m/%Y %H:%M",
                    "%d-%m-%Y %H:%M:%S","%d-%m-%Y %H:%M",
                    "%Y-%m-%d","%d/%m/%Y","%d-%m-%Y"):
            try:
                dt = datetime.strptime(s, fmt); break
            except ValueError:
                pass
        if dt is None:
            try: dt = dtparser.parse(s, dayfirst=True)
            except Exception: return None
    return TZ.localize(dt) if dt.tzinfo is None else dt.astimezone(TZ)

def month_bounds_in_london(d=None):
    now_lon = datetime.now(TZ) if d is None else d.astimezone(TZ)
    start = TZ.localize(datetime(now_lon.year, now_lon.month, 1))
    end = start + relativedelta(months=1)
    return start, end

def parse_user_dates(args):
    if len(args) == 0: return month_bounds_in_london()
    def parse_one(s):
        s = s.strip().lower()
        if s == "today":
            d = datetime.now(TZ); return TZ.localize(datetime(d.year, d.month, d.day))
        if s == "yesterday":
            d = datetime.now(TZ) - timedelta(days=1); return TZ.localize(datetime(d.year, d.month, d.day))
        for fmt in ("%Y-%m-%d","%d/%m/%Y","%d-%m-%Y","%d/%m"):
            try:
                dt = datetime.strptime(s, fmt)
                if fmt == "%d/%m": dt = dt.replace(year=datetime.now(TZ).year)
                return TZ.localize(datetime(dt.year, dt.month, dt.day))
            except ValueError:
                continue
        dt = dtparser.parse(s, dayfirst=True)
        if dt.tzinfo is None:
            dt = TZ.localize(datetime(dt.year, dt.month, dt.day))
        else:
            dt = dt.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        return dt
    start = parse_one(args[0])
    end = (parse_one(args[1]) + timedelta(days=1)) if len(args) >= 2 else (TZ.localize(datetime(start.year, start.month, 1)) + relativedelta(months=1))
    return start, end

def fmt_gbp(n): return f"£{n:,.2f}"
def fmt_pct(p): return f"{(p*100):.1f}%"

# --------------------------
# Sheets I/O
# --------------------------
def ws_open():
    return gc.open(SHEET_NAME).worksheet(SHEET_TAB)

def append_bet_row(tipster, selection, odds_dec, bookmaker, stake, event_dt=None):
    ws = ws_open()
    bet_id = gen_id()
    date_placed = now_london_with_seconds().strftime("%Y-%m-%d %H:%M:%S")
    event_date = event_dt.strftime("%Y-%m-%d %H:%M:%S") if event_dt else ""
    row = [
        bet_id,                   # A ID
        date_placed,              # B Date Placed
        event_date,               # C Event Date
        tipster,                  # D Tipster
        selection,                # E Selection
        f"{odds_dec:.2f}",        # F Odds (dec)
        bookmaker,                # G Bookmaker
        stake,                    # H Stake
        "Pending",                # I Status
        "", "", ""                # J Return, K Profit, L Cumulative Profit
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return bet_id

def sheet_find_bet_row(bet_id):
    """
    Returns (ws, row_index) where the ID is found in column A; raises if not found.
    """
    ws = ws_open()
    cell = ws.find(bet_id)  # searches entire sheet
    if not cell or cell.col != 1:
        # ensure it's in column A (ID)
        # fallback: scan column A
        colA = ws.col_values(1)
        try:
            idx = colA.index(bet_id) + 1
            return ws, idx
        except ValueError:
            raise RuntimeError(f"Bet ID '{bet_id}' not found")
    return ws, cell.row

def settle_bet(bet_id, status):
    """
    status in {"Win","Void","Loss"}.
    Reads Odds (F) & Stake (H), writes Status (I), Return (J), Profit (K).
    """
    status = status.capitalize()
    if status not in {"Win","Void","Loss"}:
        raise ValueError("Status must be Win, Void or Loss")

    ws, row = sheet_find_bet_row(bet_id)
    vals = ws.row_values(row)
    # Ensure enough columns exist
    while len(vals) < 11:
        vals.append("")
    # Columns (1-indexed): F=6 odds, H=8 stake
    odds_dec = parse_money(vals[5])  # F
    stake = parse_money(vals[7])     # H
    if odds_dec <= 1.0:
        # fallback: try raw parse if someone stored fractional
        try: odds_dec = parse_odds_to_decimal(vals[5])
        except Exception: pass

    if status == "Win":
        ret = odds_dec * stake
        prof = ret - stake
    elif status == "Void":
        ret = stake
        prof = 0.0
    else:  # Loss
        ret = 0.0
        prof = -stake

    # Update I/J/K in one call
    ws.update(f"I{row}:K{row}", [[status, ret, prof]], value_input_option="USER_ENTERED")
    return {"row": row, "status": status, "return": ret, "profit": prof, "stake": stake, "odds": odds_dec}

def load_bets_df():
    ws = ws_open()
    rows = ws.get_all_records()
    if not rows:
        return pd.DataFrame(columns=["Date Placed","Tipster","Stake","Status","Return","Profit"])
    df = pd.DataFrame(rows)
    if "Date Placed" not in df.columns:
        raise RuntimeError("Sheet missing 'Date Placed' header")
    df["Date Placed"] = df["Date Placed"].apply(parse_datetime_london)
    df = df.dropna(subset=["Date Placed"])
    if "Tipster" not in df.columns: df["Tipster"] = ""
    for col in ("Stake","Return","Profit"):
        df[col] = df[col].apply(parse_money).astype(float) if col in df.columns else 0.0
    if "Status" not in df.columns: df["Status"] = ""
    return df

# --------------------------
# Summary aggregation
# --------------------------
def build_summary(df, start_lon, end_lon):
    mask = (df["Date Placed"] >= start_lon) & (df["Date Placed"] < end_lon)
    dfx = df.loc[mask].copy()
    settled = dfx[dfx["Status"].isin(["Win","Void","Loss"])]
    pending = dfx[dfx["Status"] == "Pending"]
    pending_counts = pending.groupby("Tipster").size().to_dict() if len(pending) else {}
    records = []
    if len(settled):
        for tip, g in settled.groupby("Tipster"):
            name = tip or "—"
            bets = int(len(g))
            wins = int((g["Status"] == "Win").sum())
            staked = float(g["Stake"].sum())
            returned = float(g.apply(lambda r: r["Return"] if r["Status"]=="Win" else (r["Stake"] if r["Status"]=="Void" else 0.0), axis=1).sum())
            profit = float(g.apply(lambda r: 0.0 if r["Status"]=="Void" else r["Profit"], axis=1).sum())
            winPct = (wins / bets) if bets else 0.0
            pend_ct = int(pending_counts.get(tip, 0))
            records.append({"tipster":name,"bets":bets,"wins":wins,"staked":staked,"returned":returned,"profit":profit,"winPct":winPct,"pending":pend_ct})
    records.sort(key=lambda r: r["profit"], reverse=True)
    overall = {
        "bets": sum(r["bets"] for r in records) if records else 0,
        "wins": sum(r["wins"] for r in records) if records else 0,
        "staked": sum(r["staked"] for r in records) if records else 0.0,
        "returned": sum(r["returned"] for r in records) if records else 0.0,
        "profit": sum(r["profit"] for r in records) if records else 0.0,
        "winPct": (sum(r["wins"] for r in records) / max(1, sum(r["bets"] for r in records))) if records else 0.0,
        "pending": int(len(pending)),
    }
    return overall, records

def render_summary_text(start_lon, end_lon, overall, per_tipster):
    lines = []
    lines.append(f"*Summary* `{start_lon.strftime('%d %b %Y')} — {(end_lon - timedelta(days=1)).strftime('%d %b %Y')}`")
    lines.append("")
    lines.append(f"*Overall*  Bets: `{overall['bets']}` | Staked: `{fmt_gbp(overall['staked'])}` | Return: `{fmt_gbp(overall['returned'])}`")
    roi = (overall["profit"]/overall["staked"]) if overall["staked"]>0 else 0.0
    lines.append(f"Profit: *{fmt_gbp(overall['profit'])}* | ROI: `{fmt_pct(roi)}` | Win%: `{fmt_pct(overall['winPct'])}` | Pending: `{overall['pending']}`")
    lines.append("")
    if not per_tipster:
        lines.append("_No settled bets in this range._")
        return "\n".join(lines)
    lines.append("*By Tipster*")
    for r in per_tipster:
        roi_t = (r["profit"]/r["staked"]) if r["staked"]>0 else 0.0
        extra = f" | Pending: `{r['pending']}`" if r["pending"] else ""
        lines.append(f"• *{r['tipster']}* — Bets: `{r['bets']}` | Win%: `{fmt_pct(r['winPct'])}` | ROI: `{fmt_pct(roi_t)}`")
        lines.append(f"   Staked: `{fmt_gbp(r['staked'])}` | Return: `{fmt_gbp(r['returned'])}` | Profit: *{fmt_gbp(r['profit'])}*{extra}")
    return "\n".join(lines)

def send_long_message(chat_id, text, chunk_size=3900):
    if len(text) <= chunk_size:
        bot.send_message(chat_id, text); return
    buf, total = [], 0
    for line in text.split("\n"):
        if total + len(line) + 1 > chunk_size:
            bot.send_message(chat_id, "\n".join(buf)); buf, total = [], 0
        buf.append(line); total += len(line) + 1
    if buf: bot.send_message(chat_id, "\n".join(buf))

# --------------------------
# Bet logging + settlement
# --------------------------
def process_bet_line(line: str):
    parts = [p.strip() for p in line.split("/")]
    parts = [p for p in parts if p != ""]
    if len(parts) != 5:
        raise ValueError("Please send: Tipster / Selection / Odds / Bookmaker / Stake")
    tipster, selection, odds_raw, bookmaker, stake_raw = parts
    odds_dec = parse_odds_to_decimal(odds_raw)
    stake = parse_money(stake_raw)
    if stake <= 0: raise ValueError("Stake must be greater than 0")
    bet_id = append_bet_row(tipster, selection, odds_dec, bookmaker, stake, event_dt=None)
    return bet_id, tipster, selection, odds_dec, bookmaker, stake

def settle_and_reply(msg, bet_id, status):
    res = settle_bet(bet_id, status)
    bot.reply_to(
        msg,
        f"✅ *{status}* set for `{bet_id}`\n"
        f"Odds `{res['odds']:.2f}`  |  Stake `{fmt_gbp(res['stake'])}`\n"
        f"Return `{fmt_gbp(res['return'])}`  |  Profit `{fmt_gbp(res['profit'])}`"
    )

# --------------------------
# Commands & Handlers
# --------------------------
@bot.message_handler(commands=["start","help"])
def cmd_start(msg):
    bot.reply_to(msg,
        "Log a bet:\n"
        "`/log Tipster / Selection / Odds / Bookmaker / Stake`\n"
        "Example: `/log Lewis / 4 fold acca / 11.50 / Bet365 / 50`\n"
        "In DM, you can also paste the line without /log.\n\n"
        "Settle a bet:\n"
        "`/win <ID>`   `/void <ID>`   `/loss <ID>`\n\n"
        "Summaries:\n"
        "`/summary`  or  `/summary 23/09`  or  `/summary 23/09/2025 30/09/2025`  or  `/summary today`"
    )

# Summary
@bot.message_handler(func=lambda m: bool(m.text) and m.text.lower().startswith("/summary"))
def cmd_summary(msg):
    try:
        args = msg.text.split()[1:]
        bot.send_chat_action(msg.chat.id, "typing")
        start_lon, end_lon = parse_user_dates(args)
    except Exception:
        bot.reply_to(msg,
            "Bad date. Try:\n"
            "`/summary 2025-09-23 2025-09-30`\n"
            "`/summary 23/09/2025`\n"
            "`/summary 23/09`\n"
            "`/summary today`"
        ); return
    try:
        df = load_bets_df()
        overall, per_tipster = build_summary(df, start_lon, end_lon)
        send_long_message(msg.chat.id, render_summary_text(start_lon, end_lon, overall, per_tipster))
    except Exception as e:
        bot.reply_to(msg, f"Summary error: `{e}`")

@bot.message_handler(commands=["health"])
def cmd_health(msg):
    try:
        _ = ws_open().acell("A1").value
        bot.reply_to(msg, "OK")
    except Exception as e:
        bot.reply_to(msg, f"Health error: `{e}`")

# /log command (works in groups)
@bot.message_handler(commands=["log"])
def cmd_log(msg):
    line = msg.text.split(" ", 1)[1].strip() if len(msg.text.split(" ", 1)) > 1 else ""
    if not line:
        bot.reply_to(msg,
            "Usage:\n`/log Tipster / Selection / Odds / Bookmaker / Stake`\n"
            "Example:\n`/log Lewis / 4 fold acca / 11.50 / Bet365 / 50`"
        ); return
    try:
        bot.send_chat_action(msg.chat.id, "typing")
        bet_id, tipster, selection, odds_dec, bookmaker, stake = process_bet_line(line)

        # Inline settle buttons
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("✅ Win", callback_data=f"settle|{bet_id}|Win"),
            InlineKeyboardButton("⛔️ Loss", callback_data=f"settle|{bet_id}|Loss"),
            InlineKeyboardButton("↩️ Void", callback_data=f"settle|{bet_id}|Void"),
        )
        bot.reply_to(
            msg,
            f"✅ *Logged*\n"
            f"ID: `{bet_id}`\n"
            f"Tipster: *{tipster}*\n"
            f"Selection: `{selection}`\n"
            f"Odds: `{odds_dec:.2f}`  |  Bookmaker: `{bookmaker}`  |  Stake: `{fmt_gbp(stake)}`\n"
            f"Status: `Pending`\n\n"
            f"_Tap a button to settle now, or use `/win {bet_id}`, `/loss {bet_id}`, `/void {bet_id}` later._",
            reply_markup=kb
        )
    except Exception as e:
        bot.reply_to(msg, f"Could not log that bet: `{e}`")

# DM free-text logging (paste line without /log)
@bot.message_handler(func=lambda m: m.chat.type == "private" and bool(m.text) and "/" in m.text and not m.text.startswith("/"))
def log_bet_free_text_dm(msg):
    try:
        bot.send_chat_action(msg.chat.id, "typing")
        bet_id, tipster, selection, odds_dec, bookmaker, stake = process_bet_line(msg.text)
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("✅ Win", callback_data=f"settle|{bet_id}|Win"),
            InlineKeyboardButton("⛔️ Loss", callback_data=f"settle|{bet_id}|Loss"),
            InlineKeyboardButton("↩️ Void", callback_data=f"settle|{bet_id}|Void"),
        )
        bot.reply_to(
            msg,
            f"✅ *Logged*\n"
            f"ID: `{bet_id}`\n"
            f"Tipster: *{tipster}*\n"
            f"Selection: `{selection}`\n"
            f"Odds: `{odds_dec:.2f}`  |  Bookmaker: `{bookmaker}`  |  Stake: `{fmt_gbp(stake)}`\n"
            f"Status: `Pending`",
            reply_markup=kb
        )
    except Exception as e:
        bot.reply_to(msg, f"Could not log that bet: `{e}`\nSend like:\n`Tipster / Selection / Odds / Bookmaker / Stake`")

# Inline button handler
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("settle|"))
def cb_settle(call):
    try:
        _, bet_id, status = call.data.split("|", 2)
        res = settle_bet(bet_id, status)
        bot.answer_callback_query(call.id, f"{status} saved")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=(f"📝 Bet `{bet_id}` settled as *{status}*\n"
                  f"Odds `{res['odds']:.2f}`  |  Stake `{fmt_gbp(res['stake'])}`\n"
                  f"Return `{fmt_gbp(res['return'])}`  |  Profit `{fmt_gbp(res['profit'])}`"),
            parse_mode="Markdown"
        )
    except Exception as e:
        bot.answer_callback_query(call.id, "Error")
        bot.send_message(call.message.chat.id, f"Settle error: `{e}`")

# Text commands to settle later
@bot.message_handler(commands=["win","void","loss"])
def cmd_settle_text(msg):
    parts = msg.text.split()
    if len(parts) != 2:
        bot.reply_to(msg, f"Usage: `/{parts[0][1:]} <ID>`"); return
    bet_id = parts[1].strip().upper()
    status = msg.text.split()[0][1:].capitalize()
    try:
        bot.send_chat_action(msg.chat.id, "typing")
        settle_and_reply(msg, bet_id, status)
    except Exception as e:
        bot.reply_to(msg, f"Settle error: `{e}`")

# --------------------------
# Run (single instance, auto-retry)
# --------------------------
if __name__ == "__main__":
    print("Bot starting…")
    try:
        bot.delete_webhook(drop_pending_updates=True); print("Webhook cleared.")
    except Exception as e:
        print("delete_webhook error:", e)
    time.sleep(2)
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30, skip_pending=True, logger_level=logging.INFO)
        except ApiTelegramException as e:
            if getattr(e, "result", None) is not None and getattr(e.result, "status_code", None) == 409:
                print("409 Conflict (another getUpdates). Retrying in 10s…"); time.sleep(10); continue
            print("Telegram API error:", e); time.sleep(5)
        except Exception as e:
            print("Polling crashed:", e); time.sleep(5)
