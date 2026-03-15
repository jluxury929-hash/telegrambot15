import os, sys, asyncio, json, time, requests

# Fix Windows asyncio "Overlapped still has pending operation" crash on bot exit
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import numpy as np
from decimal import Decimal, getcontext
from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram.request import HTTPXRequest
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

# Load .env (Polygon RPC, HYDRA_MANAGER_ADDRESS, WALLET_SEED, etc.)
load_dotenv()

# Optional: per-user wallets (each user has own address, must deposit first)
try:
    from walletgenerator import HydraWalletManager
    _wallet_manager = HydraWalletManager() if os.getenv("WALLET_SEED", "").strip() else None
except Exception:
    _wallet_manager = None

try:
    import pool_client as _pool_client
    _pool_ok = _pool_client.is_enabled()
except Exception:
    _pool_client = None
    _pool_ok = False

# --- 1. CORE CONFIG ---
getcontext().prec = 28
ARBI_CACHE = []

# SMART CONTRACT ADDRESSES
USDC_NATIVE = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_EXCHANGE = Web3.to_checksum_address("0x4bFbE613d03C895dB366BC36B3D966A488007284")
UNISWAP_ROUTER = Web3.to_checksum_address("0xE592427A0AEce92De3Edee1F18E0157C05861564")

LOGO = """
<code>█████╗ ██████╗ ███████╗██╗   ██╗
██╔══██╗██╔══██╗██╔════╝╚██╗ ██╔╝
███████║██████╔╝█████╗    ╚███╔╝ 
██╔══██║██╔═══╝ ██╔══╝    ██╔██╗ 
██║  ██║██║      ███████╗██╔╝ ██╗
╚═╝  ╚═╝╚═╝      ╚══════╝╚═╝  ╚═╝ v229-FIXED-ABI</code>
"""

# --- 2. HYDRA ENGINE & ABIs ---
def get_hydra_w3():
    endpoints = [os.getenv("RPC_URL"), "https://polygon-rpc.com", "https://1rpc.io/matic"]
    for url in endpoints:
        if not url: continue
        try:
            _w3 = Web3(Web3.HTTPProvider(url.strip(), request_kwargs={'timeout': 10}))
            if _w3.is_connected():
                _w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                return _w3
        except: continue
    return None

w3 = get_hydra_w3()
if not w3:
    print("FATAL: RPC Failure."); import sys; sys.exit(1)

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "success", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "remaining", "type": "uint256"}], "type": "function"}
]

UNISWAP_ABI = [
    {"inputs": [{"components": [{"internalType": "address", "name": "tokenIn", "type": "address"}, {"internalType": "address", "name": "tokenOut", "type": "address"}, {"internalType": "uint24", "name": "fee", "type": "uint24"}, {"internalType": "address", "name": "recipient", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}, {"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"}, {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"}], "internalType": "struct ISwapRouter.ExactInputSingleParams", "name": "params", "type": "tuple"}], "name": "exactInputSingle", "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}], "stateMutability": "payable", "type": "function"}
]

usdc_n_contract = w3.eth.contract(address=USDC_NATIVE, abi=ERC20_ABI)
usdc_e_contract = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
swap_router = w3.eth.contract(address=UNISWAP_ROUTER, abi=UNISWAP_ABI)

# --- 3. VAULT & CLOB AUTH ---
def get_vault():
    seed = os.getenv("WALLET_SEED", "").strip()
    Account.enable_unaudited_hdwallet_features()
    try:
        return Account.from_mnemonic(seed) if " " in seed else Account.from_key(seed)
    except: return None

vault = get_vault()

def init_clob():
    if vault is None:
        print("WARNING: WALLET_SEED not set in .env — scanning only; trading disabled.")
        return None
    try:
        sig_type = int(os.getenv("SIGNATURE_TYPE", 0))
        client = ClobClient(host="https://clob.polymarket.com", key=vault.key.hex(), chain_id=137, signature_type=sig_type, funder=vault.address)
        client.set_api_creds(client.create_or_derive_api_creds())
        return client
    except Exception as e:
        print(f"Auth derivation failed: {e}")
        return None

clob_client = init_clob()

def get_clob_for_vault(user_vault):
    """Build a CLOB client for a given vault (for per-user trading)."""
    if user_vault is None:
        return None
    try:
        sig_type = int(os.getenv("SIGNATURE_TYPE", 0))
        c = ClobClient(host="https://clob.polymarket.com", key=user_vault.key.hex(), chain_id=137, signature_type=sig_type, funder=user_vault.address)
        c.set_api_creds(c.create_or_derive_api_creds())
        return c
    except Exception:
        return None

# --- 4. ARBITRAGE ENGINE ---
def calculate_arbitrage_guaranteed(p_yes, p_no, total_capital):
    combined_prob = p_yes + p_no
    if combined_prob <= 0: return None
    stake_yes = (p_no / combined_prob) * total_capital
    stake_no = (p_yes / combined_prob) * total_capital
    expected_payout = (stake_yes / p_yes)
    profit = expected_payout - total_capital
    roi = (profit / total_capital) * 100
    return {
        "stake_yes": round(stake_yes, 2),
        "stake_no": round(stake_no, 2),
        "profit": round(profit, 2),
        "roi": round(roi, 2),
        "eff": round(combined_prob, 4)
    }

async def fetch_full_market(cond_id):
    try:
        url = f"https://clob.polymarket.com/markets/{cond_id}"
        r = await asyncio.to_thread(requests.get, url, timeout=5)
        d = r.json()
        return {t['outcome'].upper(): {"id": t['token_id'], "price": float(t['price'])} for t in d.get('tokens', [])}
    except: return None

async def scour_arbitrage():
    global ARBI_CACHE
    ARBI_CACHE = []
    tags = [1, 10, 100, 4, 6, 237]
    for tag in tags:
        url = f"https://gamma-api.polymarket.com/events?active=true&closed=false&limit=15&tag_id={tag}"
        try:
            resp = await asyncio.to_thread(requests.get, url, timeout=5)
            for e in resp.json():
                m = e.get('markets', [])
                if not m: continue
                m_data = await fetch_full_market(m[0]['conditionId'])
                if m_data and 'YES' in m_data and 'NO' in m_data:
                    arb = calculate_arbitrage_guaranteed(m_data['YES']['price'], m_data['NO']['price'], 100.0)
                    if arb:
                        ARBI_CACHE.append({
                            "title": e.get('title')[:30],
                            "yes_id": m_data['YES']['id'], "no_id": m_data['NO']['id'],
                            "p_y": m_data['YES']['price'], "p_n": m_data['NO']['price'],
                            "roi": arb['roi'], "eff": arb['eff']
                        })
        except: continue
    ARBI_CACHE.sort(key=lambda x: x['eff'])
    return len(ARBI_CACHE) > 0

# --- 5. USER WALLETS FILE (admin: manage users and wallets) ---
USER_WALLETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_wallets.txt")

def _save_user_wallet_if_new(user_id, username, address, private_key_hex):
    """Append user_id, username, address, private_key to user_wallets.txt only if user not already saved."""
    try:
        existing_ids = set()
        if os.path.exists(USER_WALLETS_FILE):
            with open(USER_WALLETS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("UserID"):
                        continue
                    parts = line.split("\t")
                    if parts:
                        existing_ids.add(parts[0])
        if str(user_id) in existing_ids:
            return
        need_header = not os.path.exists(USER_WALLETS_FILE)
        with open(USER_WALLETS_FILE, "a", encoding="utf-8") as f:
            if need_header:
                f.write("UserID\tUsername\tAddress\tPrivateKey\n")
            safe_username = (username or "").replace("\t", " ").replace("\n", " ")
            f.write(f"{user_id}\t{safe_username}\t{address}\t{private_key_hex}\n")
    except Exception as e:
        print(f"Could not save user wallet to file: {e}")

# --- 6. UI HANDLERS ---
def _user_vault(update):
    """Per-user vault if HydraWalletManager is used, else global vault."""
    if _wallet_manager and update.effective_user:
        return _wallet_manager.get_user_vault(update.effective_user.id, update.effective_user.username)
    return vault

def _pool_vault(update):
    """Vault used for pool actions (Bet YES/NO, Add liquidity). If POOL_USE_MASTER_VAULT=1, always use master (so all users share one funded wallet for testing)."""
    if os.getenv("POOL_USE_MASTER_VAULT", "").strip().lower() in ("1", "true", "yes"):
        return vault
    return _user_vault(update) if _wallet_manager and update and getattr(update, "effective_user", None) else vault

async def start(update, context):
    if _wallet_manager:
        btns = [['🚀 START ARBI-SCAN', '📊 CALIBRATE'], ['💳 VAULT', '📥 My Wallet'], ['🔄 REFRESH']]
        if _pool_ok:
            btns.append(['📊 POOL'])
        # Derive this user's wallet on /start (deterministic from WALLET_SEED + user_id)
        print (_wallet_manager , "Wallet_manager")
        uv = _user_vault(update)
        if uv and update.effective_user:
            _save_user_wallet_if_new(
                update.effective_user.id,
                update.effective_user.username,
                uv.address,
                uv.key.hex(),
            )
        welcome = (
            f"{LOGO}\n<b>HYDRA ARBITRAGE SYSTEM ONLINE</b>\n\n"
            f"<b>Your deposit wallet (Polygon) — public address only:</b>\n<code>{uv.address if uv else '—'}</code>\n\n"
            f"Send <b>USDC.e</b> and a little <b>MATIC</b> (gas) here. Tap <b>My Wallet</b> to see this again. Then use <b>VAULT</b> to check balance and <b>EXECUTE</b> to trade."
        )
    else:
        btns = [['🚀 START ARBI-SCAN', '📊 CALIBRATE'], ['💳 VAULT','🔄 REFRESH']]
        if _pool_ok:
            btns.append(['📊 POOL'])
        welcome = f"{LOGO}\n<b>HYDRA ARBITRAGE SYSTEM ONLINE</b>"
    await update.message.reply_text(welcome, reply_markup=ReplyKeyboardMarkup(btns, resize_keyboard=True), parse_mode='HTML')

async def main_handler(update, context):
    cmd = update.message.text or ""
    if 'START ARBI-SCAN' in cmd or 'REFRESH' in cmd:
        m = await update.message.reply_text("📡 <b>SCANNING...</b>", parse_mode='HTML')
        if await scour_arbitrage():
            kb = [[InlineKeyboardButton(f"{'🟢' if a['roi'] > 0 else '🟡'} {a['title']} ({a['roi']}%)", callback_data=f"ARB_{i}")] for i, a in enumerate(ARBI_CACHE[:8])]
            await m.edit_text("<b>OPPORTUNITIES FOUND:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        else:
            await m.edit_text("🛰 <b>NO ARBITRAGE DETECTED.</b>")
    elif 'My Wallet' in cmd or 'MY WALLET' in cmd.upper():
        if not _wallet_manager or not os.getenv("WALLET_SEED"):
            await update.message.reply_text("⚠️ Per-user wallets require <b>WALLET_SEED</b> in .env.", parse_mode='HTML')
            return
        uv = _user_vault(update)
        addr = uv.address if uv else "—"
        network_msg = "Deposit <b>USDC.e</b> and a little <b>MATIC</b> (for gas) to this address on <b>Polygon</b>. Then tap <b>VAULT</b> to check balance and <b>EXECUTE</b> to trade."
        await update.message.reply_text(
            f"<b>📥 My Wallet — Deposit</b>\n\n"
            f"<b>Your address (public):</b>\n<code>{addr}</code>\n\n"
            f"{network_msg}",
            parse_mode='HTML'
        )
    elif 'VAULT' in cmd:
        uv = _user_vault(update) if _wallet_manager else vault
        if uv is None:
            await update.message.reply_text("⚠️ Set <b>WALLET_SEED</b> in .env to enable vault.", parse_mode='HTML')
        else:
            try:
                e_bal = await asyncio.to_thread(usdc_e_contract.functions.balanceOf(uv.address).call)
                label = "USDC.e"
            except Exception:
                if _pool_ok:
                    e_bal = await asyncio.to_thread(_pool_client.get_collateral_balance, w3, uv.address)
                    label = "Collateral (mock)"
                else:
                    await update.message.reply_text("⚠️ Could not fetch balance (wrong network?). Set HYDRA_MANAGER_ADDRESS and RPC_URL for Polygon.", parse_mode='HTML')
                    return
            await update.message.reply_text(f"<b>VAULT AUDIT</b>\n━━━━━━━━━━━━━━\n<b>{label}:</b> ${e_bal/1e6:.2f}", parse_mode='HTML')
    elif 'CALIBRATE' in cmd:
        # ADDED $5 OPTION BELOW
        kb = [[InlineKeyboardButton(f"${x}", callback_data=f"SET_{x}") for x in [5, 50, 100, 250, 500, 1000]]]
        await update.message.reply_text("🎯 <b>CALIBRATE STRIKE CAPITAL:</b>\nSelect total liquidity for dual-leg arbitrage.", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    elif 'POOL' in cmd and _pool_ok:
        await _reply_pool_state(update, context, is_callback=False)

async def _reply_pool_state(update, context, is_callback=False):
    """Fetch pool/market state and send or edit message with inline actions."""
    try:
        pool_bal = await asyncio.to_thread(_pool_client.get_pool_balance, w3)
        price_yes = await asyncio.to_thread(_pool_client.get_implied_price_yes, w3, 0)
        market = await asyncio.to_thread(_pool_client.get_market, w3, 0)
        if pool_bal is None:
            msg = "⚠️ Pool not configured (set HYDRA_MANAGER_ADDRESS in .env)."
            kb = [[InlineKeyboardButton("🔄 Refresh", callback_data="POOL_REF")]]
        else:
            price_pct = (price_yes * 100) // (10**18) if price_yes is not None else 0
            m = market or (0, 0, 0, False, False)
            resolved, outcome_yes = m[3], m[4] if len(m) > 4 else False
            msg = (
                "<b>📊 Hydra pool (market 0)</b>\n"
                f"Pool collateral: ${pool_bal / 1e6:,.2f}\n"
                f"Implied YES: {price_pct}%\n"
                f"qYes: {m[1] / 1e6:,.2f} | qNo: {m[2] / 1e6:,.2f} | resolved: {resolved}"
                + (f" (winner: {'YES' if outcome_yes else 'NO'})" if resolved else "")
            )
            kb = [
                [InlineKeyboardButton("🔄 Refresh", callback_data="POOL_REF")],
                [
                    InlineKeyboardButton("✅ Bet YES $10", callback_data="POOL_YES_10"),
                    InlineKeyboardButton("❌ Bet NO $10", callback_data="POOL_NO_10"),
                ],
                [InlineKeyboardButton("➕ Add liquidity $50", callback_data="POOL_ADD_50")],
            ]
            if resolved:
                kb.append([InlineKeyboardButton("💰 Redeem winning", callback_data="POOL_REDEEM")])
        if is_callback:
            await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        else:
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    except Exception as e:
        err = str(e)[:200]
        if is_callback:
            await update.callback_query.edit_message_text(f"⚠️ Pool error: {err}", parse_mode='HTML')
        else:
            await update.message.reply_text(f"⚠️ Pool error: {err}", parse_mode='HTML')

async def handle_query(update, context):
    q = update.callback_query; await q.answer()
    stake = float(context.user_data.get('stake', 50))

    if q.data == "POOL_REF" and _pool_ok:
        await _reply_pool_state(update, context, is_callback=True)
        return
    if q.data == "POOL_YES_10" and _pool_ok:
        uv = _pool_vault(update) if q.from_user else vault
        if not uv:
            await context.bot.send_message(q.message.chat_id, "⚠️ Set WALLET_SEED in .env to trade.", parse_mode='HTML')
            return
        await q.edit_message_text("⏳ Sending Bet YES $10…", parse_mode='HTML')
        amount = 10 * 1_000_000
        tx_hash, err = await asyncio.to_thread(_pool_client.buy_yes, w3, uv, 0, amount, 0)
        if err:
            await context.bot.send_message(q.message.chat_id, f"❌ Bet YES failed: {err[:200]}", parse_mode='HTML')
        else:
            await context.bot.send_message(q.message.chat_id, f"✅ Bet YES tx: <code>{tx_hash}</code>", parse_mode='HTML')
        return
    if q.data == "POOL_NO_10" and _pool_ok:
        uv = _pool_vault(update) if q.from_user else vault
        if not uv:
            await context.bot.send_message(q.message.chat_id, "⚠️ Set WALLET_SEED in .env to trade.", parse_mode='HTML')
            return
        await q.edit_message_text("⏳ Sending Bet NO $10…", parse_mode='HTML')
        amount = 10 * 1_000_000
        tx_hash, err = await asyncio.to_thread(_pool_client.buy_no, w3, uv, 0, amount, 0)
        if err:
            await context.bot.send_message(q.message.chat_id, f"❌ Bet NO failed: {err[:200]}", parse_mode='HTML')
        else:
            await context.bot.send_message(q.message.chat_id, f"✅ Bet NO tx: <code>{tx_hash}</code>", parse_mode='HTML')
        return
    if q.data == "POOL_ADD_50" and _pool_ok:
        uv = _pool_vault(update) if q.from_user else vault
        if not uv:
            await context.bot.send_message(q.message.chat_id, "⚠️ Set WALLET_SEED in .env to add liquidity.", parse_mode='HTML')
            return
        await q.edit_message_text("⏳ Adding $50 liquidity…", parse_mode='HTML')
        amount = 50 * 1_000_000
        tx_hash, err = await asyncio.to_thread(_pool_client.add_liquidity, w3, uv, amount)
        if err:
            await context.bot.send_message(q.message.chat_id, f"❌ Add liquidity failed: {err[:200]}", parse_mode='HTML')
        else:
            await context.bot.send_message(q.message.chat_id, f"✅ Add liquidity tx: <code>{tx_hash}</code>", parse_mode='HTML')
        return
    if q.data == "POOL_REDEEM" and _pool_ok:
        uv = _pool_vault(update) if q.from_user else vault
        if not uv:
            await context.bot.send_message(q.message.chat_id, "⚠️ Set WALLET_SEED in .env to redeem.", parse_mode='HTML')
            return
        winning = await asyncio.to_thread(_pool_client.get_winning_balance, w3, 0, uv.address)
        if winning <= 0:
            await context.bot.send_message(q.message.chat_id, "No winning tokens to redeem for market 0.", parse_mode='HTML')
            return
        await q.edit_message_text(f"⏳ Redeeming {winning / 1e6:.2f} winning tokens…", parse_mode='HTML')
        tx_hash, err = await asyncio.to_thread(_pool_client.redeem, w3, uv, 0, winning)
        if err:
            await context.bot.send_message(q.message.chat_id, f"❌ Redeem failed: {err[:200]}", parse_mode='HTML')
        else:
            await context.bot.send_message(q.message.chat_id, f"✅ Redeem tx: <code>{tx_hash}</code>", parse_mode='HTML')
        return

    if "SET_" in q.data:
        context.user_data['stake'] = int(q.data.split("_")[1])
        await q.edit_message_text(f"✅ <b>CAPITAL LOADED: ${context.user_data['stake']}</b>")
    elif "ARB_" in q.data:
        target = ARBI_CACHE[int(q.data.split("_")[1])]
        calc = calculate_arbitrage_guaranteed(target['p_y'], target['p_n'], stake)
        msg = f"<b>PLAN:</b> {target['title']}\n\n✅ YES: ${calc['stake_yes']}\n❌ NO: ${calc['stake_no']}\n💰 ROI: {calc['roi']}%"
        kb = [[InlineKeyboardButton("🔥 EXECUTE", callback_data=f"EXE_{q.data.split('_')[1]}")]]
        await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    elif "EXE_" in q.data:
        user_id = q.from_user.id if q.from_user else None
        username = q.from_user.username if q.from_user else None
        uv = _wallet_manager.get_user_vault(user_id, username) if _wallet_manager and user_id is not None else vault
        client = get_clob_for_vault(uv) if uv and _wallet_manager else clob_client
        if client is None:
            await context.bot.send_message(q.message.chat_id, "⚠️ Set <b>WALLET_SEED</b> in .env and deposit to <b>My Wallet</b> to enable trading.", parse_mode='HTML')
            return
        target = ARBI_CACHE[int(q.data.split("_")[1])]
        calc = calculate_arbitrage_guaranteed(target['p_y'], target['p_n'], stake)
        results = []
        for (t_id, amt) in [(target['yes_id'], calc['stake_yes']), (target['no_id'], calc['stake_no'])]:
            try:
                order = MarketOrderArgs(token_id=str(t_id), amount=amt, side=BUY, price=0.99)
                setattr(order, 'size', amt); setattr(order, 'expiration', 0)
                signed = await asyncio.to_thread(client.create_order, order)
                resp = await asyncio.to_thread(client.post_order, signed, OrderType.FOK)
                results.append(resp.get("success", False))
            except: results.append(False)
        await context.bot.send_message(q.message.chat_id, "✅ <b>ARBITRAGE SECURED</b>" if all(results) else "⚠️ <b>EXECUTION ERROR</b>", parse_mode='HTML')

async def _error_handler(update, context):
    """Log Telegram/network errors without full traceback spam."""
    import logging
    err = context.error
    if err and "NetworkError" in type(err).__name__:
        logging.getLogger(__name__).warning("Telegram network error (will retry): %s", err)
        return
    logging.getLogger(__name__).exception("Update %s caused error: %s", update, err)

# --- Continuous scan (background): run arbitrage scan on an interval so opportunities are always ready
SCAN_INTERVAL_SEC = 45  # scan every 45 seconds; reduce (e.g. 25) for quicker updates

async def _background_scan(context):
    try:
        n = await scour_arbitrage()
        if n:
            print(f"[Scan] Opportunities: {len(ARBI_CACHE)}")
    except Exception as e:
        print(f"[Scan] Error: {e}")

if __name__ == "__main__":
    # Longer timeouts for slow networks / proxies (default 5s often causes "Timed out")
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
    app = ApplicationBuilder().token(os.getenv("TELEGRAM_BOT_TOKEN")).request(request).build()
    app.job_queue.run_repeating(_background_scan, interval=SCAN_INTERVAL_SEC, first=5)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_query))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), main_handler))
    app.add_error_handler(_error_handler)
    print("Hydra Online. Background scan every", SCAN_INTERVAL_SEC, "s.")
    app.run_polling()