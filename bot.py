# bot.py
# Works with: Google Sheets + Telegram (pyTelegramBotAPI)
# Commands:
#   /health
#   /summary
#   /summary 23/09
#   /summary 23/09/2025 30/09/2025
#   /summary 2025-09-23
#
# Env vars required on Render/Heroku/local:
#   TELEGRAM_BOT_TOKEN
#   GOOGLE_CREDS_JSON      (full JSON string of the service account; not a file path)
#   SHEET_NAME  (default: "Bet Tracker")
#   SHEET_TAB   (default: "Bets")

import os, json, re, logging
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from dateutil import parser as dtparser
import pytz
import pandas as pd
import telebot
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
    raise RuntimeError("GOOGLE_CREDS_JSON missing (must be the entire JSON string)")

try:
    creds_dict = json.loads(creds_json)
except Exception as e:
    raise RuntimeError(f"GOOGLE_CREDS_JSON is not valid JSON: {e}")

scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
gc = gspread.authorize(creds)

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")
telebot.logger.setLevel(logging.INFO)  # show library logs

# --------------------------
# Helpers
# --------------------------
_NUM_RE = re.compile(r"[^\d.\-]")  # keep digits, dot, minus only

def parse_money(x):
    """Convert '£1,250.50' -> 1250.50; robust to weird inputs."""
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace("\u00a0", " ").strip()  # nbsp
    if not s:
        return 0.0
    s = s.replace(",", "").replace("£", "")
    parts = s.split(".")
    if len(parts) > 2:
        s = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(s)
    except ValueError:
        s2 = _NUM_RE.sub("", s)
        return float(s2) if s2 else 0.0

def parse_datetime_london(s):
    """
    Parses a date/time coming from Sheets.
    Returns timezone-aware datetime in Europe/London, or None if unparsable.
    """
    if isinstance(s, datetime):
        dt = s
    else:
        s = str(s).strip()
        dt = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
            "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
            "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
            "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"
        ):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                pass
        if dt is None:
            try:
                dt = dtparser.parse(s, dayfirst=True)
            except Exception:
                return None
    if dt.tzinfo is None:
        return TZ.localize(dt)
    return dt.astimezone(TZ)

def month_bounds_in_london(d=None):
    now_lon = datetime.now(TZ) if d is None else d.astimezone(TZ)
    start = TZ.localize(datetime(now_lon.year, now_lon.month, 1))
    end = start + relativedelta(months=1)  # exclusive
    return start, end

def parse_user_dates(args):
    """
    Args: [], [from], [from, to]
    Accepts 'YYYY-MM-DD', 'DD/MM[/YYYY]', 'DD-MM-YYYY', 'today', 'yesterday'
    Returns (start_inclusive, end_exclusive) as tz-aware London datetimes.
    """
    if len(args) == 0:
        return month_bounds_in_london()

    def parse_one(s):
        s = s.strip().lower()
        if s == "today":
            d = datetime.now(TZ)
            return TZ.localize(datetime(d.year, d.month, d.day))
        if s == "yesterday":
            d = datetime.now(TZ) - timedelta(days=1)
            return TZ.localize(datetime(d.year, d.month, d.day))
        # explicit formats
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m"):
            try:
                dt = datetime.strptime(s, fmt)
                if fmt == "%d/%m":
                    dt = dt.replace(year=datetime.now(TZ).year)
                return TZ.localize(datetime(dt.year, dt.month, dt.day))
            except ValueError:
                continue
        # fallback
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
        end = TZ.localize(datetime(start.year, start.month, 1)) + relativedelta(months=1)
    return start, end

def fmt_gbp(n): return f"£{n:,.2f}"
def fmt_pct(p): return f"{(p*100):.1f}%"  # p is 0..1

def load_bets_df():
    """
    Reads the Bets sheet into a pandas DataFrame with typed columns.
    Expected headers:
    ID | Date Placed | Event Date | Tipster | Selection | Odds (dec) | Bookmaker | Stake | Status | Return | Profit | Cumulative Profit
    """
    sh = gc.open(SHEET_NAME)
    ws = sh.worksheet(SHEET_TAB)
    rows = ws.get_all_records()
    if not rows:
        return pd.DataFrame(columns=["Date Placed","Tipster","Stake","Status","Return","Profit"])
    df = pd.DataFrame(rows)

    if "Date Placed" not in df.columns:
        raise RuntimeError("Sheet missing 'Date Placed' header")

    df["Date Placed"] = df["Date Placed"].apply(parse_datetime_london)
    df = df.dropna(subset=["Date Placed"])  # drop rows missing dates

    if "Tipster" not in df.columns:
        df["Tipster"] = ""

    for col in ("Stake", "Return", "Profit"):
        if col in df.columns:
            df[col] = df[col].apply(parse_money).astype(float)
        else:
            df[col] = 0.0

    if "Status" not in df.columns:
        df["Status"] = ""

    return df

def build_summary(df, start_lon, end_lon):
    """
    Returns (overall, per_tipster list)
    Dict keys: bets, wins, staked, returned, profit, winPct, pending
    """
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
            returned = float(
                g.apply(
                    lambda r: r["Return"] if r["Status"] == "Win"
                    else (r["Stake"] if r["Status"] == "Void" else 0.0),
                    axis=1
                ).sum()
            )
            profit = float(
                g.apply(lambda r: 0.0 if r["Status"] == "Void" else r["Profit"], axis=1).sum()
            )
            winPct = (wins / bets) if bets else 0.0
            pend_ct = int(pending_counts.get(tip, 0))
            records.append({
                "tipster": name, "bets": bets, "wins": wins, "staked": staked,
                "returned": returned, "profit": profit, "winPct": winPct, "pending": pend_ct
            })

    records.sort(key=lambda r: r["profit"], reverse=True)

    overall = {
        "bets": sum(r["bets"] for r in records) if records else 0,
        "wins": sum(r["wins"] for r in records) if records else 0,
        "staked": sum(r["staked"] for r in records) if records else 0.0,
        "returned": sum(r["returned"] for r in records) if records else 0.0,
        "profit": sum(r["profit"] for r in records) if records else 0.0,
        "winPct": (
            (sum(r["wins"] for r in records) / max(1, sum(r["bets"] for r in records)))
            if records else 0.0
        ),
        "pending": int(len(pending)),
    }
    return overall, records

def render_summary_text(start_lon, end_lon, overall, per_tipster):
    lines = []
    lines.append(f"*Summary* `{start_lon.strftime('%d %b %Y')} — {(end_lon - timedelta(days=1)).strftime('%d %b %Y')}`")
    lines.append("")
    lines.append(
        f"*Overall*  Bets: `{overall['bets']}` | Staked: `{fmt_gbp(overall['staked'])}` | Return: `{fmt_gbp(overall['returned'])}`"
    )
    roi = (overall["profit"] / overall["staked"]) if overall["staked"] > 0 else 0.0
    lines.append(
        f"Profit: *{fmt_gbp(overall['profit'])}* | ROI: `{fmt_pct(roi)}` | Win%: `{fmt_pct(overall['winPct'])}` | Pending: `{overall['pending']}`"
    )
    lines.append("")

    if not per_tipster:
        lines.append("_No settled bets in this range._")
        return "\n".join(lines)

    lines.append("*By Tipster*")
    for r in per_tipster:
        roi_t = (r["profit"] / r["staked"]) if r["staked"] > 0 else 0.0
        lines.append(
            f"• *{r['tipster']}* — Bets: `{r['bets']}` | Win%: `{fmt_pct(r['winPct'])}` | ROI: `{fmt_pct(roi_t)}`"
        )
        extra = f" | Pending: `{r['pending']}`" if r["pending"] else ""
        lines.append(
            f"   Staked: `{fmt_gbp(r['staked'])}` | Return: `{fmt_gbp(r['returned'])}` | Profit: *{fmt_gbp(r['profit'])}*{extra}"
        )
    return "\n".join(lines)

def send_long_message(chat_id, text, chunk_size=3900):
    """Telegram message limit ~4096 chars. Chunk by lines to be safe."""
    if len(text) <= chunk_size:
        bot.send_message(chat_id, text)
        return
    buf = []
    total = 0
    for line in text.split("\n"):
        if total + len(line) + 1 > chunk_size:
            bot.send_message(chat_id, "\n".join(buf))
            buf, total = [], 0
        buf.append(line)
        total += len(line) + 1
    if buf:
        bot.send_message(chat_id, "\n".join(buf))

# --------------------------
# Commands
# --------------------------
@bot.message_handler(commands=["start", "help"])
def cmd_start(msg):
    bot.reply_to(
        msg,
        "Hi! Use `/summary` to get results.\n\n"
        "*Examples:*\n"
        "`/summary` (this month)\n"
        "`/summary 23/09` (from 23 Sep to end of that month)\n"
        "`/summary 23/09/2025 30/09/2025`\n"
        "`/summary 2025-09-23`\n"
        "`/summary today`",
    )

# Match "/summary" and "/summary@YourBot" (works in DMs and groups)
@bot.message_handler(func=lambda m: bool(m.text) and m.text.lower().startswith("/summary"))
def cmd_summary(msg):
    try:
        parts = msg.text.split()
        args = parts[1:] if len(parts) > 1 else []
        bot.send_chat_action(msg.chat.id, "typing")
        start_lon, end_lon = parse_user_dates(args)
    except Exception:
        bot.reply_to(
            msg,
            "Bad date. Try:\n"
            "`/summary 2025-09-23 2025-09-30`\n"
            "`/summary 23/09/2025`\n"
            "`/summary 23/09`\n"
            "`/summary today`"
        )
        return

    try:
        df = load_bets_df()
        overall, per_tipster = build_summary(df, start_lon, end_lon)
        text = render_summary_text(start_lon, end_lon, overall, per_tipster)
        send_long_message(msg.chat.id, text)
    except Exception as e:
        bot.reply_to(msg, f"Summary error: `{e}`")

@bot.message_handler(commands=["health"])
def cmd_health(msg):
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
    print("Bot starting…")
    try:
        bot.delete_webhook(drop_pending_updates=True)  # ensure polling receives updates
        print("Webhook cleared.")
    except Exception as e:
        print("delete_webhook error:", e)
    bot.infinity_polling(timeout=60, long_polling_timeout=30, skip_pending=True, logger_level=logging.INFO)
