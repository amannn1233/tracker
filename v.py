import os
import asyncio
import threading
import time
import requests
import json
from datetime import datetime
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ConversationHandler,
    MessageHandler, ContextTypes, filters
)

# ------------------ BOT CONFIGURATION ------------------
RPC_URL = "https://api.mainnet-beta.solana.com"
# The SPL token account address will be provided interactively.
token_account = None

# Threshold for an incoming token transfer (in token units)
THRESHOLD_TOKEN = 0.5  
# If no incoming transfer ≥ THRESHOLD_TOKEN occurs within this many seconds, trigger an alert (40 sec)
PAUSE_THRESHOLD = 40  
# Polling interval (in seconds)
POLL_INTERVAL = 5  
# Replace with your Telegram bot token (from BotFather)
TELEGRAM_BOT_TOKEN = "8057780965:AAFyjn9qRdax2kOiZzBZae6VkB1bbBppiIg"

# ------------------ GLOBAL VARIABLES ------------------
monitoring_active = False
monitor_thread = None
last_big_inflow_time = time.monotonic()
processed_signatures = set()
alert_sent = False
alert_chat_id = None
# This variable will store the human‑readable timestamp of the last qualifying inflow transaction.
last_inflow_timestamp = None

application = None
bot_loop = None

# Conversation state constant for receiving token account input
TOKEN_ADDRESS = 1

# ------------------ UTILITY FUNCTION ------------------
def make_request(method, params):
    """
    Sends a JSON-RPC request to the Solana RPC.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.post(RPC_URL, data=json.dumps(payload, default=str), headers=headers)
        return response.json()
    except Exception as e:
        print("Error in make_request:", e)
        return {}

# ------------------ MONITORING LOOP ------------------
def monitor_loop():
    """
    Polls the provided token account for new transactions.
    For each transaction, if a parsed SPL token "transfer" instruction is detected
    where the destination matches the monitored token account and the transfer amount,
    when normalized (using 6 decimals), is ≥ THRESHOLD_TOKEN, then the timer is reset
    and the block timestamp is recorded.
    If no qualifying inflow occurs within PAUSE_THRESHOLD seconds, an alert is sent.
    """
    global last_big_inflow_time, processed_signatures, alert_sent, monitoring_active
    global alert_chat_id, application, bot_loop, token_account, last_inflow_timestamp

    while monitoring_active:
        if token_account is None:
            time.sleep(POLL_INTERVAL)
            continue

        params = [token_account, {"limit": 5}]
        res = make_request("getSignaturesForAddress", params)
        entries = res.get("result", [])
        for entry in entries:
            sig = entry.get("signature")
            if sig in processed_signatures:
                continue

            tx_res = make_request("getParsedTransaction", [sig, {"encoding": "jsonParsed"}])
            tx = tx_res.get("result")
            if not tx:
                continue

            block_time = tx.get("blockTime")
            instructions = tx.get("transaction", {}).get("message", {}).get("instructions", [])
            for instr in instructions:
                if instr.get("program") == "spl-token" and "parsed" in instr:
                    parsed = instr["parsed"]
                    if parsed.get("type") != "transfer":
                        continue
                    info = parsed.get("info", {})
                    # For inflow, the destination must match the monitored token account.
                    if info.get("destination") != token_account:
                        continue
                    # First, try to get the human‑readable uiAmount.
                    token_amount = info.get("uiAmount")
                    if token_amount is None:
                        try:
                            # Use the raw "amount" and convert it using the known 6 decimals.
                            raw_amount = float(info.get("amount", "0"))
                            token_amount = raw_amount / (10 ** 6)
                        except Exception:
                            continue
                    if float(token_amount) >= THRESHOLD_TOKEN:
                        print(f"[{datetime.utcnow().isoformat()}] Big inflow detected: {token_amount} tokens, tx: {sig}")
                        last_big_inflow_time = time.monotonic()
                        alert_sent = False
                        # Capture block time (if available) as a human‑readable timestamp.
                        if block_time:
                            last_inflow_timestamp = datetime.fromtimestamp(block_time).strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            last_inflow_timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            processed_signatures.add(sig)
        
        now = time.monotonic()
        if now - last_big_inflow_time >= PAUSE_THRESHOLD and not alert_sent:
            message_text = (
                f"🚨 ALERT: No incoming SPL token transfer ≥ {THRESHOLD_TOKEN} units for {int(now - last_big_inflow_time)} seconds.\n"
                f"Last inflow timestamp: {last_inflow_timestamp if last_inflow_timestamp else 'N/A'}"
            )
            if alert_chat_id is not None and bot_loop is not None:
                future = asyncio.run_coroutine_threadsafe(
                    application.bot.send_message(chat_id=alert_chat_id, text=message_text),
                    bot_loop
                )
                try:
                    future.result()
                except Exception as exc:
                    print("Error sending alert:", exc)
                print(message_text)
            alert_sent = True
        
        time.sleep(POLL_INTERVAL)

# ------------------ TELEGRAM BOT HANDLERS ------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Please enter the SPL token account address you want to monitor:")
    return TOKEN_ADDRESS

async def set_token_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global token_account, monitoring_active, monitor_thread, last_big_inflow_time
    global processed_signatures, alert_chat_id, alert_sent, bot_loop
    token_account = update.message.text.strip()
    if not token_account:
        await update.message.reply_text("Invalid token address. Please try /start again.")
        return ConversationHandler.END
    alert_chat_id = update.effective_chat.id
    bot_loop = asyncio.get_running_loop()
    await update.message.reply_text(f"Monitoring started for token account:\n{token_account}")
    monitoring_active = True
    last_big_inflow_time = time.monotonic()
    processed_signatures = set()
    alert_sent = False
    if monitor_thread is None or not monitor_thread.is_alive():
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

# ------------------ MAIN FUNCTION ------------------
def main():
    global application
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            TOKEN_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_token_address)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("stop", stop_command))
    
    print("Telegram bot started. Use /start to begin and /stop to stop monitoring.", flush=True)
    
    # Use (or create) the current event loop without closing it so that run_polling() can use it.
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    loop.run_until_complete(application.bot.delete_webhook(drop_pending_updates=True))
    
    application.run_polling()

if __name__ == "__main__":
    main()
