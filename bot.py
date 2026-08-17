from http.server import HTTPServer, SimpleHTTPRequestHandler
import json
import os
import threading
import time
from datetime import datetime
import numpy as np
import pandas as pd
import pytz
import requests
import yfinance as yf

# Cloud Environment Variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
PORT = int(os.environ.get("PORT", 8080))

def send_telegram_message(msg, target_chat_id=None):
  cid = target_chat_id if target_chat_id else CHAT_ID
  url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
  payload = {"chat_id": cid, "text": msg, "parse_mode": "Markdown"}
  try:
    requests.post(url, json=payload, timeout=10)
  except Exception as e:
    print("Telegram send error:", e)


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
  for symbol, name, strike_step in [
      ("^NSEI", "NIFTY 50", 50),
      ("^NSEBANK", "BANKNIFTY", 100),
  ]:
    try:
      df = calculate_partha_signals(fetch_index_data(symbol))
      latest, prev = df.iloc[-1], df.iloc[-2]
      price_diff = latest["Close"] - prev["Close"]
      direction = "▲" if price_diff >= 0 else "▼"
      trend = (
          "BULLISH (LONG)"
          if latest["Position"] == 1
          else "BEARISH (SHORT)"
          if latest["Position"] == -1
          else "NEUTRAL"
      )

      atm_strike = int(round(latest["Close"] / strike_step) * strike_step)

      msg = (
          f"📊 *MOVEMENT DETECTED - {name}*\n"
          f"Price: `{latest['Close']:.2f}` ({direction} {abs(price_diff):.2f})\n"
          f"High: `{latest['High']:.2f}` | Low: `{latest['Low']:.2f}`\n"
          f"Algo Trend: *{trend}*"
      )
      send_telegram_message(msg)

      if latest["Buy_Signal"]:
        tgt1 = latest["Close"] + ((latest["Close"] - latest["Long_Stop"]) * 1.5)
        send_telegram_message(
            f"🟢 *VIP BUY SIGNAL - {name}*\n"
            f"• Entry: `{latest['Close']:.2f}`\n"
            f"• ATR Stop Loss: `{latest['Long_Stop']:.2f}`\n"
            f"• Target 1 (1:1.5): `{tgt1:.2f}`\n"
            f"• Suggested ATM Strike: `{atm_strike} CE`"
        )
      elif latest["Sell_Signal"]:
        tgt1 = latest["Close"] - (
            (latest["Short_Stop"] - latest["Close"]) * 1.5
        )
        send_telegram_message(
            f"🔴 *VIP SELL SIGNAL - {name}*\n"
            f"• Entry: `{latest['Close']:.2f}`\n"
            f"• ATR Stop Loss: `{latest['Short_Stop']:.2f}`\n"
            f"• Target 1 (1:1.5): `{tgt1:.2f}`\n"
            f"• Suggested ATM Strike: `{atm_strike} PE`"
        )
    except Exception as e:
      print(f"Error scanning {name}:", e)


def query_openrouter_ai(user_question, market_context):
  url = "https://openrouter.ai/api/v1/chat/completions"
  headers = {
      "Authorization": f"Bearer {OPENROUTER_API_KEY}",
      "Content-Type": "application/json",
      "HTTP-Referer": "https://render.com",
      "X-Title": "Partha Algo VIP Bot",
  }

  system_prompt = (
      "You are the Partha Algo Edge VIP AI Analyst. You provide precise, professional, "
      "and sharp trading insights for Indian Stock Market indices (NIFTY 50, BANKNIFTY). "
      "Always reference technical indicators (5/39 EMA, ATR trailing stops, 8-tick breakout) "
      "and emphasize strict risk management. Keep responses concise and formatted with bullet points."
  )

  prompt = (
      f"LIVE MARKET CONTEXT:\n{market_context}\n\nUSER QUESTION: {user_question}"
  )

  payload = {
      "model": "openai/gpt-oss-20b:free",
      "messages": [
          {"role": "system", "content": system_prompt},
          {"role": "user", "content": prompt},
      ],
  }

  try:
    response = requests.post(url, headers=headers, json=payload, timeout=20)
    if response.status_code == 200:
      return response.json()["choices"][0]["message"]["content"]
    else:
      return (
          f"⚠️ AI Engine Error ({response.status_code}): {response.text[:120]}"
      )
  except Exception as e:
    return f"⚠️ Connection Error: {str(e)}"


# Handles both private DMs and channel posts
def telegram_listener():
  offset = None
  url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
  print("Telegram command & channel update listener active...")

  while True:
    try:
      params = {
          "timeout": 20,
          "offset": offset,
          "allowed_updates": ["message", "channel_post"],
      }
      res = requests.get(url, params=params, timeout=25).json()

      if "result" in res:
        for update in res["result"]:
          offset = update["update_id"] + 1

          # Intercept private message OR channel broadcast
          msg_obj = update.get("message") or update.get("channel_post")
          if not msg_obj or "text" not in msg_obj:
            continue

          user_msg = msg_obj["text"].strip()
          sender_id = msg_obj["chat"]["id"]

          if user_msg.startswith("/ask_bot") or user_msg.startswith("/start"):
            query = (
                user_msg.replace("/ask_bot", "").replace("/start", "").strip()
            )

            if not query:
              send_telegram_message(
                  "👋 *Partha Algo AI Engine Active*\n"
                  "Ask any question regarding market trends, levels, or indicator concepts.\n\n"
                  "Example: `/ask_bot what is the current trend and setup for NIFTY?`",
                  target_chat_id=sender_id,
              )
              continue

            # Fetch live market snapshot for prompt grounding
            try:
              df = calculate_partha_signals(fetch_index_data("^NSEI"))
              latest = df.iloc[-1]
              trend = "BULLISH" if latest["Position"] == 1 else "BEARISH"
              sl = (
                  latest["Long_Stop"]
                  if trend == "BULLISH"
                  else latest["Short_Stop"]
              )
              market_snapshot = (
                  f"NIFTY 50 Spot: {latest['Close']:.2f} | Trend: {trend} | "
                  f"5 EMA: {latest['EMA_5']:.2f} | 39 EMA: {latest['EMA_39']:.2f} | Trailing SL: {sl:.2f}"
              )
            except Exception:
              market_snapshot = (
                  "NIFTY 50 live data currently offline / outside session hours."
              )

            ai_response = query_openrouter_ai(query, market_snapshot)
            formatted_reply = f"🤖 *PARTHA AI INTEL*\n━━━━━━━━━━━━━━━━━━━\n{ai_response}"
            send_telegram_message(formatted_reply, target_chat_id=sender_id)

    except Exception as e:
      time.sleep(2)


def algo_loop():
  ist = pytz.timezone("Asia/Kolkata")
  while True:
    now = datetime.now(ist)
    if (
        now.weekday() < 5
        and now.replace(hour=9, minute=15)
        <= now
        <= now.replace(hour=15, minute=30)
    ):
      execute_scan()
      time.sleep(300)
    else:
      time.sleep(60)


if __name__ == "__main__":
  threading.Thread(target=algo_loop, daemon=True).start()
  threading.Thread(target=telegram_listener, daemon=True).start()

  server = HTTPServer(("0.0.0.0", PORT), SimpleHTTPRequestHandler)
  print(f"Web server running on port {PORT}...")
  server.serve_forever()
