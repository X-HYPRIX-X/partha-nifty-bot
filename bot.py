import os
import threading
import time
import asyncio
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
import pytz
import pandas as pd
import numpy as np
import requests
import yfinance as yf
import discord
from discord import app_commands
from discord.ext import commands

# Cloud Environment Variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
SIGNALS_CHANNEL_ID = int(os.environ.get("SIGNALS_CHANNEL_ID", 0))
PORT = int(os.environ.get("PORT", 8080))

# Discord Client Configuration
intents = discord.Intents.default()
intents.message_content = True
discord_client = commands.Bot(command_prefix="!", intents=intents)

last_processed_candles = {"^NSEI": None, "^NSEBANK": None}
discord_async_loop = None


# --- TELEGRAM DISPATCH ---
def send_telegram_message(msg, target_chat_id=None):
    if not BOT_TOKEN:
        return
    cid = target_chat_id if target_chat_id else CHAT_ID
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": cid, "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("Telegram send error:", e)


# --- DISCORD DISPATCH ---
def send_discord_embed_sync(embed):
    if not discord_async_loop or not SIGNALS_CHANNEL_ID:
        return
    asyncio.run_coroutine_threadsafe(dispatch_discord_embed(embed), discord_async_loop)


async def dispatch_discord_embed(embed):
    try:
        channel = discord_client.get_channel(SIGNALS_CHANNEL_ID)
        if channel:
            await channel.send(embed=embed)
    except Exception as e:
        print("Discord dispatch error:", e)


# --- DATA & PARTHA SIGNAL ENGINE ---
def fetch_index_data(ticker_symbol):
    df = yf.Ticker(ticker_symbol).history(period="5d", interval="5m")
    df.dropna(inplace=True)
    return df


def calculate_partha_signals(df):
    lengthATR, multATR, lengthPartha = 3, 3.0, 8

    df["Prev_Close"] = df["Close"].shift(1)
    df["TR"] = df.apply(
        lambda r: max(
            r["High"] - r["Low"],
            abs(r["High"] - r["Prev_Close"]) if pd.notna(r["Prev_Close"]) else 0,
            abs(r["Low"] - r["Prev_Close"]) if pd.notna(r["Prev_Close"]) else 0,
        ),
        axis=1,
    )
    df["ATR"] = df["TR"].ewm(alpha=1 / lengthATR, adjust=False).mean()
    df["ATR_Val"] = df["ATR"] * multATR

    df["Highest_Close_ATR"] = df["Close"].rolling(window=lengthATR).max()
    df["Lowest_Close_ATR"] = df["Close"].rolling(window=lengthATR).min()
    df["Highest_High_Partha"] = (
        df["High"].shift(1).rolling(window=lengthPartha).max()
    )
    df["Lowest_Low_Partha"] = (
        df["Low"].shift(1).rolling(window=lengthPartha).min()
    )

    df["EMA_5"] = df["Close"].ewm(span=5, adjust=False).mean()
    df["EMA_39"] = df["Close"].ewm(span=39, adjust=False).mean()

    long_stops, short_stops = [0.0] * len(df), [0.0] * len(df)
    positions, buy_signals, sell_signals = (
        [0] * len(df),
        [False] * len(df),
        [False] * len(df),
    )

    for i in range(1, len(df)):
        curr_close, prev_close = df["Close"].iloc[i], df["Close"].iloc[i - 1]
        atr_val = df["ATR_Val"].iloc[i]

        curr_long_stop = df["Highest_Close_ATR"].iloc[i] - atr_val
        curr_short_stop = df["Lowest_Close_ATR"].iloc[i] + atr_val
        prev_long_stop, prev_short_stop = long_stops[i - 1], short_stops[i - 1]

        long_stops[i] = (
            max(curr_long_stop, prev_long_stop)
            if prev_close > prev_long_stop
            else curr_long_stop
        )
        short_stops[i] = (
            min(curr_short_stop, prev_short_stop)
            if prev_close < prev_short_stop
            else curr_short_stop
        )

        hh, ll, prev_pos = (
            df["Highest_High_Partha"].iloc[i],
            df["Lowest_Low_Partha"].iloc[i],
            positions[i - 1],
        )
        is_buy = (curr_close > hh) and (prev_pos != 1)
        is_sell = (curr_close < ll) and (prev_pos != -1)

        if is_buy:
            positions[i], buy_signals[i] = 1, True
        elif is_sell:
            positions[i], sell_signals[i] = -1, True
        else:
            positions[i] = prev_pos

    df["Long_Stop"], df["Short_Stop"], df["Position"] = (
        long_stops,
        short_stops,
        positions,
    )
    df["Buy_Signal"], df["Sell_Signal"] = buy_signals, sell_signals
    return df


def execute_scan():
    global last_processed_candles
    for symbol, name, strike_step in [
        ("^NSEI", "NIFTY 50", 50),
        ("^NSEBANK", "BANKNIFTY", 100),
    ]:
        try:
            df = calculate_partha_signals(fetch_index_data(symbol))
            latest = df.iloc[-1]
            latest_candle_time = df.index[-1]

            if last_processed_candles[symbol] == latest_candle_time:
                continue
            last_processed_candles[symbol] = latest_candle_time

            atm_strike = int(round(latest["Close"] / strike_step) * strike_step)

            if latest["Buy_Signal"]:
                tgt1 = latest["Close"] + ((latest["Close"] - latest["Long_Stop"]) * 1.5)
                send_telegram_message(
                    f"🟢 *VIP BUY SIGNAL - {name}*\n"
                    f"• Entry: `{latest['Close']:.2f}`\n"
                    f"• ATR Stop Loss: `{latest['Long_Stop']:.2f}`\n"
                    f"• Target 1 (1:1.5): `{tgt1:.2f}`\n"
                    f"• Suggested ATM Strike: `{atm_strike} CE`"
                )
                embed = discord.Embed(
                    title=f"🟢 VIP BUY SIGNAL — {name}",
                    color=discord.Color.green(),
                    timestamp=datetime.now(pytz.timezone("Asia/Kolkata"))
                )
                embed.add_field(name="Entry Price", value=f"`{latest['Close']:.2f}`", inline=True)
                embed.add_field(name="Trailing SL", value=f"`{latest['Long_Stop']:.2f}`", inline=True)
                embed.add_field(name="Target 1 (1:1.5)", value=f"`{tgt1:.2f}`", inline=True)
                embed.add_field(name="Suggested Strike", value=f"`{atm_strike} CE`", inline=False)
                embed.set_footer(text="Partha Algo Edge VIP • Automated Engine")
                send_discord_embed_sync(embed)

            elif latest["Sell_Signal"]:
                tgt1 = latest["Close"] - ((latest["Short_Stop"] - latest["Close"]) * 1.5)
                send_telegram_message(
                    f"🔴 *VIP SELL SIGNAL - {name}*\n"
                    f"• Entry: `{latest['Close']:.2f}`\n"
                    f"• ATR Stop Loss: `{latest['Short_Stop']:.2f}`\n"
                    f"• Target 1 (1:1.5): `{tgt1:.2f}`\n"
                    f"• Suggested ATM Strike: `{atm_strike} PE`"
                )
                embed = discord.Embed(
                    title=f"🔴 VIP SELL SIGNAL — {name}",
                    color=discord.Color.red(),
                    timestamp=datetime.now(pytz.timezone("Asia/Kolkata"))
                )
                embed.add_field(name="Entry Price", value=f"`{latest['Close']:.2f}`", inline=True)
                embed.add_field(name="Trailing SL", value=f"`{latest['Short_Stop']:.2f}`", inline=True)
                embed.add_field(name="Target 1 (1:1.5)", value=f"`{tgt1:.2f}`", inline=True)
                embed.add_field(name="Suggested Strike", value=f"`{atm_strike} PE`", inline=False)
                embed.set_footer(text="Partha Algo Edge VIP • Automated Engine")
                send_discord_embed_sync(embed)

        except Exception as e:
            print(f"Error scanning {name}:", e)


def send_market_close_summary():
    try:
        live_data = fetch_index_data("^NSEI")
        processed_data = calculate_partha_signals(live_data)
        ist = pytz.timezone("Asia/Kolkata")
        today_date = datetime.now(ist).date()
        today_data = processed_data[processed_data.index.date == today_date]

        if today_data.empty:
            today_data = processed_data.tail(75)

        day_open = today_data.iloc[0]["Open"]
        day_high = today_data["High"].max()
        day_low = today_data["Low"].min()
        day_close = today_data.iloc[-1]["Close"]

        net_points = day_close - day_open
        pct_change = (net_points / day_open) * 100
        arrow = "📈" if net_points >= 0 else "📉"
        final_trend = (
            "BULLISH"
            if today_data.iloc[-1]["Position"] == 1
            else "BEARISH"
            if today_data.iloc[-1]["Position"] == -1
            else "NEUTRAL"
        )

        summary_msg = (
            f"🏁 *DAILY MARKET CLOSE SUMMARY - NIFTY 50*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{arrow} *Closing Price:* `{day_close:.2f}` ({net_points:+.2f} / {pct_change:+.2f}%)\n"
            f"• *Open:* `{day_open:.2f}`\n"
            f"• *Day High:* `{day_high:.2f}`\n"
            f"• *Day Low:* `{day_low:.2f}`\n"
            f"• *Total Range:* `{day_high - day_low:.2f}` pts\n"
            f"• *Final Algo Position:* *{final_trend}*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Market closed. Automated engine standing by until 09:15 AM tomorrow."
        )
        send_telegram_message(summary_msg)
        
        embed = discord.Embed(
            title="🏁 DAILY MARKET CLOSE SUMMARY — NIFTY 50",
            color=discord.Color.blue(),
            timestamp=datetime.now(ist)
        )
        embed.add_field(name="Closing Price", value=f"`{day_close:.2f}` ({net_points:+.2f} / {pct_change:+.2f}%)", inline=False)
        embed.add_field(name="Day High / Low", value=f"`{day_high:.2f}` / `{day_low:.2f}`", inline=True)
        embed.add_field(name="Total Range", value=f"`{day_high - day_low:.2f}` pts", inline=True)
        embed.add_field(name="Final Algo Position", value=f"**{final_trend}**", inline=False)
        embed.set_footer(text="Partha Algo Edge VIP • Automated Summary")
        send_discord_embed_sync(embed)
    except Exception as e:
        print("Failed to generate market summary:", e)


# --- DISCORD SLASH COMMANDS ---
@discord_client.tree.command(name="analyze", description="Run Partha Algo AI analysis on Indian indices")
@app_commands.describe(index="Select market index to analyze")
@app_commands.choices(index=[
    app_commands.Choice(name="NIFTY 50", value="^NSEI"),
    app_commands.Choice(name="BANKNIFTY", value="^NSEBANK")
])
async def analyze(interaction: discord.Interaction, index: app_commands.Choice[str]):
    await interaction.response.defer()
    ticker = index.value
    name = index.name
    strike_step = 100 if "BANK" in name else 50

    try:
        df = calculate_partha_signals(fetch_index_data(ticker))
        latest = df.iloc[-1]

        is_bull = latest["Position"] == 1
        trend = "BULLISH (LONG) 🟢" if is_bull else "BEARISH (SHORT) 🔴"
        sl = latest["Long_Stop"] if is_bull else latest["Short_Stop"]
        color = discord.Color.green() if is_bull else discord.Color.red()
        atm_strike = int(round(latest["Close"] / strike_step) * strike_step)
        target = latest["Close"] + (abs(latest["Close"] - sl) * 1.5) if is_bull else latest["Close"] - (abs(latest["Close"] - sl) * 1.5)

        embed = discord.Embed(
            title=f"⚡ P.A.E.V. AI Intel Report — {name}",
            color=color,
            timestamp=datetime.now(pytz.timezone("Asia/Kolkata"))
        )
        embed.add_field(name="Current Spot Price", value=f"`{latest['Close']:.2f}`", inline=True)
        embed.add_field(name="Algo Trend State", value=f"**{trend}**", inline=True)
        embed.add_field(name="Suggested ATM Strike", value=f"`{atm_strike} {'CE' if is_bull else 'PE'}`", inline=True)
        embed.add_field(name="5 EMA / 39 EMA", value=f"`{latest['EMA_5']:.2f}` / `{latest['EMA_39']:.2f}`", inline=True)
        embed.add_field(name="Dynamic Trailing SL", value=f"`{sl:.2f}`", inline=True)
        embed.add_field(name="Projected Target (1:1.5)", value=f"`{target:.2f}`", inline=True)
        embed.set_footer(text="Partha Algo Edge VIP • Quantitative Engine")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error calculating intelligence: {str(e)}")


@discord_client.event
async def on_ready():
    global discord_async_loop
    discord_async_loop = asyncio.get_running_loop()
    try:
        synced = await discord_client.tree.sync()
        print(f"Discord Slash Commands Synced: {len(synced)} commands.")
    except Exception as e:
        print("Failed to sync slash commands:", e)
    print(f"P.A.E.V. Discord Bot online as {discord_client.user}")


# --- NATIVE TELEGRAM AI INTEL ---
def generate_market_intelligence(user_query):
    ticker = "^NSEBANK" if "bank" in user_query.lower() else "^NSEI"
    asset_name = "BANKNIFTY" if ticker == "^NSEBANK" else "NIFTY 50"
    strike_step = 100 if ticker == "^NSEBANK" else 50

    try:
        df = calculate_partha_signals(fetch_index_data(ticker))
        latest = df.iloc[-1]

        trend = "BULLISH 🟢" if latest["Position"] == 1 else "BEARISH 🔴"
        sl = latest["Long_Stop"] if latest["Position"] == 1 else latest["Short_Stop"]
        ema_bias = "Bullish (5 EMA > 39 EMA)" if latest["EMA_5"] > latest["EMA_39"] else "Bearish (5 EMA < 39 EMA)"
        atm_strike = int(round(latest["Close"] / strike_step) * strike_step)
        target = latest["Close"] + abs(latest["Close"] - sl) * 1.5 if latest["Position"] == 1 else latest["Close"] - abs(latest["Close"] - sl) * 1.5

        return (
            f"🤖 *PARTHA AI INTEL REPORT*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *Asset:* `{asset_name}`\n"
            f"💵 *Live Spot Price:* `{latest['Close']:.2f}`\n"
            f"📈 *Algo State:* *{trend}*\n\n"
            f"🔍 *Technical Breakdown:*\n"
            f"• *EMA Bias:* {ema_bias}\n"
            f"• *Dynamic Trailing SL:* `{sl:.2f}`\n"
            f"• *Computed Target (1:1.5):* `{target:.2f}`\n"
            f"• *Suggested ATM Strike:* `{atm_strike} {'CE' if latest['Position']==1 else 'PE'}`\n"
        )
    except Exception as e:
        return f"🤖 *PARTHA AI INTEL*\n━━━━━━━━━━━━━━━━━━━\n⚠️ Data unavailable: {str(e)}"


def telegram_listener():
    offset = None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    print("Telegram Listener active...")

    while True:
        try:
            params = {"timeout": 20, "offset": offset, "allowed_updates": ["message", "channel_post"]}
            res = requests.get(url, params=params, timeout=25).json()

            if "result" in res:
                for update in res["result"]:
                    offset = update["update_id"] + 1
                    msg_obj = update.get("message") or update.get("channel_post")

                    if not msg_obj or "text" not in msg_obj:
                        continue

                    user_msg = msg_obj["text"].strip()
                    sender_id = msg_obj["chat"]["id"]

                    if user_msg.startswith("/ask_bot") or user_msg.startswith("ask_bot") or user_msg.startswith("/start"):
                        query = user_msg.replace("/ask_bot", "").replace("ask_bot", "").replace("/start", "").strip()

                        if not query:
                            send_telegram_message(
                                "👋 *Partha Algo AI Engine Online*\n"
                                "Example: `/ask_bot what is the current trend of NIFTY?`",
                                target_chat_id=sender_id,
                            )
                            continue

                        analysis_reply = generate_market_intelligence(query)
                        send_telegram_message(analysis_reply, target_chat_id=sender_id)
        except Exception:
            time.sleep(2)


# --- 24/7 BACKGROUND WORKER ---
def algo_loop():
    ist = pytz.timezone("Asia/Kolkata")
    summary_sent_today = False

    while True:
        now = datetime.now(ist)
        is_weekday = now.weekday() < 5
        market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)

        if now.hour == 8:
            summary_sent_today = False

        if is_weekday and (market_open <= now <= market_close):
            try:
                execute_scan()
            except Exception as e:
                print("Scan error:", e)
            time.sleep(300)
        else:
            if is_weekday and now >= market_close and not summary_sent_today:
                send_market_close_summary()
                summary_sent_today = True
            time.sleep(60)


if __name__ == "__main__":
    print("Initializing threads...")
    threading.Thread(target=algo_loop, daemon=True).start()
    if BOT_TOKEN:
        threading.Thread(target=telegram_listener, daemon=True).start()

    def run_server():
        server = HTTPServer(("0.0.0.0", PORT), SimpleHTTPRequestHandler)
        print(f"Web server running on port {PORT}...")
        server.serve_forever()

    threading.Thread(target=run_server, daemon=True).start()

    if DISCORD_BOT_TOKEN:
        print("Starting Discord Client...")
        discord_client.run(DISCORD_BOT_TOKEN)
    else:
        print("DISCORD_BOT_TOKEN not found. Running headless.")
        while True:
            time.sleep(1)
