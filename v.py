import os
import asyncio
import json
import time
from datetime import datetime

import requests  # for initial RPC calls (balance fetch)
import websockets

from fastapi import FastAPI
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ------------------ FASTAPI (UPTIME) SETUP ------------------
fast_app = FastAPI()

@fast_app.get("/")
async def root():
    return {"status": "OK"}

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(fast_app, host="0.0.0.0", port=port)

# ------------------ CONFIGURATION ------------------
RPC_HTTP_URL        = os.getenv("RPC_HTTP_URL", "https://api.mainnet-beta.solana.com")
RPC_WS_URL          = os.getenv("RPC_WS_URL", "wss://api.mainnet-beta.solana.com")
POSSIBLE_WALLETS    = json.loads(os.getenv("POSSIBLE_WALLETS_JSON", "[]"))  # e.g. '["wallet1","wallet2"]'
THRESHOLD_SOL       = float(os.getenv("THRESHOLD_SOL", "0.5"))
PAUSE_THRESHOLD     = int(os.getenv("PAUSE_THRESHOLD", "40"))
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")

# ------------------ GLOBAL STATE ------------------
application           = None
alert_chat_id         = None
monitor_task          = None
previous_balance      = None
last_big_outflow_time = None
alert_sent            = False

# ------------------ RPC HELPERS ------------------
async def fetch_balance(pubkey: str) -> int:
    # Returns lamports
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey]}
    try:
        r = requests.post(RPC_HTTP_URL, json=payload, timeout=10)
        result = r.json()
        return result.get("result", {}).get("value", 0)
    except Exception:
        return 0

# ------------------ MONITOR VIA WEBSOCKET ------------------
async def subscribe_account(pubkey: str):
    global previous_balance, last_big_outflow_time, alert_sent
    # Fetch initial balance
    prev = await fetch_balance(pubkey)
    previous_balance = prev
    last_big_outflow_time = time.monotonic()
    alert_sent = False
    try:
        async with websockets.connect(RPC_WS_URL) as ws:
            # Subscribe to account changes
            req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "accountSubscribe",
                "params": [pubkey, {"encoding": "base64"}]
            }
            await ws.send(json.dumps(req))
            # Read subscription response
            await ws.recv()
            # Listen for notifications
            async for message in ws:
                msg = json.loads(message)
                params = msg.get("params")
                if not params:
                    continue
                result = params.get("result")
                if not result:
                    continue
                value = result.get("value")
                if not value:
                    continue
                lamports = value.get("lamports")
                if lamports is None:
                    continue
                # Compare balance
                new_balance = lamports
                diff = new_balance - previous_balance
                # Outflow if diff < 0
                if diff < 0:
                    sol_out = -diff / 1e9
                    if sol_out >= THRESHOLD_SOL:
                        # Big outflow detected
                        last_big_outflow_time = time.monotonic()
                        alert_sent = False
                        if alert_chat_id and application:
                            text = f"[{datetime.utcnow().isoformat()}] {sol_out:.4f} SOL outflow detected from {pubkey}."
                            asyncio.create_task(application.bot.send_message(chat_id=alert_chat_id, text=text))
                previous_balance = new_balance
                # Check pause threshold
                elapsed = time.monotonic() - last_big_outflow_time
                if elapsed >= PAUSE_THRESHOLD and not alert_sent:
                    if alert_chat_id and application:
                        text = (f"🚨 No ≥{THRESHOLD_SOL} SOL outflow from {pubkey} in {int(elapsed)} seconds.")
                        asyncio.create_task(application.bot.send_message(chat_id=alert_chat_id, text=text))
                    alert_sent = True
    except Exception as e:
        print(f"Subscription error for {pubkey}: {e}")
        # Optionally retry after a delay
        await asyncio.sleep(5)
        # Restart subscription
        if monitor_task and not monitor_task.cancelled():
            asyncio.create_task(subscribe_account(pubkey))

# ------------------ BOT HANDLERS ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not POSSIBLE_WALLETS:
        await update.message.reply_text("No wallets configured for monitoring.")
        return
    keyboard = [
        [InlineKeyboardButton(w, callback_data=w) for w in POSSIBLE_WALLETS]
    ]
    reply = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select wallet to monitor:", reply_markup=reply)

async def wallet_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global alert_chat_id, monitor_task
    query = update.callback_query
    await query.answer()
    pubkey = query.data
    alert_chat_id = query.message.chat.id
    # Cancel existing monitor task
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
    # Start new subscription monitor
    monitor_task = asyncio.create_task(subscribe_account(pubkey))
    await query.edit_message_text(f"🟢 Now monitoring wallet:\n{pubkey}")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitor_task
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
    await update.message.reply_text("🛑 Monitoring stopped.")

# ------------------ MAIN ------------------
def main():
    global application
    # Start FastAPI uptime endpoint
    threading_loop = asyncio.new_event_loop()
    import threading
    threading.Thread(target=run_web_server, daemon=True).start()

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(wallet_selected))
    application.add_handler(CommandHandler("stop", stop))

    print("Bot is live. Use /start to pick a wallet, /stop to end.", flush=True)

    # Delete webhook if any
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(application.bot.delete_webhook(drop_pending_updates=True))

    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
