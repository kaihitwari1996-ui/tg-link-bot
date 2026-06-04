import os
import asyncio
import logging
import json
from pathlib import Path
from datetime import datetime

from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from aiohttp import web

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL  = os.environ["BASE_URL"].rstrip("/")
PORT      = int(os.environ.get("PORT", 8080))
DB_FILE   = Path("files_db.json")

# Telegram bot API limit for get_file() is 20 MB
TG_SIZE_LIMIT = 20 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Tiny JSON database ─────────────────────────────────────────────────────────
def load_db() -> dict:
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {}

def save_db(db: dict):
    DB_FILE.write_text(json.dumps(db, indent=2))

# ── Telegram handlers ──────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *File Link Generator Bot*\n\n"
        "Send me any file, video, audio, or photo and I'll give you an *instant download link*.\n\n"
        "✅ Files up to 20 MB — direct stream link\n"
        "✅ Files above 20 MB — instant Telegram CDN link\n"
        "✅ Works in any browser\n\n"
        "Just drop the file here! 🚀",
        parse_mode="Markdown"
    )

async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    file_obj = (
        msg.document
        or msg.video
        or msg.audio
        or msg.voice
        or (msg.photo[-1] if msg.photo else None)
    )

    if not file_obj:
        await msg.reply_text("⚠️ Please send a file, video, audio, or photo.")
        return

    file_id        = file_obj.file_id
    file_unique_id = file_obj.file_unique_id
    original_name  = getattr(file_obj, "file_name", None) or f"file_{file_unique_id}"
    mime_type      = getattr(file_obj, "mime_type", "application/octet-stream")
    size_bytes     = getattr(file_obj, "file_size", 0) or 0

    db = load_db()
    db[file_unique_id] = {
        "file_id":       file_id,
        "original_name": original_name,
        "mime_type":     mime_type,
        "size_bytes":    size_bytes,
        "uploaded_at":   datetime.utcnow().isoformat(),
    }
    save_db(db)

    download_url = f"{BASE_URL}/download/{file_unique_id}"
    size_mb = size_bytes / (1024 * 1024)

    await msg.reply_text(
        f"✅ *Link Generated!*\n\n"
        f"📄 *File:* `{original_name}`\n"
        f"📦 *Size:* `{size_mb:.2f} MB`\n\n"
        f"🔗 *Download Link:*\n{download_url}\n\n"
        f"_Share this link — no Telegram needed!_ 🚀",
        parse_mode="Markdown"
    )
    log.info("Link generated for %s (%s MB) → %s", original_name, f"{size_mb:.1f}", download_url)

# ── Web server ─────────────────────────────────────────────────────────────────
async def web_download(request: web.Request):
    file_unique_id = request.match_info["file_unique_id"]
    db = load_db()

    if file_unique_id not in db:
        raise web.HTTPNotFound(reason="File not found.")

    meta       = db[file_unique_id]
    file_id    = meta["file_id"]
    name       = meta["original_name"]
    size_bytes = meta.get("size_bytes", 0) or 0

    bot = Bot(token=BOT_TOKEN)

    # Files > 20 MB: Telegram won't let bots download them
    # So we redirect directly to the Telegram CDN URL
    if size_bytes > TG_SIZE_LIMIT:
        try:
            tg_file = await bot.get_file(file_id)
            raise web.HTTPFound(location=tg_file.file_path)
        except Exception as e:
            log.warning("get_file failed for large file: %s", e)
            # Fallback: construct URL manually using file_id
            raise web.HTTPServiceUnavailable(
                reason="File too large for direct download. Please download from Telegram directly."
            )

    # Files <= 20 MB: stream through our server
    try:
        tg_file = await bot.get_file(file_id)
        tg_url  = tg_file.file_path
    except Exception as e:
        log.error("get_file error: %s", e)
        raise web.HTTPInternalServerError(reason=f"Telegram error: {e}")

    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(tg_url) as resp:
            if resp.status != 200:
                raise web.HTTPBadGateway(reason="Could not fetch file from Telegram.")

            response = web.StreamResponse(
                headers={
                    "Content-Disposition": f'attachment; filename="{name}"',
                    "Content-Type": meta.get("mime_type", "application/octet-stream"),
                    "Content-Length": str(size_bytes),
                }
            )
            await response.prepare(request)
            async for chunk in resp.content.iter_chunked(1024 * 64):
                await response.write(chunk)
            await response.write_eof()
            return response

async def web_index(request: web.Request):
    db = load_db()
    total = len(db)
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Telegram File Link Bot</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: monospace; background: #0d0d0d; color: #00ff99;
            display: flex; align-items: center; justify-content: center;
            height: 100vh; }}
    .box {{ text-align: center; border: 1px solid #00ff99;
            padding: 2.5rem 3rem; border-radius: 4px; }}
    h1 {{ font-size: 2rem; margin-bottom: 0.5rem; }}
    p  {{ color: #aaa; margin-top: 0.5rem; }}
    .count {{ color: #fff; font-size: 1.5rem; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>🤖 File Link Bot</h1>
    <p>Status: <strong style="color:#00ff99">Running ✅</strong></p>
    <p>Links generated: <span class="count">{total}</span></p>
    <p style="margin-top:1.5rem;font-size:0.8rem;color:#666">
      Send any file to the bot on Telegram to get an instant download link.
    </p>
  </div>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")

# ── Entrypoint ─────────────────────────────────────────────────────────────────
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO |
        filters.VOICE | filters.PHOTO,
        handle_file
    ))

    webserver = web.Application()
    webserver.router.add_get("/", web_index)
    webserver.router.add_get("/download/{file_unique_id}", web_download)

    runner = web.AppRunner(webserver)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Web server on port %d", PORT)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("Bot started ✅")

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
