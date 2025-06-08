import os
import json
import time
import threading
import schedule
import pandas as pd
import yfinance as yf
import ta
import requests
from flask import Flask, request, jsonify, render_template_string
from openai import OpenAI
from functools import lru_cache
import time

@lru_cache(maxsize=32)
def cached_get_stock_data(ticker):
    return get_stock_data(ticker)

@lru_cache(maxsize=32)
def cached_get_fundamentals(ticker):
    return get_fundamentals(ticker)

# === CONFIGURATION ===
OPENAI_API_KEY = ""  # Your OpenAI API key here
POSTMARK_API_KEY = ""
POSTMARK_SENDER_EMAIL = ""
RECIPIENT_EMAIL = ""
HISTORY_FILE = "chat_history.json"
FAVORITES_FILE = "favorite_stocks.json"
MAX_HISTORY = 20
NEWS_API_KEY = ""
# === OpenAI client ===
client = OpenAI(api_key=OPENAI_API_KEY)

# === Flask app ===
app = Flask(__name__)

# === Load favorite stocks ===
def load_favorite_stocks():
    if os.path.exists(FAVORITES_FILE):
        with open(FAVORITES_FILE, "r") as f:
            return json.load(f)
    return []

# === Save favorite stocks ===
def save_favorite_stocks(stocks):
    with open(FAVORITES_FILE, "w") as f:
        json.dump(stocks, f)

# === Load chat history ===
def load_chat_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return []

# === Save chat history ===
def save_chat_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history[-MAX_HISTORY:], f)


def get_recent_news():
    url = (
        f"https://newsapi.org/v2/top-headlines?category=business&pageSize=30&language=en&apiKey={NEWS_API_KEY}"
    )
    response = requests.get(url)
    if response.status_code == 200:
        articles = response.json().get("articles", [])
        return [a['title'] for a in articles if 'title' in a]
    else:
        print("Failed to fetch news:", response.status_code, response.text)
        return []

# === Data Fetching ===
def get_stock_data(ticker):
    df = yf.download(ticker, period="3mo", progress=False)
    if df.empty or 'Close' not in df:
        raise ValueError(f"No data returned for {ticker}")
    df = df.reset_index()
    close = df['Close']
    volume = df['Volume']

    df['RSI'] = ta.momentum.RSIIndicator(close=close).rsi()
    df['SMA_20'] = ta.trend.SMAIndicator(close=close, window=20).sma_indicator()
    df['SMA_50'] = ta.trend.SMAIndicator(close=close, window=50).sma_indicator()
    macd = ta.trend.MACD(close=close)
    df['MACD'] = macd.macd()
    df['MACD_signal'] = macd.macd_signal()
    bb = ta.volatility.BollingerBands(close=close)
    df['BB_upper'] = bb.bollinger_hband()
    df['BB_lower'] = bb.bollinger_lband()

    return df[['Close', 'Volume', 'RSI', 'SMA_20', 'SMA_50', 'MACD', 'MACD_signal', 'BB_upper', 'BB_lower']].tail(1)

def get_fundamentals(ticker):
    stock = yf.Ticker(ticker)
    info = stock.info
    return {
        "PE_ratio": info.get("trailingPE"),
        "EPS": info.get("trailingEps"),
        "Market_cap": info.get("marketCap"),
        "Dividend_yield": info.get("dividendYield"),
        "PEG_ratio": info.get("pegRatio")
    }

# === Report Generator ===
def generate_report():
    favorite_list = load_favorite_stocks()
    chat_history = load_chat_history()  # <-- Move this up, always define it
    headlines = get_recent_news()
    if not favorite_list:
        report = "⚠️ No favorite stocks found. Please add stocks to your favorite list."
        chat_history.append({"role": "assistant", "content": report})
        save_chat_history(chat_history)
        send_email("Daily Stock Report", report, RECIPIENT_EMAIL)
        return

    market_data = {}
    for ticker in favorite_list:
        try:
            technical = cached_get_stock_data(ticker)
            fundamental = cached_get_fundamentals(ticker)
            market_data[ticker] = {**technical, **fundamental}
        except Exception as e:
            market_data[ticker] = {"error": str(e)}

    prompt = f"""

You are a personal stock advisor bot. The user's favorite stock list is:
{favorite_list}

The market data is:
{market_data}

Based on these current internet search headlines:
{headlines}


跟我說大盤趨勢跟買或賣的建議。請根據以下格式回答：
大盤趨勢：<大盤趨勢>
買或賣建議：根據favorite stocks的市場數據，分別給出每支股票的買或賣建議，並且在最後給出建倉分配的比例。同時著重分析technical指標。
格式如下：
大盤趨勢：<大盤趨勢>
股票代碼：<買或賣建議> <建倉比例分配>
資金: <剩餘資金比例分配>
總資金加碼或減碼的建議(投入美股的總資金)：<加碼或減碼建議>
推薦新的股票：<推薦的股票代碼> : <推薦理由>

"""
    chat_history.append({"role": "user", "content": prompt})
    chat_history = chat_history[-MAX_HISTORY:]

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "You are a helpful financial advisor."}
        ] + chat_history
    )

    report = response.choices[0].message.content
    chat_history.append({"role": "assistant", "content": report})
    save_chat_history(chat_history)

    send_email("Daily Stock Report", report, RECIPIENT_EMAIL)

# === Send email with Postmark ===
def send_email(subject, body, to_email):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Postmark-Server-Token": POSTMARK_API_KEY
    }
    data = {
        "From": POSTMARK_SENDER_EMAIL,
        "To": to_email,
        "Subject": subject,
        "TextBody": body,
        "MessageStream": "outbound"
    }
    response = requests.post("https://api.postmarkapp.com/email", headers=headers, json=data)
    if response.status_code == 200:
        print("✅ Email sent successfully.")
    else:
        print(f"❌ Failed to send email: {response.status_code} - {response.text}")

def init_scheduler():
    scheduler = BackgroundScheduler(timezone='Asia/Taipei')
    scheduler.add_job(generate_report, 'cron', hour='21,12')
    scheduler.start()

# === Flask routes ===
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Favorite Stocks</title></head>
<body>
    <h2>Favorite Stocks</h2>
    <form method="POST">
        <textarea name="stocks" rows="10" cols="30">{{ stocks_text }}</textarea><br>
        <button type="submit">Update</button>
    </form>
</body>
</html>
"""

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        stocks_text = request.form.get('stocks', '')
        stocks = [s.strip().upper() for s in stocks_text.splitlines() if s.strip()]
        save_favorite_stocks(stocks)
    current_stocks = load_favorite_stocks()
    return render_template_string(HTML_TEMPLATE, stocks_text="\n".join(current_stocks))

# 修改启动方式
if __name__ == '__main__':
    generate_report()  # 立即生成一次报告
    init_scheduler()  # 替代原来的threading方案
    app.run(host='0.0.0.0', port=6837)
