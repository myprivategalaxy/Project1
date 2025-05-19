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
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient
from binance.error import ClientError, ServerError
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from openai import OpenAI
from telethon import TelegramClient
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional

# ── LOGGING SETUP ───────────────────────────────────────────────
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            RotatingFileHandler(
                'trading_bot.log',
                maxBytes=1024*1024,  # 1MB
                backupCount=5
            ),
            logging.StreamHandler()
        ]
    )

setup_logging()

# ── RATE LIMITER ────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, max_requests: int, time_window: int):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = defaultdict(list)
    
    def check_rate_limit(self, endpoint: str) -> bool:
        current_time = time.time()
        
        # Odstranění starých požadavků
        self.requests[endpoint] = [t for t in self.requests[endpoint] 
                                 if current_time - t < self.time_window]
        
        if len(self.requests[endpoint]) >= self.max_requests:
            logging.warning(f"Rate limit reached for endpoint: {endpoint}")
            return False
        
        self.requests[endpoint].append(current_time)
        logging.debug(f"Request added for endpoint: {endpoint}, current count: {len(self.requests[endpoint])}")
        return True
    
    async def wait_if_needed(self, endpoint: str):
        while not self.check_rate_limit(endpoint):
            logging.info(f"Waiting for rate limit on endpoint: {endpoint}")
            await asyncio.sleep(0.1)

# Vytvoření rate limiterů pro různé endpointy
order_limiter = RateLimiter(max_requests=50, time_window=60)  # 50 požadavků za minutu
market_limiter = RateLimiter(max_requests=1200, time_window=60)  # 1200 požadavků za minutu
account_limiter = RateLimiter(max_requests=10, time_window=60)  # 10 požadavků za minutu

# ── CONFIG & ENV LOADING ────────────────────────────────────────
env_path = "apy.env"
if not os.path.exists(env_path):
    raise FileNotFoundError(f"❌ File {env_path} not found!")
load_dotenv(dotenv_path=env_path)

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
WEBSOCKET_PORT = int(os.getenv("WEBSOCKET_PORT", "8765"))
WS_AUTH_TOKENS = set(os.getenv("WS_AUTH_TOKENS", "").split(","))

if not API_KEY or not API_SECRET:
    raise ValueError("❌ API keys missing! Check your apy.env file.")

# ── CLIENT INITIALIZATION ───────────────────────────────────────
try:
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    if not os.getenv("OPENAI_API_KEY"):
        logging.warning("⚠️ OpenAI API key not found, ChatGPT parsing will be disabled")

    # Initialize Binance client (vždy produkční)
    logging.info("Initializing Binance client...")
    binance_client = UMFutures(
        key=API_KEY,
        secret=API_SECRET
    )

    # Test API connection
    try:
        logging.info("Testing Binance API connection...")
        binance_client.ping()
        account_info = binance_client.account()
        logging.info(f"✅ Binance client initialized and connected. Account type: {account_info.get('accountType', 'Unknown')}")
    except ClientError as e:
        logging.error(f"❌ Binance API Error: {e.status_code} - {e.error_message}")
        raise
    except ServerError as e:
        logging.error(f"❌ Binance Request Error: {e.status_code} - {e.error_message}")
        raise
    except Exception as e:
        logging.error(f"❌ Unexpected error during Binance connection test: {e}")
        raise

except Exception as e:
    logging.error(f"❌ Failed to initialize Binance client: {e}")
    raise

# ── RATE LIMITING PROTECTION ─────────────────────────────────────
last_request_time = {}
MIN_REQUEST_INTERVAL = 0.1  # 100ms minimum between requests

def rate_limit_request(endpoint: str):
    current_time = time.time()
    if endpoint in last_request_time:
        time_since_last = current_time - last_request_time[endpoint]
        if time_since_last < MIN_REQUEST_INTERVAL:
            logging.debug(f"Rate limiting request for endpoint: {endpoint}, waiting {MIN_REQUEST_INTERVAL - time_since_last} seconds")
            time.sleep(MIN_REQUEST_INTERVAL - time_since_last)
    last_request_time[endpoint] = time.time()

# ── TELEGRAM BOT CONFIGURATION ─────────────────────────────────
try:
    TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
    TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL")
    if not all([TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL]):
        logging.warning("⚠️ Missing Telegram configuration, notifications will be disabled")
    else:
        logging.info("Initializing Telegram client...")
        client = TelegramClient('bot', int(TELEGRAM_API_ID), TELEGRAM_API_HASH).start(bot_token=TELEGRAM_BOT_TOKEN)
        logging.info("✅ Telegram client initialized.")
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
        logging.info("Fetching account balance...")
        balances = binance_client.balance()
        if not balances:
            logging.warning("No balance information found.")
            return
        header = f"{'Asset':<8} {'Balance':>15}"
        separator = "-" * len(header)
        logging.info("\n----- Futures Account Balance -----")
        logging.info(header)
        logging.info(separator)
        for item in balances:
            asset = item.get('asset', 'N/A')
            balance = item.get('balance', '0.0')
            logging.info(f"{asset:<8} {balance:>15}")
        logging.info(separator)
        logging.info("---------------------------\n")
    except Exception as e:
        logging.error(f"❌ Error fetching balance: {e}")

def save_signal(signal):
    try:
        logging.info(f"Saving signal: {signal}")
        if not os.path.exists(SIGNALS_FILE):
            with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
                json.dump({"signals": []}, f)
        with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if any(existing.get("signal_id") == signal.get("signal_id") for existing in data.get("signals", [])):
            logging.info("Signal already exists, skipping.")
            return
        data["signals"].append(signal)
        with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        logging.info("Signal saved successfully.")
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
        logging.info("Parsing signal with ChatGPT...")
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
        logging.info(f"ChatGPT response: {content}")
        return json.loads(content)
    except Exception as e:
        logging.error(f"❌ ChatGPT fallback parser error: {e}")
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
        logging.info(f"Calculating ATR for {symbol}...")
        klines = binance_client.klines(symbol=symbol, interval=interval, limit=limit)
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
        logging.info(f"ATR calculated for {symbol}: {atr}")
        return atr
    except Exception as e:
        logging.error(f"❌ ATR calculation error for {symbol}: {e}")
        return 100

def get_current_price(symbol):
    try:
        logging.info(f"Fetching current price for {symbol}...")
        data = binance_client.ticker_price(symbol=symbol)
        price = float(data.get("price"))
        logging.info(f"Current price for {symbol}: {price}")
        return price
    except Exception as e:
        logging.error(f"❌ Error fetching current price for {symbol}: {e}")
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
        logging.info(f"Sending notification for event: {event}")
        r = __import__('requests').post(TELEGRAM_BOT_WEBHOOK_URL, json=payload)
        if r.status_code == 200:
            logging.info(f"✅ Notification '{event}' sent.")
        else:
            logging.error(f"❌ Notification '{event}' error: {r.status_code}")
    except Exception as e:
        logging.error(f"❌ Exception sending notification '{event}': {e}")

def open_trade(signal):
    try:
        # Validate signal data
        required_fields = ["symbol", "direction", "entry", "stop_loss", "position_size", "leverage"]
        if not all(field in signal for field in required_fields):
            raise ValueError("Missing required fields in signal")

        # Get symbol information
        exchange_info = binance_client.exchange_info()
        tick_size = get_tick_size(signal["symbol"], exchange_info)
        
        # Set leverage
        binance_client.change_leverage(
            symbol=signal["symbol"],
            leverage=int(signal["leverage"])
        )

        # Calculate position size
        current_price = get_current_price(signal["symbol"])
        if not current_price:
            raise ValueError("Could not get current price")

        # Round values according to exchange rules
        entry_price = round_price(float(signal["entry"]), tick_size)
        stop_loss = round_price(float(signal["stop_loss"]), tick_size)
        
        # Place the orders
        side = "BUY" if signal["direction"].upper() == "BUY" else "SELL"
        position = binance_client.new_order(
            symbol=signal["symbol"],
            side=side,
            type="LIMIT",
            timeInForce="GTC",
            quantity=signal["position_size"],
            price=entry_price
        )

        # Place stop loss
        sl_order = binance_client.new_order(
            symbol=signal["symbol"],
            side="SELL" if side == "BUY" else "BUY",
            type="STOP_MARKET",
            stopPrice=stop_loss,
            closePosition=True
        )

        return {
            "status": "success",
            "position": position,
            "stop_loss": sl_order
        }

    except Exception as e:
        logging.error(f"Error opening trade: {e}")
        return {
            "status": "error",
            "message": str(e)
        }

# ── TRADE MANAGER CLASS ─────────────────────────────────────────
class TradeManager:
    def __init__(self, client):
        self.client = client
        self.active_trades = {}
        self.trade_history = []
        
    async def monitor_position(self, symbol, entry_price, targets, stop_loss):
        """Monitors an open position for take profit or stop loss hits"""
        while True:
            try:
                current_price = get_current_price(symbol)
                if not current_price:
                    await asyncio.sleep(1)
                    continue
                
                position = self.active_trades.get(symbol)
                if not position:
                    break
                
                # Check if stop loss hit
                if (position["side"] == "BUY" and current_price <= stop_loss) or \
                   (position["side"] == "SELL" and current_price >= stop_loss):
                    await self.close_trade(symbol, "stop_loss")
                    break
                
                # Check if any target hit
                for i, target in enumerate(targets):
                    if (position["side"] == "BUY" and current_price >= target) or \
                       (position["side"] == "SELL" and current_price <= target):
                        await self.partial_close(symbol, target, i + 1)
                        targets = targets[i+1:]  # Remove hit target
                        break
                
                await asyncio.sleep(1)
                
            except Exception as e:
                logging.error(f"Error monitoring position {symbol}: {e}")
                await asyncio.sleep(5)
    
    async def partial_close(self, symbol, price, target_number):
        """Closes part of the position when a target is hit"""
        try:
            position = self.active_trades.get(symbol)
            if not position:
                return
            
            # Calculate close amount (usually 25-33% of remaining position)
            close_amount = position["quantity"] * 0.33
            
            # Place take profit order
            order = self.client.new_order(
                symbol=symbol,
                side="SELL" if position["side"] == "BUY" else "BUY",
                type="LIMIT",
                timeInForce="GTC",
                quantity=close_amount,
                price=price
            )
            
            # Update position info
            position["quantity"] -= close_amount
            position["partial_closes"].append({
                "target": target_number,
                "price": price,
                "amount": close_amount,
                "order": order
            })
            
            await send_notification("partial_close", {
                "symbol": symbol,
                "target": target_number,
                "price": price
            })
            
        except Exception as e:
            logging.error(f"Error in partial close for {symbol}: {e}")
    
    async def close_trade(self, symbol, reason="manual"):
        """Closes entire position and cancels all related orders"""
        try:
            position = self.active_trades.get(symbol)
            if not position:
                return
            
            # Cancel all open orders
            self.client.futures_cancel_all_open_orders(symbol=symbol)
            
            # Close position at market
            self.client.futures_create_order(
                symbol=symbol,
                side="SELL" if position["side"] == "BUY" else "BUY",
                type="MARKET",
                quantity=position["quantity"]
            )
            
            # Record trade history
            self.trade_history.append({
                **position,
                "close_reason": reason,
                "close_time": time.time()
            })
            
            # Remove from active trades
            del self.active_trades[symbol]
            
            await send_notification("trade_closed", {
                "symbol": symbol,
                "reason": reason
            })
            
        except Exception as e:
            logging.error(f"Error closing trade for {symbol}: {e}")

# ── ASYNC HANDLERS ─────────────────────────────────────────────
async def handle_order_error(data):
    try:
        symbol = data.get("symbol", "UNKNOWN")
        error_message = data.get("message", "")
        error_code = data.get("code", "UNKNOWN")
        
        message = (
            f"🚨 <b>Order Error for {symbol}</b>\n"
            f"Code: `{error_code}`\n"
            f"Message: `{error_message}`"
        )
        
        if client:
            await client.send_message(TELEGRAM_CHANNEL, message, parse_mode='HTML')
        logging.error(f"Order error: {message}")
        
    except Exception as e:
        logging.error(f"Error handling order error notification: {e}")

# ── WEBSOCKET MANAGER ───────────────────────────────────────────
class WebSocketRateLimiter:
    def __init__(self, max_connections: int = 5, time_window: int = 60):
        self.max_connections = max_connections
        self.time_window = time_window
        self.connections: Dict[str, List[float]] = defaultdict(list)
        self.requests: Dict[str, List[float]] = defaultdict(list)
        self.max_requests = 100
        self.blocked_ips: Dict[str, float] = {}
        self.block_duration = 3600  # fixní blokace na 1 hodinu

    def is_ip_blocked(self, ip: str) -> bool:
        if ip in self.blocked_ips:
            if time.time() - self.blocked_ips[ip] < self.block_duration:
                return True
            del self.blocked_ips[ip]
        return False

    def can_connect(self, ip: str) -> bool:
        if self.is_ip_blocked(ip):
            return False

        current_time = time.time()
        self.connections[ip] = [t for t in self.connections[ip] if current_time - t < self.time_window]
        
        if len(self.connections[ip]) >= self.max_connections:
            logging.warning(f"❌ Too many connections from IP {ip}")
            self.blocked_ips[ip] = current_time
            return False
        
        self.connections[ip].append(current_time)
        return True

    def can_make_request(self, ip: str) -> bool:
        if self.is_ip_blocked(ip):
            return False

        current_time = time.time()
        self.requests[ip] = [t for t in self.requests[ip] if current_time - t < self.time_window]
        
        if len(self.requests[ip]) >= self.max_requests:
            logging.warning(f"❌ Too many requests from IP {ip}")
            self.blocked_ips[ip] = current_time
            return False
        
        self.requests[ip].append(current_time)
        return True

class BinanceWebsocketServer:
    def __init__(self):
        self.rate_limiter = WebSocketRateLimiter()
        self.clients = set()
        self.binance_client = None
        self.initialize_binance_client()
        self.last_notification_time = 0
        self.notification_cooldown = 60  # 60 sekund mezi notifikacemi
        self.positions = {}  # Slovník pro sledování pozic
        self.position_check_interval = 60  # Kontrola pozic každou minutu
        self.last_position_check = 0

    def initialize_binance_client(self):
        try:
            # Inicializace Binance klienta
            self.binance_client = UMFutures(
                key=os.getenv('BINANCE_API_KEY'),
                secret=os.getenv('BINANCE_API_SECRET')
            )
            
            # Test API připojení
            self.binance_client.ping()
            logging.info("✅ Binance client initialized successfully")
            
        except Exception as e:
            logging.error(f"❌ Failed to initialize Binance client: {str(e)}")
            raise

    async def handle_connection(self, websocket, path):
        client_ip = websocket.remote_address[0]
        
        # Kontrola rate limitu
        if not self.rate_limiter.can_connect(client_ip):
            logging.warning(f"❌ Connection rejected from IP {client_ip} - rate limit exceeded")
            await websocket.close(1008, "Rate limit exceeded")
            return

        self.clients.add(websocket)
        logging.info(f"✅ New client connected from {client_ip}")
        
        try:
            async for message in websocket:
                # Kontrola rate limitu pro požadavky
                if not self.rate_limiter.can_make_request(client_ip):
                    logging.warning(f"❌ Request rejected from IP {client_ip} - rate limit exceeded")
                    await websocket.send(json.dumps({
                        "status": "error",
                        "message": "Rate limit exceeded. Please try again later."
                    }))
                    continue

                try:
                    data = json.loads(message)
                    logging.info(f"📥 Received message from {client_ip}: {data}")
                    
                    if data.get('type') == 'signal':
                        response = await self.process_signal(data)
                        await websocket.send(json.dumps(response))
                    else:
                        await websocket.send(json.dumps({
                            "status": "error",
                            "message": "Invalid message type"
                        }))
                except json.JSONDecodeError:
                    logging.error(f"❌ Invalid JSON from {client_ip}")
                    await websocket.send(json.dumps({
                        "status": "error",
                        "message": "Invalid JSON format"
                    }))
                except Exception as e:
                    logging.error(f"❌ Error processing message from {client_ip}: {str(e)}")
                    await websocket.send(json.dumps({
                        "status": "error",
                        "message": f"Error processing message: {str(e)}"
                    }))
        except websockets.exceptions.ConnectionClosed:
            logging.info(f"✅ Client disconnected: {client_ip}")
        finally:
            self.clients.remove(websocket)

# ── TRADE EXECUTION ─────────────────────────────────────────────
async def execute_trade(signal: dict):
    if not validate_trade_params(signal):
        logging.error("❌ Invalid trade parameters")
        return False
    
    try:
        # Použití rate limiteru pro objednávky
        await order_limiter.wait_if_needed('place_order')
        
        # Získání aktuální ceny
        await market_limiter.wait_if_needed('get_price')
        current_price = get_current_price(signal['symbol'])
        
        # Kontrola, zda je cena v přijatelném rozsahu
        price_diff_percent = abs(current_price - signal['entry']) / signal['entry'] * 100
        if price_diff_percent > 1:  # 1% tolerance
            logging.error(f"❌ Price difference too large: {price_diff_percent}%")
            return False
        
        # Vytvoření objednávky
        order = binance_client.futures_create_order(
            symbol=signal['symbol'],
            side=signal['direction'],
            type='LIMIT',
            timeInForce='GTC',
            quantity=signal['position_size'],
            price=signal['entry']
        )
        
        logging.info(f"✅ Order placed successfully: {order}")
        return True
        
    except Exception as e:
        logging.error(f"❌ Error executing trade: {e}")
        return False

# ── MAIN SERVER LOOP ───────────────────────────────────────────
async def main():
    try:
        # Inicializace WebSocket klienta
        binance_ws = UMFuturesWebsocketClient()
        binance_ws.start()
        
        # Spuštění WebSocket serveru
        async with websockets.serve(ws_manager.handle_connection, "localhost", WEBSOCKET_PORT):
            logging.info(f"✅ WebSocket server běží na portu {WEBSOCKET_PORT}")
            await asyncio.Future()  # běží donekonečna
    except Exception as e:
        logging.error(f"❌ Chyba při spuštění: {e}")
        raise

# ── MAIN ────────────────────────────────────────────────────────
if __name__ == "__main__":
    print_balance()
    # Run the async server
    asyncio.run(main())
