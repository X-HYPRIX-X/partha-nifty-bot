from http.server import HTTPServer, SimpleHTTPRequestHandler
import os
import threading
import time
from datetime import datetime
import numpy as np
import pandas as pd
import pytz
import requests
import yfinance as yf

# Environment variables pulled from Render
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
PORT = int(os.environ.get("PORT", 8080))


def send_telegram_message(msg):
  url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
  payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
  try:
    response = requests.post(url, json=payload)
    print("Telegram status:", response.status_code)
  except Exception as e:
    print("Telegram error:", e)


def fetch_nifty_data():
  nifty = yf.Ticker("^NSEI")
  df = nifty.history(period="5d", interval="5m")
  df.dropna(inplace=True)
  return df


def calculate_partha_signals(df):
  lengthATR = 3
  multATR = 3.0
  lengthPartha = 8

  df["Prev_Close"] = df["Close"].shift(1)
  df["TR"] = df.apply(
      lambda row: max(
          row["High"] - row["Low"],
          abs(row["High"] - row["Prev_Close"])
          if pd.notna(row["Prev_Close"])
          else 0,
          abs(row["Low"] - row["Prev_Close"])
          if pd.notna(row["Prev_Close"])
          else 0,
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

  df["Bull_Cross"] = (df["EMA_5"] > df["EMA_39"]) & (
      df["EMA_5"].shift(1) <= df["EMA_39"].shift(1)
  )
  df["Bear_Cross"] = (df["EMA_5"] < df["EMA_39"]) & (
      df["EMA_5"].shift(1) >= df["EMA_39"].shift(1)
  )

  long_stops = [0.0] * len(df)
  short_stops = [0.0] * len(df)
  positions = [0] * len(df)
  buy_signals = [False] * len(df)
  sell_signals = [False] * len(df)

  for i in range(len(df)):
    if i == 0:
      continue

    curr_close = df["Close"].iloc[i]
    prev_close = df["Close"].iloc[i - 1]
    atr_val = df["ATR_Val"].iloc[i]

    curr_long_stop = df["Highest_Close_ATR"].iloc[i] - atr_val
    curr_short_stop = df["Lowest_Close_ATR"].iloc[i] + atr_val
    prev_long_stop = long_stops[i - 1]
    prev_short_stop = short_stops[i - 1]

    if prev_close > prev_long_stop:
      long_stops[i] = max(curr_long_stop, prev_long_stop)
    else:
      long_stops[i] = curr_long_stop

    if prev_close < prev_short_stop:
      short_stops[i] = min(curr_short_stop, prev_short_stop)
    else:
      short_stops[i] = curr_short_stop

    hh_partha = df["Highest_High_Partha"].iloc[i]
    ll_partha = df["Lowest_Low_Partha"].iloc[i]
    prev_pos = positions[i - 1]

    is_buy = (curr_close > hh_partha) and (prev_pos != 1)
    is_sell = (curr_close < ll_partha) and (prev_pos != -1)

    if is_buy:
      positions[i] = 1
      buy_signals[i] = True
    elif is_sell:
      positions[i] = -1
      sell_signals[i] = True
    else:
      positions[i] = prev_pos

  df["Long_Stop"] = long_stops
  df["Short_Stop"] = short_stops
  df["Position"] = positions
  df["Buy_Signal"] = buy_signals
  df["Sell_Signal"] = sell_signals

  return df


def execute_scan():
  live_data = fetch_nifty_data()
  processed_data = calculate_partha_signals(live_data)
  latest = processed_data.iloc[-1]
  prev = processed_data.iloc[-2]

  price_change = latest["Close"] - prev["Close"]
  change_direction = "▲" if price_change >= 0 else "▼"
  trend_status = (
      "BULLISH (LONG)"
      if latest["Position"] == 1
      else "BEARISH (SHORT)"
      if latest["Position"] == -1
      else "NEUTRAL"
  )

  # Movement update
  movement_msg = (
      f"📊 *MOVEMENT DETECTED - NIFTY 50*\n"
      f"Current Price: `{latest['Close']:.2f}` ({change_direction}"
      f" {abs(price_change):.2f})\n"
      f"High: `{latest['High']:.2f}` | Low: `{latest['Low']:.2f}`\n"
      f"Algo Trend: *{trend_status}*"
  )
  send_telegram_message(movement_msg)

  # Signal alerts
  if latest["Buy_Signal"]:
    msg = (
        f"🟢 *PARTHA OSC: BUY SIGNAL*\nTicker: NIFTY 50\nPrice:"
        f" `{latest['Close']:.2f}`\nStop Loss: `{latest['Long_Stop']:.2f}`"
    )
    send_telegram_message(msg)
  elif latest["Sell_Signal"]:
    msg = (
        f"🔴 *PARTHA OSC: SELL SIGNAL*\nTicker: NIFTY 50\nPrice:"
        f" `{latest['Close']:.2f}`\nStop Loss: `{latest['Short_Stop']:.2f}`"
    )
    send_telegram_message(msg)
  elif latest["Bull_Cross"]:
    msg = (
        f"⚡ *EMA BULLISH CROSS (5/39)*\nTicker: NIFTY 50\nPrice:"
        f" `{latest['Close']:.2f}`"
    )
    send_telegram_message(msg)
  elif latest["Bear_Cross"]:
    msg = (
        f"⚠️ *EMA BEARISH CROSS (5/39)*\nTicker: NIFTY 50\nPrice:"
        f" `{latest['Close']:.2f}`"
    )
    send_telegram_message(msg)


def algo_loop():
  ist = pytz.timezone("Asia/Kolkata")
  print("Market scanning loop started...")

  while True:
    now = datetime.now(ist)
    is_weekday = now.weekday() < 5
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)

    if is_weekday and (market_open <= now <= market_close):
      print(f"[SCAN] Running at {now.strftime('%I:%M:%S %p')} IST")
      try:
        execute_scan()
      except Exception as e:
        print("Scan error:", e)
      time.sleep(300)
    else:
      time.sleep(60)


if __name__ == "__main__":
  # Start the background trading loop
  trading_thread = threading.Thread(target=algo_loop, daemon=True)
  trading_thread.start()

  # Serve HTTP so Render recognizes the service as live
  server = HTTPServer(("0.0.0.0", PORT), SimpleHTTPRequestHandler)
  print(f"Web server running on port {PORT}...")
  server.serve_forever()
