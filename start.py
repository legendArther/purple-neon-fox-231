try:
    import gevent.monkey
    gevent.monkey.patch_all()
except ImportError:
    pass

from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit
import requests
import json
import time
import threading
import websockets
import asyncio
from datetime import datetime
import pytz
import psycopg2
from psycopg2.extras import RealDictCursor
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
# Use gevent for async mode to avoid eventlet/asyncio conflicts
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

GAMMA_BASE = "https://gamma-api.polymarket.com"
POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- DB SETUP ---
def get_db_connection():
    if not DATABASE_URL:
        # Fallback to sqlite if DATABASE_URL is not set (useful for local dev)
        import sqlite3
        return sqlite3.connect("market_history.db")
    
    # Force use of DATABASE_URL if it exists
    print(f"Connecting to NeonDB: {DATABASE_URL[:20]}...")
    return psycopg2.connect(DATABASE_URL)

def init_db():
    print("Initializing Database...")
    conn = get_db_connection()
    try:
        c = conn.cursor()
        if DATABASE_URL:
            # Explicitly create the table if it's missing in Neon
            c.execute('''CREATE TABLE IF NOT EXISTS price_history 
                         (timestamp BIGINT, slug TEXT, up_price REAL, down_price REAL)''')
            print("Table price_history verified/created in NeonDB.")
        else:
            c.execute('''CREATE TABLE IF NOT EXISTS price_history 
                         (timestamp INTEGER, slug TEXT, up_price REAL, down_price REAL)''')
            print("Table price_history verified/created in SQLite.")
        conn.commit()
    except Exception as e:
        print(f"Init DB Error: {e}")
    finally:
        conn.close()

def save_price(ts, slug, up, down):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO price_history (timestamp, slug, up_price, down_price) VALUES (%s, %s, %s, %s)" if DATABASE_URL else "INSERT INTO price_history VALUES (?, ?, ?, ?)", (ts, slug, up, down))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB Error: {e}")

def get_history(slug):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        if DATABASE_URL:
            c.execute("SELECT timestamp, up_price, down_price FROM price_history WHERE slug=%s ORDER BY timestamp ASC", (slug,))
        else:
            c.execute("SELECT timestamp, up_price, down_price FROM price_history WHERE slug=? ORDER BY timestamp ASC", (slug,))
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"History Fetch Error: {e}")
        return []

# --- GLOBAL STATE ---
current_state = {
    "title": "Loading...",
    "market_data": [],
    "slug": "---",
    "last_updated": "---",
    "token_to_outcome": {},
    "current_timestamp_bucket": 0,
    "expires_at": 0,
    "history": [] # Initial history for the chart
}

def get_current_5min_timestamp():
    now = int(time.time())
    return (now // 300) * 300

def format_id_time(ts):
    """Convert Unix TS to IST HH:MM AM/PM string."""
    utc_dt = datetime.fromtimestamp(ts, tz=pytz.UTC)
    ist_dt = utc_dt.astimezone(pytz.timezone('Asia/Kolkata'))
    return ist_dt.strftime('%I:%M %p')

def get_event_metadata(slug):
    try:
        resp = requests.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=5)
        if resp.status_code == 200 and resp.json():
            return resp.json()[0]
    except: pass
    return None

def resolve_active_market():
    ts = get_current_5min_timestamp()
    slug = f"btc-updown-5m-{ts}"
    event = get_event_metadata(slug)
    if not event:
        prev_slug = f"btc-updown-5m-{ts - 300}"
        event = get_event_metadata(prev_slug)
        if event: 
            slug = prev_slug
            ts = ts - 300
            
    if event:
        tokens = {}
        market_list = []
        # Format Title: "Bitcoin Up or Down - 12:20 PM-12:25 PM IST"
        start_str = format_id_time(ts)
        end_str = format_id_time(ts + 300)
        display_title = f"Bitcoin Price Tracker ({start_str} - {end_str} IST)"
        
        for m in event.get("markets", []):
            try:
                ids = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                outs = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
                for tid, o in zip(ids, outs):
                    tokens[tid] = o
                    market_list.append({"outcome": o, "buy": "---", "sell": "---", "token_id": tid})
            except: continue
        return display_title, market_list, slug, tokens, ts
    return "Market Not Found", [], slug, {}, ts

async def polymarket_ws_handler():
    global current_state
    init_db()
    
    while True:
        title, markets, slug, token_map, ts_bucket = resolve_active_market()
        
        # Load history for the client's initial load
        history_rows = get_history(slug)
        history_data = [{"x": r[0] - ts_bucket, "up": r[1], "down": r[2]} for r in history_rows]

        current_state.update({
            "title": title, "market_data": markets, "slug": slug,
            "token_to_outcome": token_map, "current_timestamp_bucket": ts_bucket,
            "last_updated": time.strftime('%H:%M:%S'),
            "expires_at": ts_bucket + 300,
            "history": history_data
        })
        socketio.emit('update_prices', current_state)
        
        if not token_map:
            await asyncio.sleep(10)
            continue

        token_ids = list(token_map.keys())
        last_save_time = 0
        
        try:
            async with websockets.connect(POLY_WS_URL) as ws:
                await ws.send(json.dumps({"type": "market", "assets_ids": token_ids}))
                while True:
                    if get_current_5min_timestamp() != ts_bucket: break
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        data = json.loads(message)
                        updated = False
                        if isinstance(data, list):
                            for item in data:
                                if item.get("event_type") == "book":
                                    tid = item.get("asset_id")
                                    asks = item.get("asks", [])
                                    best_ask = asks[0]["price"] if asks else "N/A"
                                    for m in current_state["market_data"]:
                                        if m["token_id"] == tid:
                                            m["buy"] = best_ask
                                            updated = True
                        elif isinstance(data, dict) and data.get("event_type") == "price_change":
                            for change in data.get("price_changes", []):
                                tid = change.get("asset_id")
                                for m in current_state["market_data"]:
                                    if m["token_id"] == tid:
                                        if "best_ask" in change: 
                                            m["buy"] = change["best_ask"]
                                            updated = True
                        
                        if updated:
                            now = int(time.time())
                            current_state["last_updated"] = time.strftime('%H:%M:%S')
                            socketio.emit('update_prices', current_state)
                            
                            # Save to DB once per second max per update
                            if now > last_save_time:
                                # USE m["buy"] if it's not "---", else use None
                                up_p = next((m["buy"] for m in current_state["market_data"] if "up" in m["outcome"].lower()), "---")
                                down_p = next((m["buy"] for m in current_state["market_data"] if "down" in m["outcome"].lower()), "---")

                                # Convert to float only if valid
                                try:
                                    up_f = float(up_p) if up_p != "---" else None
                                    down_f = float(down_p) if down_p != "---" else None
                                    
                                    if up_f is not None or down_f is not None:
                                        save_price(now, slug, up_f, down_f)
                                        last_save_time = now
                                except (ValueError, TypeError):
                                    pass

                    except asyncio.TimeoutError: continue
        except Exception as e:
            print(f"WS Error: {e}")
            await asyncio.sleep(5)

def start_background_workers():
    def run_worker():
        # Gevent-friendly way to run the background loop
        # We use a separate thread for the asyncio loop but ensure gevent is aware
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(polymarket_ws_handler())
        except Exception as e:
            print(f"Background Worker Error: {e}")

    # Use gevent.spawn instead of threading.Thread for better compatibility with Gunicorn gevent worker
    import gevent
    gevent.spawn(run_worker)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>BTC Live RT</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: sans-serif; background: #000; color: #fff; max-width: 600px; margin: 20px auto; padding: 20px; }
        .card { background: #111; padding: 20px; border-radius: 10px; border: 1px solid #333; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 20px; }
        .box { background: #222; padding: 15px; border-radius: 5px; border-left: 4px solid #44f; }
        .box.down { border-left-color: #f44; }
        .val { font-size: 24px; font-weight: bold; font-family: monospace; }
        .label { font-size: 10px; color: #666; }
        .chart-container { background: #111; padding: 20px; border-radius: 10px; border: 1px solid #333; height: 300px; flex: 2; }
        .flex-row { display: flex; gap: 20px; margin-top: 20px; align-items: stretch; }
        .sim-container { background: #111; padding: 20px; border-radius: 10px; border: 1px solid #333; flex: 1; display: flex; flex-direction: column; }
        .sim-title { font-size: 14px; font-weight: bold; color: #fff; margin-bottom: 15px; border-bottom: 1px solid #222; padding-bottom: 10px; display: flex; justify-content: space-between; }
        .sim-stat { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 13px; }
        .sim-stat .lbl { color: #666; }
        .sim-stat .val { font-family: monospace; font-weight: bold; }
        .sim-btn { border: none; padding: 10px; border-radius: 5px; cursor: pointer; font-weight: bold; color: #fff; margin-top: 5px; flex: 1; transition: opacity 0.2s; }
        .sim-btn:disabled { opacity: 0.3; cursor: not-allowed; }
        .btn-up { background: #44f; }
        .btn-down { background: #f44; }
        .btn-close { background: #333; margin-top: 10px; border: 1px solid #444; }
        .pos-card { background: #1a1a1a; padding: 10px; border-radius: 5px; margin-top: auto; border: 1px solid #333; font-size: 12px; }
        .pnl-plus { color: #4f4; }
        .pnl-minus { color: #f44; }
    </style>
</head>
<body>
    <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <h2 id="title" style="margin:0">Syncing...</h2>
            <div id="timer" style="font-size:24px; font-weight:bold; color:#ff9900; font-family:monospace;">00:00</div>
        </div>
        <div id="slug" style="font-size:12px;color:#444"></div>
        <div class="grid" id="grid"></div>
        <div style="margin-top:20px; font-size:10px; color:#333">
            Last: <span id="last">--</span> | Latency: <span id="ping">--</span>ms
        </div>
    </div>

    <div class="flex-row">
        <div class="chart-container">
            <canvas id="priceChart"></canvas>
        </div>

        <div class="sim-container">
            <div class="sim-title">
                TRADING SIMULATOR
                <span id="sim-balance" style="color:#4f4">$1,000.00</span>
            </div>
            
            <div style="display:flex; gap:10px;">
                <button class="sim-btn btn-up" id="btn-buy-up" onclick="placeTrade('up')">BUY UP</button>
                <button class="sim-btn btn-down" id="btn-buy-down" onclick="placeTrade('down')">BUY DOWN</button>
            </div>

            <div id="active-position" class="pos-card" style="display:none; margin-top:15px;">
                <div style="display:flex; justify-content:space-between; margin-bottom:5px;">
                    <span id="pos-type" style="font-weight:bold;">UP</span>
                    <span id="pos-pnl" class="pnl-plus">+$0.00</span>
                </div>
                <div class="sim-stat"><span class="lbl">Entry</span><span class="val" id="pos-entry">0.00¢</span></div>
                <div class="sim-stat"><span class="lbl">Current</span><span class="val" id="pos-current">0.00¢</span></div>
                <button class="sim-btn btn-close" onclick="closeTrade()">CLOSE POSITION</button>
            </div>

            <div style="margin-top:auto; padding-top:15px; border-top:1px solid #222; font-size:11px; color:#444;">
                <div class="sim-stat"><span class="lbl">Total Trades</span><span class="val" id="sim-trades">0</span></div>
                <div class="sim-stat"><span class="lbl">Profit/Loss</span><span class="val" id="sim-total-pnl">$0.00</span></div>
            </div>
        </div>
    </div>

    <script>
        const socket = io();
        let targetExpiry = 0;
        let startTime = 0;

        // Simulator State
        let balance = 1000.00;
        let totalPnl = 0.00;
        let tradeCount = 0;
        let currentPos = null; // { type: 'up'|'down', entry: float, shares: float }
        let currentPrices = { up: null, down: null };

        function updateSimUI() {
            document.getElementById('sim-balance').textContent = `$${balance.toFixed(2)}`;
            document.getElementById('sim-total-pnl').textContent = `$${totalPnl.toFixed(2)}`;
            document.getElementById('sim-total-pnl').className = 'val ' + (totalPnl >= 0 ? 'pnl-plus' : 'pnl-minus');
            document.getElementById('sim-trades').textContent = tradeCount;

            const posEl = document.getElementById('active-position');
            if (currentPos) {
                posEl.style.display = 'block';
                document.getElementById('pos-type').textContent = currentPos.type.toUpperCase();
                document.getElementById('pos-type').style.color = currentPos.type === 'up' ? '#44f' : '#f44';
                document.getElementById('pos-entry').textContent = (currentPos.entry * 100).toFixed(1) + '¢';
                
                const currentPrice = currentPrices[currentPos.type];
                if (currentPrice) {
                    document.getElementById('pos-current').textContent = (currentPrice * 100).toFixed(1) + '¢';
                    const pnl = (currentPrice - currentPos.entry) * currentPos.shares;
                    document.getElementById('pos-pnl').textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
                    document.getElementById('pos-pnl').className = pnl >= 0 ? 'pnl-plus' : 'pnl-minus';
                }
                
                document.getElementById('btn-buy-up').disabled = true;
                document.getElementById('btn-buy-down').disabled = true;
            } else {
                posEl.style.display = 'none';
                document.getElementById('btn-buy-up').disabled = !currentPrices.up;
                document.getElementById('btn-buy-down').disabled = !currentPrices.down;
            }
        }

        function placeTrade(type) {
            const price = currentPrices[type];
            if (!price || currentPos) return;
            
            const tradeAmount = 100.00; // Fixed trade size for sim
            if (balance < tradeAmount) return;

            const shares = tradeAmount / price;
            currentPos = { type, entry: price, shares, cost: tradeAmount };
            balance -= tradeAmount;
            updateSimUI();
        }

        function closeTrade() {
            if (!currentPos) return;
            const price = currentPrices[currentPos.type];
            const pnl = (price - currentPos.entry) * currentPos.shares;
            
            balance += (currentPos.cost + pnl);
            totalPnl += pnl;
            tradeCount++;
            currentPos = null;
            updateSimUI();
        }
        
        // Timer Loop
        setInterval(() => {
            if (targetExpiry > 0) {
                const now = Math.floor(Date.now() / 1000);
                const diff = targetExpiry - now;
                if (diff >= 0) {
                    const m = Math.floor(diff / 60);
                    const s = diff % 60;
                    document.getElementById('timer').textContent = `${m}:${s < 10 ? '0' : ''}${s}`;
                    // Flash red when < 30s
                    document.getElementById('timer').style.color = diff < 30 ? '#f00' : '#ff9900';
                }
            }
        }, 1000);

        // Chart Initialization
        const ctx = document.getElementById('priceChart').getContext('2d');
        const chart = new Chart(ctx, {
            type: 'line',
            data: {
                datasets: [
                    {
                        label: 'Up (Buy)',
                        borderColor: '#44f',
                        backgroundColor: 'rgba(68, 68, 255, 0.1)',
                        data: [],
                        borderWidth: 2,
                        pointRadius: 0,
                        tension: 0.1
                    },
                    {
                        label: 'Down (Buy)',
                        borderColor: '#f44',
                        backgroundColor: 'rgba(244, 68, 68, 0.1)',
                        data: [],
                        borderWidth: 2,
                        pointRadius: 0,
                        tension: 0.1
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: {
                        type: 'linear',
                        min: 0,
                        max: 300,
                        display: true,
                        ticks: { 
                            color: '#444',
                            callback: function(value) {
                                if (value === 0) return "Start";
                                if (value === 300) return "End";
                                if (value === 150) return "2:30";
                                return "";
                            }
                        },
                        grid: { color: '#222' }
                    },
                    y: {
                        min: 0,
                        max: 1,
                        ticks: {
                            color: '#666',
                            callback: function(value) { return (value * 100).toFixed(0) + '¢'; }
                        },
                        grid: { color: '#222' }
                    }
                },
                plugins: {
                    legend: { labels: { color: '#fff' } }
                },
                animation: false
            }
        });

        let currentSlug = '';

        socket.on('update_prices', (data) => {
            document.getElementById('title').textContent = data.title;
            document.getElementById('slug').textContent = data.slug;
            document.getElementById('last').textContent = data.last_updated;
            targetExpiry = data.expires_at;
            startTime = data.current_timestamp_bucket;
            
            // Extract prices for simulator
            const upItem = data.market_data.find(m => m.outcome.toLowerCase().includes('up'));
            const downItem = data.market_data.find(m => m.outcome.toLowerCase().includes('down'));
            currentPrices.up = upItem ? parseFloat(upItem.buy) : null;
            currentPrices.down = downItem ? parseFloat(downItem.buy) : null;
            updateSimUI();

            // Reset chart if slug changes (new 5m round)
            if (currentSlug !== data.slug) {
                currentSlug = data.slug;
                chart.data.datasets[0].data = [];
                chart.data.datasets[1].data = [];
                
                // Load history if available
                if (data.history && data.history.length > 0) {
                    data.history.forEach(h => {
                        if (h.up !== null) chart.data.datasets[0].data.push({x: h.x, y: h.up});
                        if (h.down !== null) chart.data.datasets[1].data.push({x: h.x, y: h.down});
                    });
                }
            }

            const now = Math.floor(Date.now() / 1000);
            const xVal = now - startTime;

            let html = '';
            data.market_data.forEach(m => {
                const buyVal = parseFloat(m.buy);
                const isDown = m.outcome.toLowerCase().includes('down');
                
                // Add to chart using time offset (0-300 seconds)
                if (!isNaN(buyVal) && xVal >= 0 && xVal <= 300) {
                    const dsIdx = isDown ? 1 : 0;
                    chart.data.datasets[dsIdx].data.push({x: xVal, y: buyVal});
                    
                    // Keep roughly 1000 points max per line for performance
                    if (chart.data.datasets[dsIdx].data.length > 1000) {
                        chart.data.datasets[dsIdx].data.shift();
                    }
                }

                html += `<div class="box ${isDown ? 'down' : ''}">
                    <div style="font-size:14px">${m.outcome}</div>
                    <div class="val">${m.buy}</div>
                    <div class="label">BUY (ASK)</div>
                </div>`;
            });
            
            chart.update('none');
            document.getElementById('grid').innerHTML = html;
        });
        
        // Initial UI sync
        updateSimUI();

        setInterval(() => {
            const s = Date.now();
            socket.emit('ping_c', () => document.getElementById('ping').textContent = Date.now() - s);
        }, 1000);
    </script>
</body>
</html>
"""

@app.route('/')
def home(): return render_template_string(HTML_TEMPLATE)

@socketio.on('ping_c')
def handle_ping(data=None):
    return "pong"

# Start background workers before running app
start_background_workers()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    socketio.run(app, debug=False, host='0.0.0.0', port=port)
