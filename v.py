import os 
import asyncio
import json
import time
from datetime import datetime

import requests
try:
    import websockets
    USE_WEBSOCKETS = True
except ImportError:
    USE_WEBSOCKETS = False

from fastapi import FastAPI
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

fast_app = FastAPI()

@fast_app.get("/")
async def root():
    return {"status": "OK"}

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(fast_app, host="0.0.0.0", port=port)

RPC_HTTP_URL        = os.getenv("RPC_HTTP_URL", "https://api.mainnet-beta.solana.com")
RPC_WS_URL          = os.getenv("RPC_WS_URL", "wss://api.mainnet-beta.solana.com")
POSSIBLE_WALLETS    = [
    "9B1fR2Z38ggjqmFuhYBEsa7fXaBR1dkC7BamixjmWZb4",
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa"
]
THRESHOLD_SOL       = float(os.getenv("THRESHOLD_SOL", "0.5"))
PAUSE_THRESHOLD     = int(os.getenv("PAUSE_THRESHOLD", "2700"))  # 45 mins
POLL_INTERVAL       = float(os.getenv("POLL_INTERVAL", "1"))
UPDATE_INTERVAL     = int(os.getenv("UPDATE_INTERVAL", "180"))  # 3 minutes
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TOKEN")

application           = None
alert_chat_id         = None
monitor_task          = None
previous_balance      = None
last_big_outflow_time = None
last_update_time      = None
alert_sent            = False
monitor_pubkey        = None

async def fetch_balance(pubkey: str) -> int:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey]}
    try:
        r = requests.post(RPC_HTTP_URL, json=payload, timeout=10)
        return r.json().get("result", {}).get("value", 0)
    except Exception:
        return 0

async def notify(msg):
    global alert_chat_id, application
    if alert_chat_id and application:
        await application.bot.send_message(chat_id=alert_chat_id, text=msg)

async def subscribe_account_ws(pubkey: str):
    global previous_balance, last_big_outflow_time, alert_sent, last_update_time
    prev = await fetch_balance(pubkey)
    previous_balance = prev
    last_big_outflow_time = time.monotonic()
    last_update_time = time.monotonic()
    alert_sent = False
    try:
        async with websockets.connect(RPC_WS_URL) as ws:
            req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "accountSubscribe",
                "params": [pubkey, {"encoding": "base64"}]
            }
            await ws.send(json.dumps(req))
            await ws.recv()
            async for message in ws:
                msg = json.loads(message)
                lamports = msg.get("params", {}).get("result", {}).get("value", {}).get("lamports")
                if lamports is None:
                    continue
                new_balance = lamports
                diff = new_balance - previous_balance
                if diff < 0:
                    sol_out = -diff / 1e9
                    if sol_out >= THRESHOLD_SOL:
                        now = time.monotonic()
                        last_big_outflow_time = now
                        alert_sent = False
                        if now - last_update_time >= UPDATE_INTERVAL:
                            await notify(f"\u2705 {sol_out:.4f} SOL outflow detected from {pubkey}.")
                            last_update_time = now
                previous_balance = new_balance
                elapsed = time.monotonic() - last_big_outflow_time
                if elapsed >= PAUSE_THRESHOLD and not alert_sent:
                    await notify(f"\ud83d\udea8 ALERT: Wallet {pubkey} had no outgoing transfer \u2265{THRESHOLD_SOL} SOL for {int(elapsed)} seconds.")
                    alert_sent = True
    except Exception as e:
        print(f"Subscription error: {e}")
        await asyncio.sleep(5)
        asyncio.create_task(subscribe_account(pubkey))

async def subscribe_account(pubkey: str):
    global monitor_pubkey
    monitor_pubkey = pubkey
    if USE_WEBSOCKETS:
        await subscribe_account_ws(pubkey)
    else:
        print("WebSocket not available, please install websockets.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not POSSIBLE_WALLETS:
        await update.message.reply_text("No wallets configured.")
        return
    keyboard = [[InlineKeyboardButton(w, callback_data=w) for w in POSSIBLE_WALLETS]]
    reply = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select wallet to monitor:", reply_markup=reply)

async def wallet_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global alert_chat_id, monitor_task
    query = update.callback_query
    await query.answer()
    pubkey = query.data
    alert_chat_id = query.message.chat.id
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
    monitor_task = asyncio.create_task(subscribe_account(pubkey))
    await query.edit_message_text(f"\U0001F7E2 Now monitoring wallet:\n{pubkey}")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitor_task
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
    await update.message.reply_text("\u274C Monitoring stopped.")

def main():
    global application
    import threading
    threading.Thread(target=run_web_server, daemon=True).start()

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(wallet_selected))
    application.add_handler(CommandHandler("stop", stop))

    print("Bot is live.", flush=True)

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(application.bot.delete_webhook(drop_pending_updates=True))

    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
