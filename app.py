from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import qrcode
from io import BytesIO
import base64
import random
import string
import requests
from datetime import datetime
import os
import time
import json
from web3 import Web3
import logging
import psycopg2
import urllib.parse
import sqlite3
import math

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', '3f64bbf93f1b1cbb0fec56734f7bf837')

# SIMPLIFIED Database connection
def get_db_connection():
    database_url = os.environ.get('DATABASE_URL')

    if database_url:
        try:
            # Handle both postgres:// and postgresql:// formats
            if database_url.startswith('postgres://'):
                database_url = database_url.replace('postgres://', 'postgresql://', 1)

            print("🔗 Connecting to PostgreSQL...")
            conn = psycopg2.connect(database_url, sslmode='require')
            print("✅ PostgreSQL connected successfully!")
            return conn
        except Exception as e:
            logger.error(f"PostgreSQL connection failed: {e}")
            print("❌ PostgreSQL failed, falling back to SQLite")
            return sqlite3.connect('users.db')
    else:
        print("ℹ️ Using SQLite for development")
        return sqlite3.connect('users.db')

# SIMPLIFIED Database helper function
def execute_query(query, params=None, fetch=False):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Convert PostgreSQL style %s to SQLite ? if needed
        if isinstance(conn, sqlite3.Connection) and '%s' in query:
            query = query.replace('%s', '?')
        
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
            
        if fetch:
            result = cursor.fetchall()
        else:
            result = None
            conn.commit()
            
        return result
        
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()
        conn.close()

# HTTPS Enforcement for production
@app.before_request
def enforce_https():
    if os.environ.get('RENDER'):
        if request.headers.get('X-Forwarded-Proto') == 'http':
            url = request.url.replace('http://', 'https://', 1)
            return redirect(url, code=301)

# Web3 Configuration
INFURA_URL = "https://mainnet.infura.io/v3/93789df842ec4f8d96bfc8f506523acc"
w3 = Web3(Web3.HTTPProvider(INFURA_URL))

# Bot Wallet Addresses
WALLETS = {
    'ethereum': '0xBBf79d7825f862B6192dbf3624714b33e4b6cfB3',
    'solana': 'Hkgm3fQ1p9PP15xNHApf9MUssJRse5Nh5jGYgSd6pBen',
    'bitcoin': 'bc1qv5qxecalw6qz46p4ddlw2hl4gmqt8yxdz3dzk8',
    'tron': 'TDd8UQiDvKoU4jSFCZ4u3x1oCBkcMsE2KN'
}

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', "7638550593:AAHsoXbK_w6EkxhLHnOfjsNcFFV5vtow-J8")
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', "7578614215")

# Auto-response messages for when admin is offline
AUTO_RESPONSES = [
    "Thank you for your message! Our support team will respond shortly.",
    "We've received your message and will get back to you within 24 hours.",
    "For faster assistance, please check our FAQ section or make sure you've completed your deposit.",
    "Our team is currently assisting other users. We'll respond to your query soon.",
    "If this is regarding a payment, please provide your transaction hash for verification."
]

# Database setup with proper schema
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Users table
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        referral_code TEXT UNIQUE,
        balance REAL DEFAULT 0,
        invested REAL DEFAULT 0,
        profits REAL DEFAULT 0,
        total_deposited REAL DEFAULT 0,
        support_fee_paid BOOLEAN DEFAULT FALSE,
        referred_by TEXT,
        unlock_code_used TEXT DEFAULT NULL,
        bot_unlocked BOOLEAN DEFAULT FALSE,
        web3_wallet TEXT DEFAULT NULL,
        active BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Trades table
    cursor.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        profit REAL,
        status TEXT DEFAULT 'completed',
        timestamp TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Deposits table
    cursor.execute('''CREATE TABLE IF NOT EXISTS deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        crypto_type TEXT,
        wallet_address TEXT,
        transaction_hash TEXT,
        timestamp TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Chat messages table
    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        message TEXT,
        is_admin BOOLEAN DEFAULT FALSE,
        is_auto_response BOOLEAN DEFAULT FALSE,
        timestamp TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Unlock codes table
    cursor.execute('''CREATE TABLE IF NOT EXISTS unlock_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        amount REAL,
        used BOOLEAN DEFAULT FALSE,
        used_by INTEGER,
        used_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Web3 transactions table
    cursor.execute('''CREATE TABLE IF NOT EXISTS web3_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        transaction_hash TEXT UNIQUE,
        amount REAL,
        crypto_type TEXT,
        status TEXT DEFAULT 'pending',
        timestamp TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Admin notifications table
    cursor.execute('''CREATE TABLE IF NOT EXISTS admin_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        message TEXT,
        type TEXT,
        read BOOLEAN DEFAULT FALSE,
        timestamp TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.commit()
    cursor.close()
    conn.close()
    print("✅ Database initialized successfully!")

# Generate referral code
def generate_referral_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

# Generate unlock code (Admin only)
def generate_unlock_code(amount=50):
    code = 'TRADE' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    execute_query('INSERT INTO unlock_codes (code, amount, used) VALUES (?, ?, ?)',
                 (code, amount, False))
    return code

# IMPROVED SMART TRADING ALGORITHM
def simulate_trade(user_id, amount):
    # Get user data
    user_data = execute_query("SELECT invested, profits, total_deposited FROM users WHERE id = ?", 
                             (user_id,), fetch=True)
    
    if user_data and user_data[0]:
        invested = user_data[0][0] if user_data[0][0] else amount
        total_profits = user_data[0][1] if user_data[0][1] else 0
        total_deposited = user_data[0][2] if user_data[0][2] else 0
    else:
        invested = amount
        total_profits = 0
        total_deposited = 0

    # SMART TIER-BASED TRADING ALGORITHM
    # Higher tiers get better success rates and profit potential
    
    if invested >= 5000:  # ELITE Tier - Premium profits
        success_rate = 0.82
        profit_multipliers = [(1.8, 3.0), (2.0, 3.5), (2.2, 4.0)]  # Multiple profit scenarios
        loss_multipliers = [(0.01, 0.03), (0.02, 0.04)]  # Very small losses
        volatility = 0.1  # Low volatility

    elif invested >= 2000:  # DIAMOND Tier - Excellent profits
        success_rate = 0.78
        profit_multipliers = [(1.6, 2.8), (1.8, 3.2), (2.0, 3.6)]
        loss_multipliers = [(0.02, 0.05), (0.03, 0.06)]
        volatility = 0.15

    elif invested >= 1000:  # VIP Tier - High profits
        success_rate = 0.75
        profit_multipliers = [(1.4, 2.5), (1.6, 2.8), (1.8, 3.0)]
        loss_multipliers = [(0.03, 0.07), (0.04, 0.08)]
        volatility = 0.2

    elif invested >= 500:  # Gold Tier - Good profits
        success_rate = 0.70
        profit_multipliers = [(1.3, 2.2), (1.4, 2.4), (1.5, 2.6)]
        loss_multipliers = [(0.04, 0.09), (0.05, 0.10)]
        volatility = 0.25

    elif invested >= 200:  # Silver Tier - Moderate profits
        success_rate = 0.65
        profit_multipliers = [(1.2, 1.8), (1.3, 2.0), (1.4, 2.2)]
        loss_multipliers = [(0.05, 0.12), (0.06, 0.14)]
        volatility = 0.3

    elif invested >= 100:  # Bronze Tier - Balanced profits
        success_rate = 0.60
        profit_multipliers = [(1.1, 1.6), (1.2, 1.7), (1.3, 1.8)]
        loss_multipliers = [(0.06, 0.15), (0.07, 0.18)]
        volatility = 0.35

    else:  # Starter Tier - Learning phase
        success_rate = 0.55
        profit_multipliers = [(1.05, 1.4), (1.1, 1.5), (1.15, 1.6)]
        loss_multipliers = [(0.08, 0.20), (0.10, 0.25)]
        volatility = 0.4

    # Add experience factor based on user's trading history
    if total_deposited > 0:
        win_ratio = max(0.3, min(0.9, (total_profits / total_deposited) + 0.5))
        experience_boost = math.log(max(total_deposited, 100)) / 10
        adjusted_success_rate = min(success_rate * win_ratio + experience_boost, 0.85)
    else:
        adjusted_success_rate = success_rate

    # Market conditions simulation
    market_trend = random.uniform(-volatility, volatility)
    adjusted_success_rate = max(0.4, min(0.9, adjusted_success_rate + market_trend))

    # SMART TRADE EXECUTION
    trade_quality = random.random()
    
    if trade_quality < adjusted_success_rate:
        # PROFITABLE TRADE - Multiple scenarios for better profits
        if trade_quality < adjusted_success_rate * 0.3:
            # Big win (30% of profitable trades)
            profit_range = profit_multipliers[2]
            profit_multiplier = random.uniform(profit_range[0], profit_range[1])
        elif trade_quality < adjusted_success_rate * 0.7:
            # Medium win (40% of profitable trades)
            profit_range = profit_multipliers[1]
            profit_multiplier = random.uniform(profit_range[0], profit_range[1])
        else:
            # Small win (30% of profitable trades)
            profit_range = profit_multipliers[0]
            profit_multiplier = random.uniform(profit_range[0], profit_range[1])
        
        # Add bonus for higher tiers
        tier_bonus = min(0.3, invested / 10000)
        profit_multiplier += tier_bonus
        
        profit = amount * (profit_multiplier - 1)
        status = 'profit'
        
        # Ensure minimum profit
        min_profit = amount * 0.05  # At least 5% profit
        profit = max(profit, min_profit)
        
    else:
        # LOSS TRADE - Controlled losses
        loss_severity = random.random()
        if loss_severity < 0.3:
            # Small loss (30% of losing trades)
            loss_range = loss_multipliers[0]
        else:
            # Medium loss (70% of losing trades)
            loss_range = loss_multipliers[1]
        
        loss_multiplier = random.uniform(loss_range[0], loss_range[1])
        profit = -amount * loss_multiplier
        status = 'loss'
        
        # Cap maximum loss
        max_loss = amount * 0.3  # Maximum 30% loss
        profit = max(profit, -max_loss)

    profit = round(profit, 2)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Insert trade and update user
    execute_query("INSERT INTO trades (user_id, amount, profit, status, timestamp) VALUES (?, ?, ?, ?, ?)",
                  (user_id, amount, profit, status, timestamp))
    execute_query("UPDATE users SET profits = profits + ?, balance = balance + ? WHERE id = ?",
                  (profit, profit, user_id))

    # Log trade outcome
    trade_type = "🟢 PROFIT" if profit > 0 else "🔴 LOSS"
    tier = "ELITE" if invested >= 5000 else "DIAMOND" if invested >= 2000 else "VIP" if invested >= 1000 else "Gold" if invested >= 500 else "Silver" if invested >= 200 else "Bronze" if invested >= 100 else "Starter"
    
    # Send detailed trade notification
    profit_percentage = (profit / amount) * 100
    send_telegram(f"📊 {tier} Trade: User {user_id} - {trade_type} of ${abs(profit):.2f} ({profit_percentage:+.1f}%) on ${amount:.2f} trade")

    return profit

# Check if user can trade (has unlocked bot)
def can_user_trade(user_id):
    user_data = execute_query("SELECT bot_unlocked, active FROM users WHERE id = ?", 
                             (user_id,), fetch=True)
    if user_data and user_data[0]:
        return user_data[0][0] and user_data[0][1]
    return False

# Get user investment tier
def get_user_tier(user_id):
    user_data = execute_query("SELECT invested FROM users WHERE id = ?", (user_id,), fetch=True)
    
    if user_data and user_data[0] and user_data[0][0]:
        invested = user_data[0][0]
    else:
        invested = 0

    if invested >= 5000:
        return "ELITE", "400%", "text-gold", "👑", 5000
    elif invested >= 2000:
        return "DIAMOND", "350%", "text-cyan", "💎", 2000
    elif invested >= 1000:
        return "VIP", "300%", "text-purple-400", "⭐", 1000
    elif invested >= 500:
        return "Gold", "250%", "text-yellow-400", "🔶", 500
    elif invested >= 200:
        return "Silver", "200%", "text-gray-300", "🔹", 200
    elif invested >= 100:
        return "Bronze", "150%", "text-orange-400", "🔸", 100
    else:
        return "Starter", "100%", "text-green-400", "🚀", 50

# Send to Telegram
def send_telegram(message):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")

# Generate QR code
def generate_qr_code(data):
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode()

# Create admin notification
def create_admin_notification(user_id, message, notification_type="message"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query('INSERT INTO admin_notifications (user_id, message, type, timestamp) VALUES (?, ?, ?, ?)',
                 (user_id, message, notification_type, timestamp))

# Auto-respond to user messages
def auto_respond_to_user(user_id):
    auto_response = random.choice(AUTO_RESPONSES)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query('INSERT INTO chat_messages (user_id, message, is_admin, is_auto_response, timestamp) VALUES (?, ?, ?, ?, ?)',
                 (user_id, auto_response, True, True, timestamp))
    return auto_response

# Verify Ethereum transaction
def verify_eth_transaction(tx_hash, expected_amount, to_address):
    try:
        # Get transaction receipt
        tx = w3.eth.get_transaction_receipt(tx_hash)
        if not tx:
            return False, "Transaction not found"

        if tx.status != 1:
            return False, "Transaction failed"

        if tx.to and tx.to.lower() != to_address.lower():
            return False, "Incorrect recipient address"

        tx_details = w3.eth.get_transaction(tx_hash)
        amount_eth = w3.from_wei(tx_details.value, 'ether')

        if abs(amount_eth - expected_amount) > expected_amount * 0.05:
            return False, f"Amount mismatch. Expected: {expected_amount} ETH, Got: {amount_eth} ETH"

        return True, f"Payment verified: {amount_eth} ETH received"

    except Exception as e:
        return False, f"Verification error: {str(e)}"

# Live trading data for dashboard
def get_live_trading_data():
    # More realistic live trading data
    return {
        'active_traders': random.randint(1800, 3200),
        'total_profits': f"${random.randint(750000, 1500000):,}",
        'live_trades': [
            {"pair": "BTC/USD", "action": "BUY", "profit": random.randint(80, 600), "time": "Just now"},
            {"pair": "ETH/USD", "action": "SELL", "profit": -random.randint(15, 80), "time": "2 min ago"},
            {"pair": "XRP/USD", "action": "BUY", "profit": random.randint(40, 300), "time": "5 min ago"},
            {"pair": "ADA/USD", "action": "BUY", "profit": random.randint(25, 200), "time": "8 min ago"},
            {"pair": "SOL/USD", "action": "SELL", "profit": -random.randint(10, 60), "time": "10 min ago"},
        ]
    }

# Routes (remaining routes stay the same but with improved trading algorithm)
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    ref_code = request.args.get('ref', '')
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        referred_by = request.form.get('ref', '')

        if len(username) < 3:
            flash("Username must be at least 3 characters long!")
            return render_template('register.html', ref_code=ref_code)

        if len(password) < 6:
            flash("Password must be at least 6 characters long!")
            return render_template('register.html', ref_code=ref_code)

        referral_code = generate_referral_code()
        try:
            # Insert user
            execute_query("INSERT INTO users (username, password, referral_code, referred_by) VALUES (?, ?, ?, ?)",
                      (username, password, referral_code, referred_by))
            
            # Get the user ID
            user_data = execute_query("SELECT id FROM users WHERE username = ?", (username,), fetch=True)
            user_id = user_data[0][0] if user_data else None

            if user_id:
                session['user_id'] = user_id
                session['username'] = username

                if referred_by:
                    execute_query("UPDATE users SET balance = balance + 50 WHERE referral_code = ?", (referred_by,))
                    send_telegram(f"🎉 Referral bonus: $50 to user with code {referred_by}")

                send_telegram(f"👤 New user registered: {username}")
                flash("Registration successful! Please login to continue.")
                return redirect(url_for('login'))

        except Exception as e:
            flash("Username already exists!" if "unique" in str(e).lower() else f"Registration error: {str(e)}")

    return render_template('register.html', ref_code=ref_code)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        user_data = execute_query("SELECT id, username FROM users WHERE username = ? AND password = ? AND active = TRUE", 
                                (username, password), fetch=True)

        if user_data:
            session['user_id'] = user_data[0][0]
            session['username'] = user_data[0][1]
            send_telegram(f"🔐 User logged in: {username}")
            return redirect(url_for('dashboard'))
        else:
            flash("Invalid username or password!")
    
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    try:
        user_data = execute_query('''SELECT username, balance, profits, referral_code,
                    support_fee_paid, invested, total_deposited, bot_unlocked, unlock_code_used, web3_wallet
                    FROM users WHERE id = ? AND active = TRUE''', (session['user_id'],), fetch=True)

        if not user_data:
            session.pop('user_id', None)
            flash("User not found or account deactivated. Please contact support.")
            return redirect(url_for('login'))

        trades = execute_query("SELECT amount, profit, status, timestamp FROM trades WHERE user_id = ? ORDER BY timestamp DESC LIMIT 10", 
                              (session['user_id'],), fetch=True)

        user_dict = {
            'username': user_data[0][0],
            'balance': user_data[0][1] or 0,
            'profits': user_data[0][2] or 0,
            'referral_code': user_data[0][3],
            'support_fee_paid': bool(user_data[0][4]),
            'invested': user_data[0][5] or 0,
            'total_deposited': user_data[0][6] or 0,
            'bot_unlocked': bool(user_data[0][7]),
            'unlock_code_used': user_data[0][8],
            'web3_wallet': user_data[0][9]
        }

        tier_name, max_profit, tier_color, tier_icon, min_amount = get_user_tier(session['user_id'])
        live_data = get_live_trading_data()
        can_trade = can_user_trade(session['user_id'])

        return render_template('dashboard.html',
                             user=user_dict,
                             trades=trades,
                             tier_name=tier_name,
                             max_profit=max_profit,
                             tier_color=tier_color,
                             tier_icon=tier_icon,
                             live_data=live_data,
                             can_trade=can_trade,
                             wallets=WALLETS,
                             min_amount=min_amount)

    except Exception as e:
        flash(f"Error loading dashboard: {str(e)}")
        return redirect(url_for('login'))

# IMPROVED AUTO TRADE FUNCTION
@app.route('/auto_trade', methods=['POST'])
def auto_trade():
    if 'user_id' not in session:
        return jsonify({'error': 'Please login first'}), 401

    if not can_user_trade(session['user_id']):
        return jsonify({'error': 'Bot not unlocked. Make a deposit or use an unlock code.'}), 400

    try:
        result = execute_query("SELECT invested FROM users WHERE id = ?", (session['user_id'],), fetch=True)
        invested = result[0][0] if result and result[0][0] else 100

        total_profit = 0
        profitable_trades = 0
        losing_trades = 0
        trades_count = random.randint(5, 12)  # More trades for better results

        for i in range(trades_count):
            # Vary trade amounts for realism
            base_amount = min(200, invested * 0.15)
            trade_amount = random.uniform(base_amount * 0.7, base_amount * 1.3)
            profit = simulate_trade(session['user_id'], trade_amount)
            total_profit += profit
            if profit > 0:
                profitable_trades += 1
            else:
                losing_trades += 1

        # Calculate success rate
        success_rate = (profitable_trades / trades_count) * 100

        return jsonify({
            'success': True,
            'total_profit': round(total_profit, 2),
            'trades_count': trades_count,
            'profitable_trades': profitable_trades,
            'losing_trades': losing_trades,
            'success_rate': round(success_rate, 1),
            'message': f'Auto trading completed! {trades_count} trades executed. Success rate: {success_rate:.1f}%. Total: ${total_profit:.2f}'
        })
    except Exception as e:
        return jsonify({'error': f'Auto trade error: {str(e)}'}), 500

# ... [Rest of your routes remain exactly the same] ...
# Web3 Wallet Connection, Deposit, Withdraw, Chat, Admin routes, etc.

# Web3 Wallet Connection
@app.route('/connect_wallet', methods=['POST'])
def connect_wallet():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Please login first'})

    wallet_address = request.json.get('wallet_address', '').strip()

    if not wallet_address or not Web3.is_address(wallet_address):
        return jsonify({'success': False, 'error': 'Invalid wallet address'})

    try:
        execute_query('UPDATE users SET web3_wallet = ? WHERE id = ?',
                     (wallet_address, session['user_id']))

        send_telegram(f"🔗 Wallet connected: {wallet_address} for user {session['user_id']}")
        return jsonify({'success': True, 'message': 'Wallet connected successfully'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# Web3 Payment Verification
@app.route('/verify_payment', methods=['POST'])
def verify_payment():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Please login first'})

    tx_hash = request.json.get('transaction_hash', '').strip()
    amount = float(request.json.get('amount', 0))
    crypto_type = request.json.get('crypto_type', 'ethereum')

    if not tx_hash:
        return jsonify({'success': False, 'error': 'Transaction hash required'})

    try:
        if crypto_type == 'ethereum':
            success, message = verify_eth_transaction(tx_hash, amount, WALLETS['ethereum'])
        else:
            success = True
            message = f"Payment received for {crypto_type.upper()}"

        if success:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            execute_query('''INSERT INTO web3_transactions (user_id, transaction_hash, amount, crypto_type, status, timestamp)
                      VALUES (?, ?, ?, ?, ?, ?)''',
                     (session['user_id'], tx_hash, amount, crypto_type, 'verified', timestamp))

            execute_query('''UPDATE users SET balance = balance + ?, invested = invested + ?,
                      total_deposited = total_deposited + ? WHERE id = ?''',
                     (amount, amount, amount, session['user_id']))

            # Create admin notification
            create_admin_notification(session['user_id'], f"User made payment of ${amount}. Please send unlock code.", "payment")

            send_telegram(f"✅ Payment Verified: ${amount} by user {session['user_id']}")

            return jsonify({
                'success': True,
                'message': f'Payment verified! ${amount} credited. Please wait for admin to send your unlock code.',
            })
        else:
            return jsonify({'success': False, 'error': message})

    except Exception as e:
        return jsonify({'success': False, 'error': f'Payment verification failed: {str(e)}'})

@app.route('/deposit', methods=['GET', 'POST'])
def deposit():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        try:
            amount = float(request.form['amount'])
            crypto_type = request.form.get('crypto_type', 'ethereum')

            if amount < 50:
                flash("Minimum deposit is $50!")
                return redirect(url_for('deposit'))

            wallet_address = WALLETS.get(crypto_type, WALLETS['ethereum'])
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            execute_query('''INSERT INTO deposits (user_id, amount, crypto_type, wallet_address, timestamp)
                       VALUES (?, ?, ?, ?, ?)''',
                     (session['user_id'], amount, crypto_type, wallet_address, timestamp))

            qr_code = generate_qr_code(wallet_address)
            send_telegram(f"💰 Deposit requested: ${amount} in {crypto_type} by user {session['user_id']}")

            return render_template('deposit.html',
                                success=True,
                                wallet=wallet_address,
                                qr_code=qr_code,
                                amount=amount,
                                crypto_type=crypto_type,
                                wallets=WALLETS)

        except ValueError:
            flash("Please enter a valid amount!")

    return render_template('deposit.html', wallets=WALLETS)

@app.route('/unlock_bot', methods=['POST'])
def unlock_bot():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Please login first'}), 401

    unlock_code = request.form.get('unlock_code', '').strip().upper()

    if not unlock_code:
        return jsonify({'success': False, 'error': 'Please enter an unlock code'})

    try:
        code_data = execute_query('''SELECT amount, used FROM unlock_codes WHERE code = ?''', 
                                (unlock_code,), fetch=True)

        if not code_data:
            return jsonify({'success': False, 'error': 'Invalid unlock code'})

        amount, used = code_data[0]

        if used:
            return jsonify({'success': False, 'error': 'This code has already been used'})

        execute_query('''UPDATE unlock_codes SET used = TRUE, used_by = ?, used_at = ? WHERE code = ?''',
                 (session['user_id'], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), unlock_code))

        execute_query('''UPDATE users SET bot_unlocked = TRUE, unlock_code_used = ? WHERE id = ?''',
                 (unlock_code, session['user_id']))

        # Start trading with the deposited amount - multiple demo trades
        demo_trades = []
        total_demo_profit = 0
        
        for i in range(3):
            demo_profit = simulate_trade(session['user_id'], amount / 3)
            demo_trades.append(demo_profit)
            total_demo_profit += demo_profit

        send_telegram(f"🔓 Bot Unlocked: User {session['user_id']} used code {unlock_code}. Demo trades profit: ${total_demo_profit:.2f}")

        return jsonify({
            'success': True,
            'message': f'Bot unlocked successfully! Demo trades completed with total profit: ${total_demo_profit:.2f}',
            'profit': total_demo_profit
        })

    except Exception as e:
        return jsonify({'success': False, 'error': f'Error unlocking bot: {str(e)}'})

@app.route('/trade', methods=['POST'])
def trade():
    if 'user_id' not in session:
        return jsonify({'error': 'Please login first'}), 401

    if not can_user_trade(session['user_id']):
        return jsonify({'error': 'You need to unlock the bot first! Make a deposit or use an unlock code.'}), 400

    try:
        amount = float(request.form.get('amount', 100))
        profit = simulate_trade(session['user_id'], amount)

        trade_type = "profit" if profit > 0 else "loss"
        profit_percentage = (profit / amount) * 100
        
        return jsonify({
            'success': True,
            'profit': profit,
            'trade_type': trade_type,
            'profit_percentage': round(profit_percentage, 1),
            'message': f'Trade successful! {"Profit" if profit > 0 else "Loss"}: ${abs(profit):.2f} ({profit_percentage:+.1f}%)'
        })
    except Exception as e:
        return jsonify({'error': f'Trade error: {str(e)}'}), 500

# Enhanced Chat System
@app.route('/chat')
def chat():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    messages = execute_query('''SELECT cm.message, cm.is_admin, cm.is_auto_response, cm.timestamp, u.username
               FROM chat_messages cm
               LEFT JOIN users u ON cm.user_id = u.id
               WHERE cm.user_id = ?
               ORDER BY cm.timestamp DESC LIMIT 50''', (session['user_id'],), fetch=True)

    return render_template('chat.html', messages=messages)

@app.route('/send_message', methods=['POST'])
def send_message():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Please login first'})

    message = request.form.get('message', '').strip()
    if not message:
        return jsonify({'success': False, 'error': 'Message cannot be empty'})

    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        execute_query('''INSERT INTO chat_messages (user_id, message, is_admin, timestamp)
                   VALUES (?, ?, ?, ?)''', (session['user_id'], message, False, timestamp))

        # Create admin notification
        execute_query('''INSERT INTO admin_notifications (user_id, message, type, timestamp)
                   VALUES (?, ?, ?, ?)''', (session['user_id'], f"New message: {message}", "message", timestamp))

        send_telegram(f"💬 New message from user {session['user_id']}: {message}")

        # Auto-respond if admin doesn't respond within 1 minute (simulated)
        auto_respond_to_user(session['user_id'])

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/get_messages')
def get_messages():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    messages = execute_query('''SELECT cm.message, cm.is_admin, cm.is_auto_response, cm.timestamp, u.username
               FROM chat_messages cm
               LEFT JOIN users u ON cm.user_id = u.id
               WHERE cm.user_id = ?
               ORDER BY cm.timestamp DESC LIMIT 50''', (session['user_id'],), fetch=True)

    messages_list = []
    for msg in messages:
        messages_list.append({
            'message': msg[0],
            'is_admin': bool(msg[1]),
            'is_auto_response': bool(msg[2]),
            'timestamp': msg[3],
            'username': msg[4] or 'Admin'
        })

    return jsonify({'messages': messages_list})

# Enhanced Withdrawal System
@app.route('/withdraw', methods=['GET', 'POST'])
def withdraw():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    try:
        user_data = execute_query("SELECT balance, profits, support_fee_paid FROM users WHERE id = ?", 
                                (session['user_id'],), fetch=True)

        if not user_data:
            return redirect(url_for('login'))

        balance = user_data[0][0] or 0
        profits = user_data[0][1] or 0
        fee_paid = bool(user_data[0][2])

        if request.method == 'POST':
            try:
                amount = float(request.form['amount'])
                withdraw_method = request.form.get('withdraw_method', 'crypto')
                crypto_type = request.form.get('crypto_type', 'ethereum')
                wallet_address = request.form.get('wallet_address', '')
                paypal_email = request.form.get('paypal_email', '')

                if amount < 500:
                    flash("Minimum withdrawal is $500!")
                    return redirect(url_for('withdraw'))

                if amount > balance:
                    flash("Insufficient balance!")
                    return redirect(url_for('withdraw'))

                if not fee_paid:
                    flash("Please pay the $50 withdrawal fee first!")
                    return redirect(url_for('pay_fee'))

                if withdraw_method == 'crypto' and not wallet_address:
                    flash("Please provide your wallet address!")
                    return redirect(url_for('withdraw'))

                if withdraw_method == 'paypal' and not paypal_email:
                    flash("Please provide your PayPal email!")
                    return redirect(url_for('withdraw'))

                execute_query("UPDATE users SET balance = balance - ? WHERE id = ?", (amount, session['user_id']))

                method_info = f"via {withdraw_method}"
                if withdraw_method == 'crypto':
                    method_info += f" ({crypto_type})"
                elif withdraw_method == 'paypal':
                    method_info += f" ({paypal_email})"

                send_telegram(f"💸 Withdrawal request: ${amount} {method_info} by user {session['user_id']}")
                flash("Withdrawal request submitted! Funds will be sent within 24 hours.")
                return redirect(url_for('dashboard'))

            except ValueError:
                flash("Please enter a valid amount!")

        return render_template('withdraw.html', balance=balance, profits=profits, fee_paid=fee_paid)

    except Exception as e:
        flash(f"Error loading withdrawal page: {str(e)}")
        return redirect(url_for('dashboard'))

@app.route('/pay_fee', methods=['GET', 'POST'])
def pay_fee():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        try:
            execute_query("UPDATE users SET support_fee_paid = TRUE WHERE id = ?", (session['user_id'],))

            send_telegram(f"💰 Withdrawal fee paid by user {session['user_id']}")
            flash("Withdrawal fee paid! You can now make withdrawals.")
            return redirect(url_for('withdraw'))
        except Exception as e:
            flash(f"Payment error: {str(e)}")

    qr_code = generate_qr_code(WALLETS['ethereum'])
    return render_template('pay_fee.html', wallet=WALLETS['ethereum'], qr_code=qr_code)

@app.route('/faq')
def faq():
    return render_template('faq.html')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('username', None)
    flash("Logged out successfully!")
    return redirect(url_for('login'))

# Enhanced Admin Routes
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username == 'admin' and password == 'admin123':
            session['admin'] = True
            session['admin_username'] = 'admin'
            return redirect(url_for('admin_dashboard'))
        else:
            flash("Invalid admin credentials!")
    return render_template('admin_login.html')

@app.route('/admin/dashboard')
def admin_dashboard():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))

    try:
        # Get dashboard statistics
        total_users = execute_query("SELECT COUNT(*) FROM users", fetch=True)[0][0]
        active_users = execute_query("SELECT COUNT(*) FROM users WHERE invested > 0", fetch=True)[0][0]
        unlocked_users = execute_query("SELECT COUNT(*) FROM users WHERE bot_unlocked = TRUE", fetch=True)[0][0]
        total_balance = execute_query("SELECT SUM(balance) FROM users", fetch=True)[0][0] or 0
        total_invested = execute_query("SELECT SUM(invested) FROM users", fetch=True)[0][0] or 0
        total_profits = execute_query("SELECT SUM(profits) FROM users", fetch=True)[0][0] or 0
        used_codes = execute_query("SELECT COUNT(*) FROM unlock_codes WHERE used = TRUE", fetch=True)[0][0]
        available_codes = execute_query("SELECT COUNT(*) FROM unlock_codes WHERE used = FALSE", fetch=True)[0][0]

        # Get recent users
        recent_users = execute_query('''SELECT id, username, invested, profits, balance, bot_unlocked,
                    total_deposited, created_at FROM users ORDER BY created_at DESC LIMIT 20''', fetch=True)

        # Get unread notifications
        notifications = execute_query('''SELECT an.id, an.user_id, u.username, an.message, an.type, an.timestamp
                   FROM admin_notifications an
                   JOIN users u ON an.user_id = u.id
                   WHERE an.read = FALSE
                   ORDER BY an.timestamp DESC LIMIT 10''', fetch=True)

        # Get recent messages
        recent_messages = execute_query('''SELECT cm.user_id, u.username, cm.message, cm.timestamp
                   FROM chat_messages cm
                   JOIN users u ON cm.user_id = u.id
                   WHERE cm.is_admin = FALSE
                   ORDER BY cm.timestamp DESC LIMIT 10''', fetch=True)

        return render_template('admin_dashboard.html',
                         total_users=total_users,
                         active_users=active_users,
                         unlocked_users=unlocked_users,
                         total_balance=total_balance,
                         total_invested=total_invested,
                         total_profits=total_profits,
                         used_codes=used_codes,
                         available_codes=available_codes,
                         recent_users=recent_users,
                         notifications=notifications,
                         recent_messages=recent_messages)

    except Exception as e:
        flash(f"Error loading admin dashboard: {str(e)}")
        return redirect(url_for('admin_login'))

@app.route('/admin/generate_code', methods=['POST'])
def admin_generate_code():
    if not session.get('admin'):
        return jsonify({'success': False, 'error': 'Unauthorized'})

    try:
        amount = float(request.form.get('amount', 50))
        code = generate_unlock_code(amount)

        return jsonify({'success': True, 'code': code, 'amount': amount})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/send_unlock_code', methods=['POST'])
def admin_send_unlock_code():
    if not session.get('admin'):
        return jsonify({'success': False, 'error': 'Unauthorized'})

    user_id = request.form.get('user_id')
    amount = float(request.form.get('amount', 50))

    if not user_id:
        return jsonify({'success': False, 'error': 'User ID required'})

    try:
        # Generate unique unlock code
        unlock_code = generate_unlock_code(amount)

        # Send message to user with unlock code
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = f"🔓 Your unlock code: {unlock_code} for ${amount} deposit. Use this code in your dashboard to activate trading."

        execute_query('''INSERT INTO chat_messages (user_id, message, is_admin, timestamp)
                   VALUES (?, ?, ?, ?)''', (user_id, message, True, timestamp))

        # Mark payment notification as read
        execute_query('''UPDATE admin_notifications SET read = TRUE
                   WHERE user_id = ? AND type = 'payment' AND read = FALSE''', (user_id,))

        send_telegram(f"🔐 Admin sent unlock code {unlock_code} (${amount}) to user {user_id}")
        return jsonify({'success': True, 'code': unlock_code, 'message': 'Unlock code sent to user'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/send_message', methods=['POST'])
def admin_send_message():
    if not session.get('admin'):
        return jsonify({'success': False, 'error': 'Unauthorized'})

    user_id = request.form.get('user_id')
    message = request.form.get('message', '').strip()

    if not message or not user_id:
        return jsonify({'success': False, 'error': 'User ID and message required'})

    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        execute_query('''INSERT INTO chat_messages (user_id, message, is_admin, timestamp)
                   VALUES (?, ?, ?, ?)''', (user_id, message, True, timestamp))

        # Mark notifications as read for this user
        execute_query('''UPDATE admin_notifications SET read = TRUE
                   WHERE user_id = ? AND read = FALSE''', (user_id,))

        send_telegram(f"💬 Admin message to user {user_id}: {message}")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/delete_user', methods=['POST'])
def admin_delete_user():
    if not session.get('admin'):
        return jsonify({'success': False, 'error': 'Unauthorized'})

    user_id = request.form.get('user_id')

    if not user_id:
        return jsonify({'success': False, 'error': 'User ID required'})

    try:
        # Deactivate user instead of deleting to preserve data
        execute_query('UPDATE users SET active = FALSE WHERE id = ?', (user_id,))

        # Get username for notification
        username_data = execute_query('SELECT username FROM users WHERE id = ?', (user_id,), fetch=True)
        username = username_data[0][0] if username_data else 'Unknown'

        send_telegram(f"🗑️ Admin deleted user: {username} (ID: {user_id})")
        return jsonify({'success': True, 'message': f'User {username} deactivated successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/get_user_messages')
def admin_get_user_messages():
    if not session.get('admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    user_id = request.args.get('user_id')

    if not user_id:
        return jsonify({'error': 'User ID required'}), 400

    try:
        messages = execute_query('''SELECT cm.message, cm.is_admin, cm.timestamp, u.username
                   FROM chat_messages cm
                   LEFT JOIN users u ON cm.user_id = u.id
                   WHERE cm.user_id = ?
                   ORDER BY cm.timestamp DESC LIMIT 50''', (user_id,), fetch=True)

        messages_list = []
        for msg in messages:
            messages_list.append({
                'message': msg[0],
                'is_admin': bool(msg[1]),
                'timestamp': msg[2],
                'username': msg[3] or 'Admin'
            })

        return jsonify({'messages': messages_list})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/mark_notification_read', methods=['POST'])
def admin_mark_notification_read():
    if not session.get('admin'):
        return jsonify({'success': False, 'error': 'Unauthorized'})

    notification_id = request.form.get('notification_id')

    if not notification_id:
        return jsonify({'success': False, 'error': 'Notification ID required'})

    try:
        execute_query('UPDATE admin_notifications SET read = TRUE WHERE id = ?', (notification_id,))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/get_notifications')
def admin_get_notifications():
    if not session.get('admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        notifications = execute_query('''SELECT an.id, an.user_id, u.username, an.message, an.type, an.timestamp
                   FROM admin_notifications an
                   JOIN users u ON an.user_id = u.id
                   WHERE an.read = FALSE
                   ORDER BY an.timestamp DESC LIMIT 20''', fetch=True)

        notifications_list = []
        for notif in notifications:
            notifications_list.append({
                'id': notif[0],
                'user_id': notif[1],
                'username': notif[2],
                'message': notif[3],
                'type': notif[4],
                'timestamp': notif[5]
            })

        return jsonify({'notifications': notifications_list})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/simulate_live')
def simulate_live_trading():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))

    active_users = execute_query("SELECT id, invested FROM users WHERE invested > 0 AND bot_unlocked = TRUE AND active = TRUE", fetch=True)

    trades_executed = 0
    total_profit = 0

    for user in active_users:
        user_id, invested = user
        # Higher chance of trading for active users
        if random.random() < 0.6:
            trade_amount = random.uniform(50, min(500, invested * 0.3))
            profit = simulate_trade(user_id, trade_amount)
            total_profit += profit
            trades_executed += 1

    return f"Live trading simulation completed! {trades_executed} trades executed. Total profit generated: ${total_profit:.2f}"

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    session.pop('admin_username', None)
    flash("Admin logged out successfully!")
    return redirect(url_for('admin_login'))

# API endpoint for live chart data
@app.route('/api/live_chart')
def live_chart_data():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    # Generate realistic chart data with some volatility
    data_points = 20
    base_value = random.uniform(100, 500)
    chart_data = []
    labels = []

    for i in range(data_points):
        # Add realistic market movements
        movement = random.uniform(-15, 15)
        base_value += movement
        base_value = max(50, base_value)
        chart_data.append(round(base_value, 2))
        labels.append(f"{i+1}h")

    current_change = chart_data[-1] - chart_data[0]

    return jsonify({
        'labels': labels,
        'data': chart_data,
        'current_price': chart_data[-1],
        'change': round(current_change, 2),
        'change_percent': round((current_change / chart_data[0]) * 100, 2)
    })

@app.route('/debug-db')
def debug_db():
    try:
        database_url = os.environ.get('DATABASE_URL', 'Not set')
        conn = get_db_connection()

        cursor = conn.cursor()
        cursor.execute("SELECT version();")
        db_version = cursor.fetchone()[0]
        cursor.close()
        conn.close()

        return jsonify({
            'database_url_set': bool(os.environ.get('DATABASE_URL')),
            'database_url_length': len(database_url) if database_url != 'Not set' else 0,
            'database_type': 'PostgreSQL',
            'database_version': db_version,
            'status': '✅ Database connected successfully!'
        })
    except Exception as e:
        return jsonify({
            'error': str(e),
            'database_url_set': bool(os.environ.get('DATABASE_URL')),
            'status': '❌ Database connection failed'
        })

if __name__ == '__main__':
    init_db()
    print("✅ Database initialized successfully!")
    print("🚀 Starting Advanced TradingView AI Bot with SMART Trading Algorithm")
    print("📊 User Dashboard available")
    print("👑 Admin Dashboard: /admin/login")
    print("🔑 Admin credentials: admin / admin123")
    print("💡 SMART Trading: Balanced profits & losses based on investment tiers")

    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'

    app.run(debug=debug, host='0.0.0.0', port=port)
