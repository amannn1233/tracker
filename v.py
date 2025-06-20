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
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ConversationHandler,
    MessageHandler, ContextTypes, filters
)

# ─── FASTAPI UPTIME ───────────────────────────────────────────
fast_app = FastAPI()

@fast_app.get("/")
async def root():
    return {"status": "OK"}

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(fast_app, host="0.0.0.0", port=port)

# ─── CONFIGURATION ────────────────────────────────────────────
RPC_URL            = "https://api.mainnet-beta.solana.com"
SOL_THRESHOLD      = 0.5     # 0.5 SOL threshold
PAUSE_THRESHOLD    = 40      # seconds without inflow = alert
POLL_INTERVAL      = 5       # seconds between checks

TELEGRAM_BOT_TOKEN = "8057780965:AAFyjn9qRdax2kOiZzBZae6VkB1bbBppiIg"

# ─── STATE ─────────────────────────────────────────────────────
vault_address        = None   # the SOL-vault (pool) address you’ll paste
monitoring_active    = False
monitor_thread       = None
last_inflow_time     = time.monotonic()
processed_signatures = set()
alert_sent           = False
alert_chat_id        = None
application          = None
bot_loop             = None

# conversation state
VAULT_ADDRESS = 1

# ─── JSON-RPC HELPER ───────────────────────────────────────────
def make_request(method, params):
    payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
    try:
        r = requests.post(RPC_URL, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print("RPC error:", e)
        return {}

# ─── MONITORING LOOP ───────────────────────────────────────────
def monitor_loop():
    global last_inflow_time, processed_signatures, alert_sent
    global monitoring_active, alert_chat_id, application, bot_loop, vault_address

    while monitoring_active:
        if not vault_address:
            time.sleep(POLL_INTERVAL)
            continue

        res = make_request("getSignaturesForAddress", [vault_address, {"limit": 10}])
        sigs = res.get("result", [])

        for entry in sigs:
            sig = entry.get("signature")
            if sig in processed_signatures:
                continue

            tx = make_request("getParsedTransaction", [sig, {"encoding":"jsonParsed"}]).get("result")
            if not tx:
                processed_signatures.add(sig)
                continue

            instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
            for instr in instrs:
                if instr.get("program") == "system" and "parsed" in instr:
                    parsed = instr["parsed"]
                    if parsed.get("type") == "transfer":
                        info = parsed.get("info", {})
                        if info.get("destination") == vault_address:
                            lam = info.get("lamports", 0)
                            sol = lam / 1e9
                            if sol >= SOL_THRESHOLD:
                                print(f"[{datetime.utcnow().isoformat()}] "
                                      f"SOL inflow detected: {sol} SOL, tx: {sig}")
                                last_inflow_time = time.monotonic()
                                alert_sent = False

            processed_signatures.add(sig)

        now = time.monotonic()
        if now - last_inflow_time >= PAUSE_THRESHOLD and not alert_sent:
            text = (f"🚨 ALERT: No SOL inflow ≥ {SOL_THRESHOLD} SOL to {vault_address} "
                    f"for {int(now-last_inflow_time)}s")
            if alert_chat_id and bot_loop:
                fut = asyncio.run_coroutine_threadsafe(
                    application.bot.send_message(chat_id=alert_chat_id, text=text),
                    bot_loop
                )
                try: fut.result()
                except Exception as e: print("Alert send error:", e)
            print(text)
            alert_sent = True

        time.sleep(POLL_INTERVAL)

# ─── TELEGRAM HANDLERS ─────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Send me the SOL-vault address (the pool account) you want to monitor for ≥0.5 SOL buys:"
    )
    return VAULT_ADDRESS

async def set_vault_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global vault_address, monitoring_active, monitor_thread
    global last_inflow_time, processed_signatures, alert_chat_id, alert_sent, bot_loop

    vault_address = update.message.text.strip()
    alert_chat_id = update.effective_chat.id
    bot_loop      = asyncio.get_running_loop()

    await update.message.reply_text(
        f"Monitoring SOL inflows ≥{SOL_THRESHOLD} SOL to:\n{vault_address}"
    )

    monitoring_active    = True
    last_inflow_time     = time.monotonic()
    processed_signatures = set()
    alert_sent           = False

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    return ConversationHandler.END

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_active
    monitoring_active = False
    await update.message.reply_text("Monitoring stopped.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# ─── MAIN ENTRYPOINT ────────────────────────────────────────────
def main():
    global application

    # start FastAPI uptime endpoint
    threading.Thread(target=run_web_server, daemon=True).start()

    # build and start the Telegram bot
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={ VAULT_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_vault_address)] },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(conv)
    application.add_handler(CommandHandler("stop", stop_command))

    print("Bot is live. Use /start to set the SOL-vault address.", flush=True)

    # ─── ensure no webhook/getUpdates conflict ────────────────
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    loop.run_until_complete(
        application.bot.delete_webhook(drop_pending_updates=True)
    )

    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
