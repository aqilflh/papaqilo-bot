"""Telegram handlers, the auth gate, and the message splitter.

The auth gate is the security boundary: only ids present in the `users` table
get any response at all. Everyone else is ignored silently, with their id logged
at WARNING so the owner can find it in the server log and run /adduser.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import Conflict, TelegramError
from telegram.ext import Application, ContextTypes, MessageHandler, CommandHandler, filters

from app import db
from app.agent import Agent, run_and_store, user_zone
from app.log import get_logger, log_event
from app.recap import build_recap
from app.reminders import format_reminder
from app.scheduler import DEFAULT_RECAP_TIME, parse_hhmm

log = get_logger("bot")

#: Telegram refuses a single message longer than this.
TELEGRAM_LIMIT = 4096
#: Leave headroom so a split never lands exactly on the limit.
CHUNK_SIZE = 3900

HELP_TEXT = """Hai! Aku asisten pribadimu.

Perintah:
/start - perkenalan
/help - daftar perintah ini
/city <kota> - set kota untuk data cuaca
/recap_on [HH:MM] - aktifkan ringkasan harian (default 06:30)
/recap_off - matikan ringkasan harian
/recap_now - kirim ringkasan sekarang juga
/notes [kata kunci] - lihat catatanmu
/usage - pemakaian hari ini
/forget - hapus riwayat chat (catatan tetap ada)

Pengingat:
Cukup ngobrol aja, misalnya "ingetin aku besok jam 9 pagi meeting" atau
"ingetin tiap hari jam 7 pagi dengan kopi". Aku akan sebut ulang tanggal dan
jamnya supaya kelihatan kalau aku salah.
/reminders - daftar pengingat aktif
/remind_del <nomor> - batalkan pengingat

Owner:
/adduser <id> <nama> - tambah pengguna
/removeuser <id> - hapus pengguna
/users - daftar pengguna

Selain perintah, bebas ngobrol aja. Aku bisa cek cuaca, baca berita,
nyimpen catatan, dan mengirim pengingat. Satu hal yang perlu kamu tahu: data
yang aku ambil dari internet itu diperlakukan sebagai data, bukan perintah,
jadi aku tidak akan ikut perintah yang nyemplil di dalam berita.
"""

OWNER_HELP = """Perintah owner:
/adduser <telegram_id> <nama> - menambah pengguna baru
/removeuser <telegram_id> - menghapus pengguna
/users - daftar semua pengguna

Cari id Telegram seseorang: mereka bisa chat @userinfobot.
"""


def split_message(text: str, limit: int = CHUNK_SIZE) -> list[str]:
    """Split a long reply on paragraph, then line, then word boundaries.

    The separators are tried in priority order and the first one that actually
    occurs in the window wins. Taking the furthest position of all of them
    instead would always land on a space, because a space is nearly always
    present, and prose would get cut mid-sentence.

    Always returns at least one chunk, even for empty input.
    """
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for separator in ("\n\n", "\n", " "):
            position = window.rfind(separator)
            if position > 0:
                cut = position
                break
        if cut <= 0:
            # A single unbreakable token longer than the limit: hard cut.
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


class Bot:
    """Wires handlers onto the application and owns the shared agent."""

    def __init__(self, config: Any, agent: Agent, http: Any) -> None:
        self.config = config
        self.agent = agent
        self.http = http
        self.application: Application | None = None
        self.last_backup: datetime | None = None

    # ------------------------------------------------------------- transport

    async def send_message(self, user_id: int, text: str) -> None:
        """Send, splitting as needed. Also used by the scheduler."""
        if self.application is None:
            raise RuntimeError("Bot.register() has not been called")
        for chunk in split_message(text):
            try:
                await self.application.bot.send_message(chat_id=user_id, text=chunk)
            except TelegramError as exc:
                log_event(
                    log, logging.ERROR, "send_failed", user_id=user_id, error=type(exc).__name__
                )
                raise

    async def _reply(self, update: Update, text: str) -> None:
        await self.send_message(update.effective_user.id, text)

    # ----------------------------------------------------------------- setup

    def register(self, application: Application) -> None:
        self.application = application
        application.add_handler(CommandHandler("start", self.cmd_start))
        application.add_handler(CommandHandler("help", self.cmd_help))
        application.add_handler(CommandHandler("city", self.cmd_city))
        application.add_handler(CommandHandler("recap_on", self.cmd_recap_on))
        application.add_handler(CommandHandler("recap_off", self.cmd_recap_off))
        application.add_handler(CommandHandler("recap_now", self.cmd_recap_now))
        application.add_handler(CommandHandler("notes", self.cmd_notes))
        application.add_handler(CommandHandler("reminders", self.cmd_reminders))
        application.add_handler(CommandHandler("remind_del", self.cmd_remind_del))
        application.add_handler(CommandHandler("usage", self.cmd_usage))
        application.add_handler(CommandHandler("forget", self.cmd_forget))
        application.add_handler(CommandHandler("adduser", self.cmd_adduser))
        application.add_handler(CommandHandler("removeuser", self.cmd_removeuser))
        application.add_handler(CommandHandler("users", self.cmd_users))
        # Must stay last: it catches every non-command text message.
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text)
        )

        application.add_error_handler(self.on_error)

    # -------------------------------------------------------------- gate

    def _is_owner(self, user_id: int) -> bool:
        return user_id == self.config.owner_telegram_id

    def _allowed(self, user_id: int) -> bool:
        return self._is_owner(user_id) or db.user_exists(user_id)

    async def _guard(self, update: Update) -> bool:
        """Return True if the update may proceed. Logs strangers at WARNING."""
        user = update.effective_user
        if user is None:
            return False
        if self._allowed(user.id):
            return True
        log_event(
            log,
            logging.WARNING,
            "rejected_unauthorized",
            telegram_id=user.id,
            username=user.username or "-",
        )
        return False

    # ------------------------------------------------------------ commands

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        user_id = update.effective_user.id
        log_event(log, logging.INFO, "user_started_bot", user_id=user_id)
        nama = (update.effective_user.first_name or "kamu").split()[0]
        await self._reply(update, f"Halo {nama}! Kirim /help untuk daftar perintah.")

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        text = HELP_TEXT
        if self._is_owner(update.effective_user.id):
            text += "\n" + OWNER_HELP
        await self._reply(update, text)

    async def cmd_city(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        args = ctx.args
        if not args:
            user = db.get_user(update.effective_user.id) or {}
            current = user.get("city") or "(belum diatur)"
            await self._reply(update, f"Kota sekarang: {current}\nPakai: /city <nama kota>")
            return
        city = " ".join(args).strip()
        db.set_city(update.effective_user.id, city)
        await self._reply(update, f"Kota disimpan: {city}")

    async def cmd_recap_on(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        raw = ctx.args[0] if ctx.args else DEFAULT_RECAP_TIME
        if parse_hhmm(raw) is None:
            await self._reply(update, f"Format jam salah. Contoh: /recap_on {DEFAULT_RECAP_TIME}")
            return
        db.set_schedule(update.effective_user.id, "daily_recap", raw, enabled=True)
        tz_name = (db.get_user(update.effective_user.id) or {}).get("timezone")
        await self._reply(
            update, f"Ringkasan harian aktif, tiap hari jam {raw} ({tz_name or 'Asia/Jakarta'})."
        )

    async def cmd_recap_off(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        db.set_schedule_enabled(update.effective_user.id, "daily_recap", False)
        await self._reply(update, "Ringkasan harian dimatikan.")

    async def cmd_recap_now(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        user_id = update.effective_user.id
        await update.effective_chat.send_action(ChatAction.TYPING)
        recap = await build_recap(self.agent, user_id, self.http)
        await self._reply(update, recap.text)
        if recap.tokens_in or recap.tokens_out:
            user = db.get_user(user_id) or {}
            day = db.local_day(user_zone(user.get("timezone")))
            db.record_usage(
                user_id, day, msg_count=0, tokens_in=recap.tokens_in, tokens_out=recap.tokens_out
            )

    async def cmd_notes(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        user_id = update.effective_user.id
        query = " ".join(ctx.args).strip() if ctx.args else None
        notes = db.list_notes(user_id, query=query, limit=20)
        if not notes:
            await self._reply(update, "Belum ada catatan.")
            return
        lines = [f"- (id {n['id']}) {n['content']}" for n in notes]
        await self._reply(update, f"{len(notes)} catatan:\n" + "\n".join(lines))

    async def cmd_reminders(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        user_id = update.effective_user.id
        reminders = db.list_reminders(user_id)
        if not reminders:
            await self._reply(
                update,
                "Belum ada pengingat aktif. Bilang aja, misalnya "
                "\"ingetin aku besok jam 9 pagi meeting\".",
            )
            return
        user = db.get_user(user_id) or {}
        tz = user_zone(user.get("timezone"))
        lines = [format_reminder(r, tz) for r in reminders]
        await self._reply(
            update,
            f"{len(reminders)} pengingat aktif:\n"
            + "\n".join(lines)
            + "\n\nBatalkan: /remind_del <nomor>",
        )

    async def cmd_remind_del(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not ctx.args:
            await self._reply(update, "Pakai: /remind_del <nomor>")
            return
        try:
            reminder_id = int(ctx.args[0])
        except ValueError:
            await self._reply(update, "Nomor pengingat harus angka.")
            return
        if db.disable_reminder(reminder_id, update.effective_user.id):
            await self._reply(update, f"Pengingat #{reminder_id} dimatikan.")
        else:
            await self._reply(
                update, f"Tidak ada pengingat aktif #{reminder_id} milik lo."
            )

    async def cmd_usage(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        user_id = update.effective_user.id
        user = db.get_user(user_id) or {}
        day = db.local_day(user_zone(user.get("timezone")))
        usage = db.get_usage(user_id, day)
        limit = int(user.get("daily_limit") or self.config.default_daily_limit)
        sisa = max(0, limit - usage["msg_count"])
        await self._reply(
            update,
            f"Pemakaian {day}\n"
            f"Pesan: {usage['msg_count']} / {limit} (sisa {sisa})\n"
            f"Token masuk: {usage['tokens_in']}\n"
            f"Token keluar: {usage['tokens_out']}",
        )

    async def cmd_forget(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        removed = db.delete_history(update.effective_user.id)
        await self._reply(
            update, f"{removed} pesan dihapus dari riwayat. Catatan tidak ikut terhapus."
        )

    # -------------------------------------------------------- owner only

    async def _require_owner(self, update: Update) -> bool:
        if self._is_owner(update.effective_user.id):
            return True
        await self._reply(update, "Perintah itu khusus owner.")
        return False

    async def cmd_adduser(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update) or not await self._require_owner(update):
            return
        if len(ctx.args) < 2:
            await self._reply(update, "Pakai: /adduser <telegram_id> <nama>")
            return
        raw_id, name = ctx.args[0], " ".join(ctx.args[1:])
        try:
            new_id = int(raw_id)
        except ValueError:
            await self._reply(update, f"'{raw_id}' bukan telegram id yang valid.")
            return
        db.upsert_user(new_id, name, role="user", daily_limit=self.config.default_daily_limit)
        await self._reply(update, f"{name} (id {new_id}) sudah bisa pakai bot ini.")

    async def cmd_removeuser(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update) or not await self._require_owner(update):
            return
        if not ctx.args:
            await self._reply(update, "Pakai: /removeuser <telegram_id>")
            return
        try:
            target = int(ctx.args[0])
        except ValueError:
            await self._reply(update, f"'{ctx.args[0]}' bukan telegram id yang valid.")
            return
        if target == self.config.owner_telegram_id:
            await self._reply(update, "Tidak bisa menghapus owner.")
            return
        if db.get_user(target) is None:
            await self._reply(update, "ID itu tidak ada di daftar pengguna.")
            return
        db.remove_user(target)
        await self._reply(update, f"Pengguna {target} dihapus, termasuk riwayatnya.")

    async def cmd_users(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update) or not await self._require_owner(update):
            return
        users = db.list_users()
        if not users:
            await self._reply(update, "Belum ada pengguna.")
            return
        lines = [
            f"- {u['name']} (id {u['id']}) peran={u['role']} kota={u.get('city') or '-'}"
            for u in users
        ]
        await self._reply(update, f"{len(users)} pengguna:\n" + "\n".join(lines))

    # ------------------------------------------------------------- messages

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        user_id = update.effective_user.id
        text = update.effective_message.text or ""

        user = db.get_user(user_id) or {}
        day = db.local_day(user_zone(user.get("timezone")))
        if db.usage_remaining(user_id, day) <= 0:
            log_event(log, logging.INFO, "daily_limit_hit", user_id=user_id, day=day)
            await self._reply(
                update, f"Kuotes harianmu habis ({user.get('daily_limit')} pesan). Reset besok ya."
            )
            return

        await update.effective_chat.send_action(ChatAction.TYPING)
        try:
            result = await run_and_store(
                self.agent, user_id, text, owner=self._is_owner(user_id)
            )
        except Exception as exc:  # noqa: BLE001 - a handler must never kill the process
            log_event(
                log, logging.ERROR, "agent_crashed", user_id=user_id, error=type(exc).__name__
            )
            await self._reply(update, "Ada yang error di sisiku. Coba lagi sebentar ya.")
            return

        # Log the outcome of every accepted message. Without this, a bot that is
        # up but silently not answering looks identical to a healthy one.
        log_event(
            log,
            logging.INFO,
            "message_handled",
            user_id=user_id,
            chars=len(text),
            tools=result.tool_calls,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
        )
        await self._reply(update, result.text)

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Swallow library-level errors so polling keeps running.

        `Conflict` means a second poller is running against the same token;
        Telegram kills this one. The process is alive but deaf, which looks
        exactly like "the bot ignores me" from the outside. The only cure is to
        stop the other poller, so say so loudly in the log and let
        `restart: unless-stopped` bring us back clean.
        """
        exc = getattr(ctx, "error", None)
        log_event(
            log,
            logging.ERROR,
            "telegram_error",
            error=type(exc).__name__ if exc else "unknown",
        )
        if isinstance(exc, Conflict):
            log_event(
                log,
                logging.CRITICAL,
                "polling_conflict",
                detail="A second bot instance is polling this token. "
                "This process is now deaf until it is restarted. "
                "Run `docker compose ps` and stop the other instance.",
            )
