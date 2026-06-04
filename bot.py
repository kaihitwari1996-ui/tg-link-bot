import os
import asyncio
import logging
import json
import secrets
from pathlib import Path
from datetime import datetime
from pyrogram import Client
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from aiohttp import web

# ── Config ───────────────────────────────────────────────────────────────────
API_ID    = int(os.environ["API_ID"])
API_HASH  = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL  = os.environ["BASE_URL"].rstrip("/")
DATA_FILE = Path("links.json")
PORT      = int(os.environ.get("PORT", 8080))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Pyrogram client (for streaming only) ─────────────────────────────────────
pyro = Client(
    name="streamer",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

# ── Storage ───────────────────────────────────────────────────────────────────
def load_links():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {}

def save_links(links):
    DATA_FILE.write_text(json.dumps(links))

# ── PTB Handlers ──────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("Received /start from %s", update.effective_user.id)
    await update.message.reply_text(
        "👋 Hello! Send me any file, video, or photo.\n"
        "I'll give you a direct browser download link!\n\n"
        "✅ Works for files up to 2 GB"
    )

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    media = msg.document or msg.video or msg.audio or msg.voice or msg.video_note or msg.photo

    if not media:
        await msg.reply_text("⚠️ Please send a file or video.")
        return

    if isinstance(media, (list, tuple)):
        media = media[-1]  # photo is a list, take largest

    file_id   = media.file_id
    file_size = getattr(media, "file_size", 0) or 0
    file_name = getattr(media, "file_name", None) or f"file_{secrets.token_hex(4)}"

    token = secrets.token_urlsafe(12)
    links = load_links()
    links[token] = {
        "file_id":    file_id,
        "file_size":  file_size,
        "file_name":  file_name,
        "created":    datetime.utcnow().isoformat(),
    }
    save_links(links)

    url     = f"{BASE_URL}/download/{token}"
    size_mb = file_size / (1024 * 1024)

    await msg.reply_text(
        f"✅ Your download link is ready!\n\n"
        f"🔗 {url}\n\n"
        f"📦 Size: {size_mb:.1f} MB\n"
        f"📁 File: {file_name}\n\n"
        f"Open the link in any browser to download directly!"
    )

# ── Web server ────────────────────────────────────────────────────────────────
async def web_download(request: web.Request):
    token = request.match_info["token"]
    links = load_links()

    if token not in links:
        raise web.HTTPNotFound(reason="Link not found or expired.")

    entry     = links[token]
    file_id   = entry["file_id"]
    file_name = entry.get("file_name", "download")
    file_size = entry.get("file_size", 0)

    response = web.StreamResponse(
        headers={
            "Content-Disposition": f'attachment; filename="{file_name}"',
            "Content-Type": "application/octet-stream",
            **({"Content-Length": str(file_size)} if file_size else {}),
        }
    )
    await response.prepare(request)

    async for chunk in pyro.stream_media(file_id):
        await response.write(chunk)

    await response.write_eof()
    return response

async def web_health(request):
    return web.Response(text="✅ Bot is running!")

def make_web_app():
    web_app = web.Application()
    web_app.router.add_get("/download/{token}", web_download)
    web_app.router.add_get("/health", web_health)
    web_app.router.add_get("/", web_health)
    return web_app

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    # Start web server
    web_application = make_web_app()
    runner = web.AppRunner(web_application)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Web server running on port %s", PORT)

    # Start Pyrogram (for streaming)
    await pyro.start()
    log.info("Pyrogram streamer started!")

    # Start PTB bot (for receiving messages)
    ptb = Application.builder().token(BOT_TOKEN).build()
    ptb.add_handler(CommandHandler("start", start))
    ptb.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO |
        filters.VOICE | filters.VIDEO_NOTE | filters.PHOTO,
        handle_file
    ))

    await ptb.initialize()
    await ptb.start()
    await ptb.updater.start_polling(drop_pending_updates=True)
    log.info("PTB bot polling started!")

    # Keep running forever
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
