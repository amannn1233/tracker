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

# ------------------ FASTAPI (UPTIME) SETUP ------------------
fast_app = FastAPI()

@fast_app.get("/")
async def root():
    return {"status": "OK"}

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(fast_app, host="0.0.0.0", port=port)

# ------------------ CONFIGURATION ------------------
RPC_HTTP_URL = os.getenv("RPC_HTTP_URL", "https://api.mainnet-beta.solana.com")
RPC_WS_URL = os.getenv("RPC_WS_URL", "wss://api.mainnet-beta.solana.com")
POSSIBLE_WALLETS = [
    "9B1fR2Z38ggjqmFuhYBEsa7fXaBR1dkC7BamixjmWZb4",
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa"
]
THRESHOLD_SOL = float(os.getenv("THRESHOLD_SOL", "0.5"))
PAUSE_THRESHOLD = int(os.getenv("PAUSE_THRESHOLD", "40"))
POLL_SIGNATURES_INTERVAL = int(os.getenv("POLL_SIGNATURES_INTERVAL", "20"))  # seconds
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7545022673:AAHUSh--IN95PVDATCeu6a0bHYd6ymuet_Y")

application = None
alert_chat_id = None
monitor_task = None
monitor_pubkey = None
last_big_outflow_time = None
alert_sent = False
processed_sigs = set()

async def send_message(text: str):
    global alert_chat_id, application
    if alert_chat_id and application:
        try:
            await application.bot.send_message(chat_id=alert_chat_id, text=text)
        except Exception:
            pass

async def fetch_parsed_transaction(sig: str):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "jsonParsed"}]
    }
    try:
        r = requests.post(RPC_HTTP_URL, json=payload, timeout=10)
        return r.json().get("result")
    except Exception:
        return None

async def poll_signatures(pubkey: str):
    global last_big_outflow_time, alert_sent, processed_sigs
    # Periodically fetch recent signatures to catch missed transfers
    while True:
        try:
            # Fetch up to 1000 recent signatures
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [pubkey, {"limit": 1000}]
            }
            r = requests.post(RPC_HTTP_URL, json=payload, timeout=10).json()
            sigs = r.get("result", [])
            for entry in sigs:
                sig = entry.get("signature")
                if not sig or sig in processed_sigs:
                    continue
                processed_sigs.add(sig)
                tx = await fetch_parsed_transaction(sig)
                if not tx:
                    continue
                # Check outgoing sol transfers in instructions and innerInstructions
                # Top-level instructions
                msg = tx.get("transaction", {}).get("message", {})
                instrs = msg.get("instructions", [])
                await _process_instructions(instrs, pubkey)
                # Inner instructions
                meta = tx.get("meta", {})
                inner = meta.get("innerInstructions", [])
                for inner_set in inner:
                    instrs2 = inner_set.get("instructions", [])
                    await _process_instructions(instrs2, pubkey)
            # After polling, wait
        except Exception as e:
            await send_message(f"⚠️ Polling error for {pubkey}: {e}")
        await asyncio.sleep(POLL_SIGNATURES_INTERVAL)

async def _process_instructions(instructions, pubkey: str):
    global last_big_outflow_time, alert_sent
    for instr in instructions:
        if instr.get("program") == "system" and instr.get("parsed", {}).get("type") == "transfer":
            info = instr.get("parsed", {}).get("info", {})
            if info.get("source") == pubkey:
                try:
                    lamports = int(info.get("lamports", 0))
                except:
                    lamports = 0
                sol = lamports / 1e9
                if sol >= THRESHOLD_SOL:
                    last_big_outflow_time = time.monotonic()
                    alert_sent = False
                    await send_message(f"✅ Transfer found from {pubkey}: {sol:.4f} SOL. Monitoring continues...")

async def check_elapsed_loop(pubkey: str):
    global last_big_outflow_time, alert_sent
    while True:
        await asyncio.sleep(PAUSE_THRESHOLD)
        if last_big_outflow_time is None:
            continue
        elapsed = time.monotonic() - last_big_outflow_time
        if elapsed >= PAUSE_THRESHOLD:
            await send_message(f"🚨 ALERT: Wallet {pubkey} had no outgoing transfer ≥{THRESHOLD_SOL} SOL for {int(elapsed)} seconds.")
            alert_sent = True

async def subscribe_account_ws(pubkey: str):
    global last_big_outflow_time, alert_sent, processed_sigs
    last_big_outflow_time = time.monotonic()
    alert_sent = False
    processed_sigs.clear()
    await send_message(f"🔍 Monitoring started for {pubkey}. Watching for transfers ≥{THRESHOLD_SOL} SOL...")
    # Start elapsed checker and signature polling in background
    asyncio.create_task(check_elapsed_loop(pubkey))
    asyncio.create_task(poll_signatures(pubkey))
    try:
        async with websockets.connect(RPC_WS_URL) as ws:
            req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [{"mentions": [pubkey]}, {"commitment": "confirmed"}]
            }
            await ws.send(json.dumps(req))
            await ws.recv()
            async for message in ws:
                msg = json.loads(message)
                sig = msg.get("params", {}).get("result", {}).get("signature")
                if not sig:
                    continue
                if sig in processed_sigs:
                    continue
                processed_sigs.add(sig)
                tx = await fetch_parsed_transaction(sig)
                if not tx:
                    continue
                # process instructions and innerInstructions
                msg_obj = tx.get("transaction", {}).get("message", {})
                instrs = msg_obj.get("instructions", [])
                await _process_instructions(instrs, pubkey)
                meta = tx.get("meta", {})
                inner = meta.get("innerInstructions", [])
                for inner_set in inner:
                    instrs2 = inner_set.get("instructions", [])
                    await _process_instructions(instrs2, pubkey)
    except Exception as e:
        await send_message(f"⚠️ Subscription error for {pubkey}: {e}. Restarting monitor.")
        await asyncio.sleep(5)
        if monitor_task and not monitor_task.cancelled():
            asyncio.create_task(subscribe_account(pubkey))

async def subscribe_account(pubkey: str):
    global monitor_pubkey
    monitor_pubkey = pubkey
    if USE_WEBSOCKETS:
        await subscribe_account_ws(pubkey)
    else:
        await send_message("❌ websockets module not installed. Cannot monitor in real time.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not POSSIBLE_WALLETS:
        await update.message.reply_text("No wallets configured for monitoring.")
        return
    keyboard = [[InlineKeyboardButton(w, callback_data=w) for w in POSSIBLE_WALLETS]]
    reply = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select wallet to monitor:", reply_markup=reply)

async def wallet_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global alert_chat_id, monitor_task, last_big_outflow_time, alert_sent, processed_sigs
    query = update.callback_query
    await query.answer()
    pubkey = query.data
    alert_chat_id = query.message.chat.id
    last_big_outflow_time = time.monotonic()
    alert_sent = False
    processed_sigs.clear()
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
    monitor_task = asyncio.create_task(subscribe_account(pubkey))
    await query.edit_message_text(f"🟢 Now monitoring wallet:\n{pubkey}")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitor_task
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
    await update.message.reply_text("🛑 Monitoring stopped.")


def main():
    global application
    import threading
    threading.Thread(target=run_web_server, daemon=True).start()
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(wallet_selected))
    application.add_handler(CommandHandler("stop", stop))
    print("Bot is live. Use /start to pick a wallet, /stop to end.", flush=True)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(application.bot.delete_webhook(drop_pending_updates=True))
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
