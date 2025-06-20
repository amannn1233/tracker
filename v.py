import os
import asyncio
import threading
import time
import requests
import json
from datetime import datetime
from fastapi import FastAPI
import uvicorn
from telegram import Update
from telegram.error import Conflict as TGConflict
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

# ─── CONFIG ────────────────────────────────────────────────────
RPC_URL            = "https://api.mainnet-beta.solana.com"
SOL_THRESHOLD      = 0.5
PAUSE_THRESHOLD    = 40
POLL_INTERVAL      = 5
TELEGRAM_BOT_TOKEN = "8057780965:AAFyjn9qRdax2kOiZzBZae6VkB1bbBppiIg"

# ─── STATE ─────────────────────────────────────────────────────
vault_address        = None
monitoring_active    = False
monitor_thread       = None
last_inflow_time     = time.monotonic()
processed_signatures = set()
alert_sent           = False
alert_chat_id        = None
application          = None
bot_loop             = None

VAULT_ADDRESS = 1  # Conversation step

# ─── JSON-RPC HELPER ───────────────────────────────────────────
def make_request(method, params):
    try:
        r = requests.post(RPC_URL, json={"jsonrpc":"2.0","id":1,"method":method,"params":params}, timeout=10)
        return r.json()
    except Exception as e:
        print("RPC error:", e)
        return {}

# ─── MONITOR LOOP ───────────────────────────────────────────
def monitor_loop():
    global last_inflow_time, processed_signatures, alert_sent
    global monitoring_active, alert_chat_id, application, bot_loop, vault_address

    while monitoring_active:
        if not vault_address:
            time.sleep(POLL_INTERVAL); continue

        sigs = make_request("getSignaturesForAddress", [vault_address, {"limit":10}]).get("result", [])
        for entry in sigs:
            sig = entry["signature"]
            if sig in processed_signatures:
                continue

            tx = make_request("getParsedTransaction", [sig, {"encoding":"jsonParsed"}]).get("result")
            if not tx:
                processed_signatures.add(sig); continue

            instrs = tx["transaction"]["message"]["instructions"]
            for instr in instrs:
                if instr.get("program")=="system" and "parsed" in instr:
                    p = instr["parsed"]
                    if p.get("type")=="transfer":
                        info = p["info"]
                        if info.get("destination")==vault_address:
                            sol = info.get("lamports",0)/1e9
                            if sol>=SOL_THRESHOLD:
                                print(f"[{datetime.utcnow().isoformat()}] SOL inflow {sol} SOL, tx:{sig}")
                                last_inflow_time = time.monotonic()
                                alert_sent = False

            processed_signatures.add(sig)

        # pause alert
        if time.monotonic() - last_inflow_time >= PAUSE_THRESHOLD and not alert_sent:
            text = f"🚨 ALERT: No ≥{SOL_THRESHOLD} SOL inflow to {vault_address} for {int(time.monotonic()-last_inflow_time)}s"
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

# ─── TELEGRAM HANDLERS ────────────────────────────────────────
async def start_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Send me the SOL-vault address (pool account) to monitor for ≥0.5 SOL buys:"
    )
    return VAULT_ADDRESS

async def set_vault_address(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    global vault_address, monitoring_active, monitor_thread
    global last_inflow_time, processed_signatures, alert_chat_id, alert_sent, bot_loop

    vault_address    = update.message.text.strip()
    alert_chat_id    = update.effective_chat.id
    bot_loop         = asyncio.get_running_loop()
    last_inflow_time = time.monotonic()
    processed_signatures.clear()
    alert_sent       = False

    await update.message.reply_text(f"Monitoring SOL inflows ≥{SOL_THRESHOLD} SOL to:\n{vault_address}")
    monitoring_active = True
    monitor_thread    = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    return ConversationHandler.END

async def stop_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global monitoring_active
    monitoring_active = False
    await update.message.reply_text("Monitoring stopped.")

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# ─── MAIN ENTRYPOINT ───────────────────────────────────────────
def main():
    global application

    # 1) Start FastAPI uptime server
    threading.Thread(target=run_web_server, daemon=True).start()

    # 2) Build Telegram app
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={ VAULT_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_vault_address)] },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(conv)
    application.add_handler(CommandHandler("stop", stop_command))

    print("Bot live—use /start to set the SOL-vault address.", flush=True)

    # 3) Clear any webhook/getUpdates conflicts
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    loop.run_until_complete(
        application.bot.delete_webhook(drop_pending_updates=True)
    )

    # 4) Run polling, auto-retry once on Conflict
    try:
        application.run_polling(drop_pending_updates=True)
    except TGConflict:
        # clear again and retry
        print("Conflict detected—clearing updates and retrying polling.", flush=True)
        loop.run_until_complete(
            application.bot.delete_webhook(drop_pending_updates=True)
        )
        application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
