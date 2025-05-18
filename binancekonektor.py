# ── IMPORTS ─────────────────────────────────────────────────────
import re
import time
import logging
import json
import os
import math
import asyncio
import aiohttp
import websockets
from binance.client import Client
from binance.streams import BinanceSocketManager
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from openai import OpenAI
from telethon import TelegramClient

# ── CONFIG & ENV LOADING ────────────────────────────────────────
env_path = "apy.env"
if not os.path.exists(env_path):
    raise FileNotFoundError(f"❌ File {env_path} not found!")
load_dotenv(dotenv_path=env_path)

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
WEBSOCKET_PORT = int(os.getenv("WEBSOCKET_PORT", "8765"))
if not API_KEY or not API_SECRET:
    raise ValueError("❌ API keys missing! Check your apy.env file.")

# ── CLIENT INITIALIZATION ───────────────────────────────────────
try:
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    if not os.getenv("OPENAI_API_KEY"):
        logging.warning("⚠️ OpenAI API key not found, ChatGPT parsing will be disabled")

    binance_client = Client(api_key=API_KEY, api_secret=API_SECRET)
    binance_client.FUTURES_BASE_URL = 'https://fapi.binance.com'
    
    # Test API connection
    binance_client.futures_ping()
    print("✅ Binance client initialized and connected.")
except Exception as e:
    logging.error(f"❌ Failed to initialize Binance client: {e}")
    raise

# ── TELEGRAM BOT CONFIGURATION ─────────────────────────────────
try:
    TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
    TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL")
    if not all([TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL]):
        logging.warning("⚠️ Missing Telegram configuration, notifications will be disabled")
    else:
        client = TelegramClient('bot', int(TELEGRAM_API_ID), TELEGRAM_API_HASH).start(bot_token=TELEGRAM_BOT_TOKEN)
        print("✅ Telegram client initialized.")
except Exception as e:
    logging.error(f"❌ Failed to initialize Telegram client: {e}")
    client = None

TELEGRAM_BOT_WEBHOOK_URL = "http://localhost:5001/webhook"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

IGNORED_RATE = 0
MIN_DELAY = 60
MAX_DELAY = 120
ALLOWED_KEYWORDS = ["VIP Trade ID", "Pair:", "Direction:", "Position Size:", "Leverage", "Trade Type:", "ENTRY", "Target", "STOP LOSS"]
SIGNALS_FILE = "signals.json"

# ── BALANCE & SIGNAL STORAGE ─────────────────────────────────────
def print_balance():
    try:
        balances = binance_client.futures_account_balance()
        if not balances:
            print("No balance information found.")
            return
        header = f"{'Asset':<8} {'Balance':>15}"
        separator = "-" * len(header)
        print("\n----- Futures Account Balance -----")
        print(header)
        print(separator)
        for item in balances:
            asset = item.get('asset', 'N/A')
            balance = item.get('balance', '0.0')
            print(f"{asset:<8} {balance:>15}")
        print(separator)
        print("---------------------------\n")
    except Exception as e:
        print("❌ Error fetching balance:", e)

def save_signal(signal):
    try:
        if not os.path.exists(SIGNALS_FILE):
            with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
                json.dump({"signals": []}, f)
        with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if any(existing.get("signal_id") == signal.get("signal_id") for existing in data.get("signals", [])):
            return
        data["signals"].append(signal)
        with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving signal: {e}")

# ── UTILITIES ───────────────────────────────────────────────────
def typewriter_effect(text, total_duration=3):
    if len(text) == 0:
        return
    delay = total_duration / len(text)
    for char in text:
        print(char, end='', flush=True)
        time.sleep(delay)
    print()

def greet():
    greeting = ("Wake up, Neo... The Matrix has you... Follow the white rabbit. "
                "Knock, knock, Neo. Bot developed by Jiri Gazo.")
    print("\033[32m", end='')
    typewriter_effect(greeting, total_duration=3)
    print("\033[0m", end='')

# ── PARSING WITH CHATGPT ────────────────────────────────────────
def parse_with_chatgpt(message):
    prompt = f"""
Parse the following trading signal and return a JSON object with the keys:
- "signal_id": string,
- "symbol": string (e.g., "BTCUSDT"),
- "direction": string ("BUY" or "SELL"),
- "entry": number or list of numbers,
- "stop_loss": number,
- "target": number or list of numbers,
- "position_size": string,
- "leverage": string,
- "trade_type": string.
If any value is missing, return null for that key.
Signal: {message}
Return only valid JSON, without any extra commentary.
    """
    try:
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are an expert trading signal parser. Return only JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=300
        )
        content = response.choices[0].message.content.strip()
        return json.loads(content)
    except Exception as e:
        print("❌ ChatGPT fallback parser error:", e)
        return None

# ── PRICE & QUANTITY HELPERS ────────────────────────────────────
def get_tick_size(symbol, exchange_info):
    for s in exchange_info.get("symbols", []):
        if s.get("symbol") == symbol:
            for f in s.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    return float(f.get("tickSize"))
    return None

def round_price(price, tick_size):
    if tick_size is None or tick_size == 0:
        return price
    precision = abs(int(round(math.log10(tick_size))))
    return round(price, precision)

def round_quantity(qty, step_size):
    if step_size is None or step_size == 0:
        return qty
    precision = abs(int(round(math.log10(step_size))))
    return round(qty, precision)

def calculate_atr(symbol, interval='1h', period=14, limit=100):
    try:
        klines = binance_client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        import pandas as pd
        df = pd.DataFrame(klines, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'num_trades',
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
        ])
        df['high'] = pd.to_numeric(df['high'])
        df['low'] = pd.to_numeric(df['low'])
        df['close'] = pd.to_numeric(df['close'])
        df['previous_close'] = df['close'].shift(1)
        df['tr'] = df.apply(lambda row: max(
            row['high'] - row['low'],
            abs(row['high'] - row['previous_close']) if pd.notna(row['previous_close']) else 0,
            abs(row['low'] - row['previous_close']) if pd.notna(row['previous_close']) else 0
        ), axis=1)
        atr = df['tr'].rolling(window=period).mean().iloc[-1]
        return atr
    except Exception as e:
        print(f"❌ ATR calculation error for {symbol}: {e}")
        return 100

def get_current_price(symbol):
    try:
        data = binance_client.futures_ticker_price(symbol=symbol)
        price = float(data.get("price"))
        return price
    except Exception as e:
        print(f"❌ Error fetching current price for {symbol}: {e}")
        return None

# ── SIGNAL FORMATTING & PROCESSING ──────────────────────────────
def reformat_signal(raw_text):
    lines = raw_text.splitlines()
    clean_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if any(keyword in line for keyword in ALLOWED_KEYWORDS):
            line = re.sub(r'^[^\w]+', '', line)
            if "Pair:" in line:
                line = re.sub(r'\s*\(.*?\)', '', line)
            if "Direction:" in line:
                line = "Direction: " + re.sub(r'[^A-Za-z\s]', '', line.split(":", 1)[1]).strip()
            line = re.sub(r'\s*-\s*', '-', line)
            clean_lines.append(line)
    return "\n".join(clean_lines)

def process_signal_text(message_text):
    if isinstance(message_text, dict):
        return {"data": message_text, "formatted": json.dumps(message_text, indent=4)}
    if not isinstance(message_text, str):
        message_text = str(message_text)
    message_text = reformat_signal(message_text)
    required_keywords = ["Pair:", "ENTRY", "STOP LOSS"]
    if not any(keyword in message_text for keyword in required_keywords):
        logging.info("Signal does not contain required keywords. Falling back to ChatGPT parser.")
        fallback_result = parse_with_chatgpt(message_text)
        if fallback_result:
            return {"data": fallback_result, "formatted": json.dumps(fallback_result, indent=4)}
        else:
            return None

    # extract fields...
    # (existing parsing logic unchanged)

# ── TRADE EXECUTION & NOTIFICATIONS ────────────────────────────
def send_notification(event, data):
    payload = {"event": event}
    payload.update(data)
    try:
        r = __import__('requests').post(TELEGRAM_BOT_WEBHOOK_URL, json=payload)
        if r.status_code == 200:
            print(f"✅ Notification '{event}' sent.")
        else:
            print(f"❌ Notification '{event}' error: {r.status_code}")
    except Exception as e:
        print(f"❌ Exception sending notification '{event}': {e}")

def open_trade(signal):
    # (execution logic unchanged)

# ── TRADE MANAGER CLASS ─────────────────────────────────────────
class TradeManager:
    # (monitoring, partial_close, close_trade logic unchanged)

# ── ASYNC HANDLERS ─────────────────────────────────────────────
async def handle_order_error(data):
    symbol = data.get("symbol", "UNKNOWN")
    error_message = data.get("message", "")
    message = f"🚨 <b>Order Error for ${symbol}</b>\nZPRÁVA_Z_POLE `{error_message}`"
    await client.send_message(TELEGRAM_CHANNEL, message)

# ── WEBSOCKET HANDLERS ────────────────────────────────────────
class WebSocketManager:
    def __init__(self):
        self.connected_clients = set()
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.reconnect_delay = 5  # seconds
        
    async def register(self, websocket):
        self.connected_clients.add(websocket)
        try:
            await websocket.send(json.dumps({
                "type": "connection_established",
                "message": "Connected to Binance Connector"
            }))
            self.reconnect_attempts = 0  # Reset counter on successful connection
        except Exception as e:
            logging.error(f"Error sending welcome message: {e}")
            
    async def unregister(self, websocket):
        self.connected_clients.remove(websocket)
        
    async def broadcast_message(self, message):
        disconnected = set()
        for client in self.connected_clients:
            try:
                await client.send(json.dumps(message))
            except websockets.exceptions.ConnectionClosed:
                disconnected.add(client)
            except Exception as e:
                logging.error(f"Error broadcasting message: {e}")
                disconnected.add(client)
        
        # Cleanup disconnected clients
        for client in disconnected:
            await self.unregister(client)
            
    async def handle_connection(self, websocket, path):
        await self.register(websocket)
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self.process_message(websocket, data)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": "Invalid JSON format"
                    }))
                except Exception as e:
                    logging.error(f"Error processing message: {e}")
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": str(e)
                    }))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self.unregister(websocket)
            
    async def process_message(self, websocket, data):
        event_type = data.get("event", "").strip().lower()
        
        if event_type == "open_trade":
            result = open_trade(data)
            await websocket.send(json.dumps({
                "type": "trade_response",
                "status": "success",
                "data": result
            }))
        elif event_type == "order_error":
            await handle_order_error(data)
            await websocket.send(json.dumps({
                "type": "error_handled",
                "status": "success"
            }))
        else:
            await websocket.send(json.dumps({
                "type": "error",
                "message": "Unknown event type"
            }))

# ── BINANCE WEBSOCKET HANDLERS ─────────────────────────────────
class BinanceWebsocketClient:
    def __init__(self, client):
        self.client = client
        self.bm = None
        self.ws_client = None
        
    async def connect(self):
        self.bm = BinanceSocketManager(self.client)
        self.ws_client = await self.bm.futures_user_socket()
        
    async def start(self):
        if not self.ws_client:
            await self.connect()
        while True:
            try:
                msg = await self.ws_client.recv()
                await handle_binance_user_socket(msg)
            except Exception as e:
                logging.error(f"Binance WebSocket error: {e}")
                await asyncio.sleep(5)
                await self.connect()

# ── MAIN SERVER LOOP ───────────────────────────────────────────
async def main():
    ws_manager = WebSocketManager()
    binance_ws = BinanceWebsocketClient(binance_client)
    
    while True:
        try:
            async with websockets.serve(ws_manager.handle_connection, "0.0.0.0", WEBSOCKET_PORT):
                print(f"✅ Websocket server started on port {WEBSOCKET_PORT}")
                
                # Start Binance websocket
                binance_task = asyncio.create_task(binance_ws.start())
                
                # Keep the server running
                await asyncio.Future()  # run forever
        except Exception as e:
            logging.error(f"Server error: {e}")
            ws_manager.reconnect_attempts += 1
            
            if ws_manager.reconnect_attempts >= ws_manager.max_reconnect_attempts:
                logging.error("Maximum reconnection attempts reached. Exiting.")
                break
                
            wait_time = ws_manager.reconnect_delay * ws_manager.reconnect_attempts
            logging.info(f"Attempting to reconnect in {wait_time} seconds...")
            await asyncio.sleep(wait_time)

# ── MAIN ────────────────────────────────────────────────────────
if __name__ == "__main__":
    print_balance()
    # Run the async server
    asyncio.run(main())
