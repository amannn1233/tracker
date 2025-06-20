import os
import asyncio
import threading
import time
import requests
import json
from datetime import datetime

# FastAPI & Uvicorn for uptime endpoint
from fastapi import FastAPI
import uvicorn

# Telegram imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
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
RPC_URL             = "https://api.mainnet-beta.solana.com"
POSSIBLE_WALLETS    = [
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa",
    "9B1fR2Z38ggjqmFuhYBEsa7fXaBR1dkC7BamixjmWZb4"
]
THRESHOLD_SOL       = 0.5
PAUSE_THRESHOLD     = 40
POLL_INTERVAL       = 5
TRANSACTION_LIMIT   = 10   # Debug mode: fetch only the last 10 transactions
TELEGRAM_BOT_TOKEN  = "7545022673:AAHUSh--IN95PVDATCeu6a0bHYd6ymuet_Y"

# ------------------ GLOBAL STATE ------------------
monitoring_active     = False
monitor_thread        = None
SELECTED_WALLET       = None
last_big_outflow_time = time.monotonic()
processed_signatures  = set()
alert_sent            = False
alert_chat_id         = None
application           = None
bot_loop              = None

# ------------------ RPC HELPER ------------------
def make_request(method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = requests.post(RPC_URL, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print("RPC error:", e)
        return {}

# ------------------ MONITOR LOOP (DEBUG MODE) ------------------
def monitor_loop():
    """
    Debug mode: Fetch the last 10 transactions from SELECTED_WALLET and print
    each transaction's signature and its SOL amount for every System transfer.
    """
    while monitoring_active and SELECTED_WALLET:
        res = make_request("getSignaturesForAddress", [SELECTED_WALLET, {"limit": TRANSACTION_LIMIT}])
        for entry in res.get("result", []):
            sig = entry["signature"]
            if sig in processed_signatures:
                continue

            tx = make_request("getParsedTransaction", [sig, {"encoding": "jsonParsed"}]).get("result")
            if tx:
                instructions = tx["transaction"]["message"]["instructions"]
                for instr in instructions:
                    if instr.get("program") == "system" and instr.get("parsed", {}).get("type") == "transfer":
                        info = instr["parsed"]["info"]
                        if info.get("source") == SELECTED_WALLET:
                            lam = float(info.get("lamports", 0))
                            sol = lam / 1e9
                            print(f"[{datetime.utcnow().isoformat()}] TX: {sig} sent {sol:.4f} SOL")
            processed_signatures.add(sig)
        time.sleep(POLL_INTERVAL)

# ------------------ BOT HANDLERS ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton(POSSIBLE_WALLETS[0], callback_data=POSSIBLE_WALLETS[0]),
            InlineKeyboardButton(POSSIBLE_WALLETS[1], callback_data=POSSIBLE_WALLETS[1]),
        ]
    ]
    reply = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select wallet to monitor (debug: showing last 10 txs & amounts):", reply_markup=reply)

async def wallet_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SELECTED_WALLET, monitoring_active, monitor_thread
    global last_big_outflow_time, processed_signatures, alert_sent, alert_chat_id, bot_loop

    query = update.callback_query
    await query.answer()

    SELECTED_WALLET = query.data
    alert_chat_id = query.message.chat.id
    bot_loop = asyncio.get_running_loop()

    monitoring_active = True
    last_big_outflow_time = time.monotonic()
    processed_signatures = set()
    alert_sent = False

    await query.edit_message_text(f"🟢 Now monitoring wallet:\n{SELECTED_WALLET}\n(Debug mode: displaying last 10 transactions and amounts)")

    if not monitor_thread or not monitor_thread.is_alive():
        monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        monitor_thread.start()

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_active
    monitoring_active = False
    await update.message.reply_text("🛑 Monitoring stopped.")

# ------------------ MAIN ------------------
def main():
    global application
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
