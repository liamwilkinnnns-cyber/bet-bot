# bot.py
# Requires: pyTelegramBotAPI, gspread, google-auth, pandas, python-dateutil, pytz
# env vars: TELEGRAM_BOT_TOKEN, GOOGLE_CREDS_JSON, SHEET_NAME=Bet Tracker, SHEET_TAB=Bets

import os, json, re
from datetime import datetime, timedelta
import pytz
from dateutil.relativedelta import relativedelta
from dateutil import parser as dtparser
import pandas as pd
import telebot
import gspread
from google.oauth2.service_account import Credentials

# --------------------------
# Config
# --------------------------
TZ = pytz.timezone("Europe/London")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SHEET_NAME = os.getenv("SHEET_NAME", "Bet Tracker").strip()
SHEET_TAB  = os.getenv("SHEET_TAB",  "Bets").strip()

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")

# Google client (service account JSON string in env)
creds_json = os.getenv("GOOGLE_CREDS_JSON", "")
if not creds_json:
    raise RuntimeError("GOOGLE_CREDS_JSON missing")
creds_dict = json.loads(creds_json)
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
gc = gspread.authorize(creds)

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")

# --------------------------
# Helpers
# --------------------------
NUM_RE = re.compile(r"[^\d.\-]")  # strip everything except digits, dot, minus

def parse_money(x):
    """'£1,250.50' -> 1250.50 ; also handles None/'' """
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace("\u00a0", " ").strip()  # nbsp
    if not s:
        return 0.0
    s = s.replace(",", "")  # drop thousands sep
    s = s.replace("£", "")
    # keep last dot if someone pasted "1.234.56"
    parts = s.split(".")
    if len(parts) > 2:
        s = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(s)
    except ValueError:
        s2 = NUM_RE.sub("", s)
        return float(s2) if s2 else 0.0

def parse_datetime_london(s):
    """
    Tries multiple formats; treats naive times as Europe/London.
    Returns timezone-aware datetime.
    """
    if isinstance(s, datetime):
        dt = s
    else:
        s = str(s).strip()
        # Common sheet formats first
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
                    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            # Fallback to dateutil
            try:
                # dayfirst for UK-like inputs
                dt = dtparser.parse(s, dayfirst=True)
            except Exception:
                return None
    if dt.tzinfo is None:
        return TZ.localize(dt)
    # If it's already tz-aware, normalize to London then return (we filter in London)
    return dt.astimezone(TZ)

def month_bounds_in_london(d=None):
    now_lon = datetime.now(TZ) if d is None else d.astimezone(TZ)
    start = TZ.localize(datetime(now_lon.year, now_lon.month, 1))
    end = (start + relativedelta(months=1))
    return start, end

def parse_user_dates(args):
    """
    Args can be [], [from], [from, to].
    Accepts 'YYYY-MM-DD' or 'DD/MM[/YYYY]' or 'DD-MM-YYYY'.
    Returns (start_in_London_inclusive, end_in_London_exclusive)
    """
    if len(args) == 0:
        return month_bounds_in_london()

    def parse_one(s):
        s = s.strip().lower()
        if s in ("today",):
            d = datetime.now(TZ)
            return TZ.localize(datetime(d.year, d.month, d.day))
        if s in ("yesterday",):
            d = datetime.now(TZ) - timedelta(days=1)
            return TZ.localize(datetime(d.year, d.month, d.day))
        # Try with/without year (assume current year)
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m"):
            try:
                dt = datetime.strptime(s, fmt)
                if fmt == "%d/%m":
                    dt = dt.replace(year=datetime.now(TZ).year)
                return TZ.localize(datetime(dt.year, dt.month, dt.day))
            except ValueError:
                continue
        # Fallback parser
        try:
            dt = dtparser.parse(s, dayfirst=True)
            if dt.tzinfo is None:
                dt = TZ.localize(datetime(dt.year, dt.month, dt.day))
            else:
                dt = dt.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            return dt
        except Exception:
            raise ValueError("Use YYYY-MM-DD or DD/MM[/YYYY]")

    start = parse_one(args[0])
    if len(args) >= 2:
        end_day = parse_one(args[1])
        end = end_day + timedelta(days=1)  # exclusive
    else:
        # end of that month
        end = (TZ.localize(datetime(start.year, start.month, 1)) + relativedelta(months=1))
    return start, end

def fmt_gbp(n):
    return f"£{n:,.2f}"

def fmt_pct(p):  # p is 0..1
    return f"{(p*100):.1f}%"

def load_bets_df():
    """
    Reads the Bets sheet into a pandas DataFrame with typed columns.
    Expected headers (A1:L1):
    ID | Date Placed | Event Date | Tipster | Selection | Odds (dec) | Bookmaker | Stake | Status | Return | Profit | Cumulative Profit
    """
    sh = gc.open(SHEET_NAME)
    ws = sh.worksheet(SHEET_TAB)
    rows = ws.get_all_records()  # list of dicts using header row
    if not rows:
        return pd.DataFrame(columns=["Date Placed","Tipster","Stake","Status","Return","Profit"])
    df = pd.DataFrame(rows)

    # Robust typing
    # Dates (assume "Date Placed" exists)
    if "Date Placed" in df.columns:
        df["Date Placed"] = df["Date Placed"].apply(parse_datetime_london)
    else:
        raise RuntimeError("Sheet is missing 'Date Placed' column in header row.")

    # Tipster
    if "Tipster" not in df.columns:
        df["Tipster"] = ""

    # Stake/Return/Profit numeric
    for col in ("Stake", "Return", "Profit"):
        if col in df.columns:
            df[col] = df[col].apply(parse_money).astype(float)
        else:
            df[col] = 0.0

    # Status
    if "Status" not in df.columns:
        df["Status"] = ""

    # Drop rows without a date
    df = df.dropna(subset=["Date Placed"])
    return df

def build_summary(df, start_lon, end_lon):
    """
    Returns (overall, per_tipster list)
    overall/per_tipster dict keys: bets, wins, staked, returned, profit, winPct, pending
    """
    # Filter window in London tz
    mask_window = (df["Date Placed"] >= start_lon) & (df["Date Placed"] < end_lon)
    dfx = df.loc[mask_window].copy()

    # Pending separate
    settled = dfx[dfx["Status"].isin(["Win","Void","Loss"])]
    pending = dfx[dfx["Status"] == "Pending"]

    # Aggregation
    records = []
    for tip, g in settled.groupby(dfx["Tipster"]):
        tip = tip or "—"
        bets = len(g)
        wins = int((g["Status"] == "Win").sum())
        staked = float(g["Stake"].sum())
        # Returned: Win -> Return; Void -> Stake; Loss -> 0
        returned = float(
            g.apply(lambda r: r["Return"] if r["Status"] == "Win" else (r["Stake"] if r["Status"] == "Void" else 0.0), axis=1).sum()
        )
        # Profit: Void contributes 0; Win/Loss use Profit col
        profit = float(
            g.apply(lambda r: 0.0 if r["Status"] == "Void" else r["Profit"], axis=1).sum()
        )
        winPct = (wins / bets) if bets > 0 else 0.0
        pend_ct = int((pending["Tipster"] == tip).sum())
        records.append(dict(tipster=tip, bets=bets, wins=wins, staked=staked, returned=returned, profit=profit, winPct=winPct, pending=pend_ct))

    # Sort by profit desc
    records.sort(key=lambda r: r["profit"], reverse=True)

    # Overall
    overall = {
        "bets": sum(r["bets"] for r in records),
        "wins": sum(r["wins"] for r in records),
        "staked": sum(r["staked"] for r in records),
        "returned": sum(r["returned"] for r in records),
        "profit": sum(r["profit"] for r in records),
        "winPct": (sum(r["wins"] for r in records) / max(1, sum(r["bets"] for r in records))) if sum(r["bets"] for r in records) else 0.0,
        "pending": int(len(pending)),
    }
    return overall, records

def render_summary_text(start_lon, end_lon, overall, per_tipster):
    lines = []
    lines.append(f"*Summary* `{start_lon.strftime('%d %b %Y')} — {(end_lon - timedelta(days=1)).strftime('%d %b %Y')}`")
    lines.append("")
    lines.append(f"*Overall*  Bets: `{overall['bets']}` | Staked: `{fmt_gbp(overall['staked'])}` | Return: `{fmt_gbp(overall['returned'])}`")
    lines.append(f"Profit: *{fmt_gbp(overall['profit'])}* | ROI: `{fmt_pct(overall['profit']/overall['staked'] if overall['staked']>0 else 0)}` | Win%: `{fmt_pct(overall['winPct'])}` | Pending: `{overall['pending']}`")
    lines.append("")

    if not per_tipster:
        lines.append("_No settled bets in this range._")
        return "\n".join(lines)

    lines.append("*By Tipster*")
    for r in per_tipster:
        lines.append(f"• *{r['tipster']}* — Bets: `{r['bets']}` | Win%: `{fmt_pct(r['winPct'])}` | ROI: `{fmt_pct(r['profit']/r['staked'] if r['staked']>0 else 0)}`")
        lines.append(f"   Staked: `{fmt_gbp(r['staked'])}` | Return: `{fmt_gbp(r['returned'])}` | Profit: *{fmt_gbp(r['profit'])}*"
                     + (f" | Pending: `{r['pending']}`" if r['pending'] else ""))
    return "\n".join(lines)

# --------------------------
# Commands
# --------------------------
@bot.message_handler(commands=["start", "help"])
def cmd_start(msg):
    bot.reply_to(msg,
        "Hi! Use `/summary` to get month-to-date results.\n\n"
        "Custom ranges:\n"
        "`/summary 23/09/2025 30/09/2025`\n"
        "`/summary 23/09`\n"
        "`/summary 2025-09-23 2025-09-30`\n"
        "`/summary today`\n",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=["summary"])
def cmd_summary(msg):
    try:
        args = msg.text.split()[1:]
        start_lon, end_lon = parse_user_dates(args)
    except Exception:
        bot.reply_to(msg, "Bad date. Try:\n`/summary 2025-09-23 2025-09-30`\n`/summary 23/09/2025`\n`/summary 23/09`\n`/summary today`")
        return

    try:
        df = load_bets_df()
        overall, per_tipster = build_summary(df, start_lon, end_lon)
        text = render_summary_text(start_lon, end_lon, overall, per_tipster)
        bot.reply_to(msg, text)
    except Exception as e:
        bot.reply_to(msg, f"Summary error: `{e}`")

@bot.message_handler(commands=["health"])
def cmd_health(msg):
    # quick connectivity check
    try:
        sh = gc.open(SHEET_NAME)
        ws = sh.worksheet(SHEET_TAB)
        _ = ws.acell("A1").value
        bot.reply_to(msg, "OK")
    except Exception as e:
        bot.reply_to(msg, f"Health error: `{e}`")

# --------------------------
# Run
# --------------------------
if __name__ == "__main__":
    print("Bot running…")
    # For Render/long polling
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
