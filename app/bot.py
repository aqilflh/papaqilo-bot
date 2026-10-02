"""Telegram handlers, the auth gate, and the message splitter.

The auth gate is the security boundary: only ids present in the `users` table
get any response at all. Everyone else is ignored silently, with their id logged
at WARNING so the owner can find it in the server log and run /adduser.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from telegram import CallbackQuery, Update
from telegram.constants import ChatAction
from telegram.error import Conflict, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import actions, db, geo, routes, shifts
from app.agent import Agent, run_and_store, user_zone
from app.log import get_logger, log_event
from app.recap import build_recap
from app.reminders import format_reminder
from app.scheduler import DEFAULT_RECAP_TIME, parse_hhmm
from app.shift_parser import PLACES, count_matching_lines, parse_schedule, resolve_manual_date

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

Jadwal shift:
/jadwal - cara kirim jadwal bulanan (tempel teksnya, aku yang baca)
/besok - jadwal besok
/jadwal_saya [hari] - jadwal N hari ke depan (default 7)
/jaga <dd/mm> <pagi|siang|malam|libur> [tempat] - tambah/edit manual
/hapus_jaga <dd/mm> - hapus satu jadwal
/hapus - konfirmasi penghapusan yang diusulkan lewat chat (jadwal/pengingat/catatan)
/pengingat on|off - nyalakan/matikan pengingat shift
/jam_eve HH:MM - jam pesan pengingat D-1 (default 20:00)
/jam <shift> HH:MM-HH:MM - jam shift (misal: /jam malam 22:00-06:00)
/sebelum <menit|off> - ingatkan beberapa menit sebelum shift mulai

Route:
Bilang aja "buat route pulang", lalu ikuti langkah bot: nama → kirim lokasi
titik lewat 📍 Telegram → "done" → titik B → simpan. 
/routes - daftar route kamu
/tambahan: "tambah alternatif route ...", "lihat route ...", "hapus route ..."
Pengingat rute: "ingetin route pulang jam 07:00", "cek reminder", "hapus reminder route pulang"

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
        application.add_handler(CommandHandler("jadwal", self.cmd_jadwal))
        application.add_handler(CommandHandler("besok", self.cmd_besok))
        application.add_handler(CommandHandler("simpan", self.cmd_simpan))
        application.add_handler(CommandHandler("batal", self.cmd_batal))
        application.add_handler(CommandHandler("hapus", self.cmd_hapus))
        application.add_handler(CommandHandler("routes", self.cmd_routes))
        application.add_handler(CommandHandler("jadwal_saya", self.cmd_jadwal_saya))
        application.add_handler(CommandHandler("jaga", self.cmd_jaga))
        application.add_handler(CommandHandler("hapus_jaga", self.cmd_hapus_jaga))
        application.add_handler(CommandHandler("pengingat", self.cmd_pengingat))
        application.add_handler(CommandHandler("jam_eve", self.cmd_jam_eve))
        application.add_handler(CommandHandler("jam", self.cmd_jam))
        application.add_handler(CommandHandler("sebelum", self.cmd_sebelum))
        application.add_handler(CommandHandler("usage", self.cmd_usage))
        application.add_handler(CommandHandler("forget", self.cmd_forget))
        application.add_handler(CommandHandler("adduser", self.cmd_adduser))
        application.add_handler(CommandHandler("removeuser", self.cmd_removeuser))
        application.add_handler(CommandHandler("users", self.cmd_users))
        application.add_handler(
            CallbackQueryHandler(self.on_shift_callback, pattern=r"^shift_(save|cancel):\d+")
        )
        application.add_handler(CallbackQueryHandler(self.on_any_callback))
        application.add_handler(MessageHandler(filters.LOCATION, self.on_location))
        # Must stay last: it catches every non-command text message.
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

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
                '"ingetin aku besok jam 9 pagi meeting".',
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
            await self._reply(update, f"Tidak ada pengingat aktif #{reminder_id} milik lo.")

    # ------------------------------------------------------------- shifts

    def _user_tz(self, user_id: int) -> Any:
        user = db.get_user(user_id) or {}
        return user_zone(user.get("timezone"))

    async def cmd_jadwal(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self._reply(
            update,
            "Tempel jadwal bulananmu (isi penuh, tanpa diedit), misalnya:\n\n"
            "OKTOBER\n"
            "1 kamis igd malam (+jihan)\n"
            "2 jumat: klinik siang\n"
            "3 sabtu libur\n"
            "5 senin igd pagi/prolanis\n\n"
            "Tiap baris: tanggal + hari + tempat + waktu. Aku tampilkan pratinjau "
            "dulu; balas /simpan untuk menyimpan atau /batal untuk membatalkan. "
            "Kalau bulan tidak ketahuan, aku minta kamu tulis nama bulannya.",
        )

    async def cmd_simpan(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        user_id = update.effective_user.id
        wizard = routes.get_wizard(user_id)
        if wizard is not None and wizard["phase"] == "confirm":
            try:
                reply_text, route_id = routes.save_wizard(user_id, wizard)
            except Exception as exc:
                log_event(
                    log,
                    logging.ERROR,
                    "route_save_failed",
                    user_id=user_id,
                    error=type(exc).__name__,
                    detail=repr(exc),
                )
                await self._reply(update, "Gagal menyimpan route. Coba mulai ulang ya.")
                return
            await self._reply(update, reply_text)
            # cmd_simpan runs outside the agent loop; record the confirmation
            # so the model learns the route exists on later turns.
            db.add_message(user_id, "assistant", reply_text)
            await self._route_eta_after_save(update, user_id, route_id)
            return
        parsed = shifts.pending_import(user_id)
        if parsed is None:
            await self._reply(
                update,
                "Tidak ada yang menunggu disimpan, atau sudah kedaluwarsa. "
                "/jadwal buat panduan import.",
            )
            return
        try:
            saved = shifts.apply_import(user_id, parsed)
            shifts.clear_pending(user_id)
        except Exception as exc:
            log_event(
                log,
                logging.ERROR,
                "shift_import_failed",
                user_id=user_id,
                error=type(exc).__name__,
                detail=repr(exc),
            )
            await self._reply(update, "Gagal menyimpan jadwal, tempel ulang lalu coba lagi.")
            return
        log_event(log, logging.INFO, "shift_import_saved", user_id=user_id, saved=saved)
        await self._reply(update, f"Jadwal tersimpan: {saved} shift. Cek /besok.")

    async def _route_eta_after_save(
        self, update: Update, user_id: int, route_id: int | None
    ) -> None:
        """Fetch and store a freshly saved route's ETA, then tell the user.

        Runs in plain code: the model is never allowed to invent travel time.
        Silently does nothing when the API key is unset or the call fails --
        the route itself is already saved.
        """
        api_key = getattr(self.config, "google_maps_api_key", "") or ""
        if not api_key or not route_id:
            return
        route = db.get_route(route_id, user_id)
        if route is None:
            return
        waypoints = [(w["latitude"], w["longitude"]) for w in db.list_waypoints(route_id, user_id)]
        try:
            metrics = await geo.compute_route(
                self.http,
                api_key,
                (route["origin_lat"], route["origin_lng"]),
                waypoints,
                (route["destination_lat"], route["destination_lng"]),
            )
        except geo.RouteError as exc:
            log_event(log, logging.WARNING, "route_eta_skipped", user_id=user_id, error=str(exc))
            return
        db.set_route_metrics(
            route_id, user_id, metrics["distance_meters"], metrics["duration_seconds"]
        )
        text = (
            f"⏱️ Estimasi: ±{routes.fmt_duration(metrics['duration_seconds'])} • "
            f"{routes.fmt_distance(metrics['distance_meters'])}"
        )
        await self._reply(update, text)
        db.add_message(user_id, "assistant", text)

    async def _route_eta_reply(self, user_id: int, group: dict[str, Any]) -> str | None:
        """ETA + distance for every alternative under a route group.

        Mirror of the ``route_eta`` tool, reused by the deterministic text
        interceptor so a question like "berapa lama route ini?" gets a real
        answer even when the model would rather guess from memory.
        """
        api_key = getattr(self.config, "google_maps_api_key", "") or ""
        if not api_key:
            return (
                "ETA belum aktif: GOOGLE_MAPS_API_KEY belum diisi di server. "
                "Route & koordinatnya tetap tersimpan."
            )
        routes_in_group = db.list_routes(group["id"], user_id)
        if not routes_in_group:
            return None

        lines = [f"⏱️ ETA {group['name']}:"]
        for route in routes_in_group:
            alt = route["name"] or "(utama)"
            if route["origin_lat"] is None or route["destination_lat"] is None:
                lines.append(f"- {alt}: koordinat belum lengkap.")
                continue
            waypoints = [
                (w["latitude"], w["longitude"]) for w in db.list_waypoints(route["id"], user_id)
            ]
            try:
                metrics = await geo.compute_route(
                    self.http,
                    api_key,
                    (route["origin_lat"], route["origin_lng"]),
                    waypoints,
                    (route["destination_lat"], route["destination_lng"]),
                )
            except geo.RouteError as exc:
                log_event(
                    log, logging.WARNING, "route_eta_skipped", user_id=user_id, error=str(exc)
                )
                lines.append(f"- {alt}: gagal menghitung ETA.")
                continue
            db.set_route_metrics(
                route["id"], user_id, metrics["distance_meters"], metrics["duration_seconds"]
            )
            lines.append(
                f"- {alt}: ±{routes.fmt_duration(metrics['duration_seconds'])} • "
                f"{routes.fmt_distance(metrics['distance_meters'])}"
            )
        return "\n".join(lines)

    async def cmd_routes(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self._reply(update, routes.list_routes_text(update.effective_user.id))

    async def cmd_hapus(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Confirm a deletion proposed via chat (the agent only stages)."""
        if not await self._guard(update):
            return
        user_id = update.effective_user.id
        entry = actions.pending(user_id)
        if not entry:
            await self._reply(update, "Tidak ada penghapusan yang menunggu konfirmasi.")
            return
        kind, targets = entry["kind"], entry["targets"]
        deleted = 0
        for key, _label in targets:
            if actions.apply(user_id, kind, key):
                deleted += 1
        actions.clear(user_id)
        noun = {
            "shift": "jadwal",
            "reminder": "pengingat",
            "note": "catatan",
            "route": "route",
            "route_group": "route",
        }.get(kind, "item")
        log_event(
            log,
            logging.INFO,
            f"{kind}_deleted",
            user_id=user_id,
            count=deleted,
            targets=len(targets),
        )
        await self._reply(update, f"Terhapus {deleted} {noun}.")
        if deleted != len(targets):
            await self._reply(
                update, "Beberapa tidak ada lagi, kalau-kalau bukan kamu yang menghapusnya."
            )

    async def cmd_batal(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        user_id = update.effective_user.id
        delete_staged = actions.pending(user_id) is not None
        import_pending = shifts.pending_import(user_id) is not None
        wizard_active = routes.get_wizard(user_id) is not None
        shifts.clear_pending(user_id)
        actions.clear(user_id)
        routes.clear_wizard(user_id)
        if delete_staged:
            await self._reply(update, "Penghapusan dibatalkan.")
        elif import_pending:
            await self._reply(update, "Import jadwal dibatalkan.")
        elif wizard_active:
            msg = "Pembuatan route dibatalkan."
            await self._reply(update, msg)
            db.add_message(user_id, "assistant", msg)
        else:
            await self._reply(update, "Tidak ada yang sedang berjalan.")

    async def cmd_besok(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        user_id = update.effective_user.id
        await self._reply(
            update, shifts.besok_text(user_id, self._user_tz(update.effective_user.id))
        )

    async def cmd_jadwal_saya(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        days = 7
        if ctx.args:
            try:
                days = int(ctx.args[0])
            except ValueError:
                await self._reply(update, "Jumlah hari harus angka, contoh: /jadwal_saya 14")
                return
        if not 1 <= days <= 90:
            await self._reply(update, "Jumlah hari antara 1 sampai 90.")
            return
        user_id = update.effective_user.id
        await self._reply(
            update,
            shifts.my_shifts_text(user_id, self._user_tz(update.effective_user.id), days),
        )

    async def cmd_jaga(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Manual add/edit: /jaga <dd/mm> <kind> [place]"""
        if not await self._guard(update):
            return
        args = ctx.args or []
        if len(args) < 2:
            await self._reply(update, "Pakai: /jaga <dd/mm> <pagi|siang|malam|libur> [tempat]")
            return
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", args[0])
        if match is None:
            await self._reply(update, "Tanggal harus format dd/mm, contoh: /jaga 22/10 malam igd")
            return
        day, month = int(match.group(1)), int(match.group(2))
        kind = args[1].lower()
        if kind not in db.SHIFT_KINDS:
            allowed = ", ".join(db.SHIFT_KINDS)
            await self._reply(update, f"Jenis shift harus salah satu dari: {allowed}")
            return
        place = args[2].lower() if len(args) > 2 else None
        if place is not None and place not in PLACES:
            await self._reply(
                update, f"Tempat harus salah satu dari: {', '.join(PLACES)} (atau kosongkan)."
            )
            return
        today = datetime.now(self._user_tz(update.effective_user.id)).date()
        resolve = resolve_manual_date(today, day, month)
        if resolve is None:
            await self._reply(update, "Tanggal tidak valid.")
            return
        user_id = update.effective_user.id
        db.upsert_shift(user_id, resolve.isoformat(), kind, place, raw=" ".join(args).upper())
        row = db.get_shift(user_id, resolve.isoformat())
        hours = db.get_shift_settings(user_id)["hours"]
        await self._reply(
            update,
            f"Jaga {shifts.fmt_day(resolve)}: {shifts.describe_shift(row, hours)}.",
        )

    async def cmd_hapus_jaga(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not ctx.args:
            await self._reply(update, "Pakai: /hapus_jaga <dd/mm>")
            return
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", ctx.args[0])
        if match is None:
            await self._reply(update, "Format tanggal salah, contoh: /hapus_jaga 22/10")
            return
        day, month = int(match.group(1)), int(match.group(2))
        today = datetime.now(self._user_tz(update.effective_user.id)).date()
        resolve = resolve_manual_date(today, day, month)
        if resolve is None:
            await self._reply(update, "Tanggal tidak valid.")
            return
        if db.delete_shift(update.effective_user.id, resolve.isoformat()):
            await self._reply(update, f"Jadwal {shifts.fmt_day(resolve)} dihapus.")
        else:
            await self._reply(update, f"Tidak ada jadwal {shifts.fmt_day(resolve)}.")

    async def cmd_pengingat(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not ctx.args or ctx.args[0].lower() not in ("on", "off"):
            settings = db.get_shift_settings(update.effective_user.id)
            state = "aktif" if settings["reminders_on"] else "mati"
            await self._reply(
                update,
                f"Pengingat shift sedang {state} (D-1 jam {settings['eve_time']}). "
                "Ubah: /pengingat on|off",
            )
            return
        enabled = ctx.args[0].lower() == "on"
        db.set_shift_reminders_on(update.effective_user.id, enabled)
        if enabled:
            eve = db.get_shift_settings(update.effective_user.id)["eve_time"]
            await self._reply(update, f"Pengingat shift aktif. Pesan D-1 dikirim tiap jam {eve}.")
        else:
            await self._reply(update, "Pengingat shift dimatikan.")

    async def cmd_jam_eve(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not ctx.args or parse_hhmm(ctx.args[0]) is None:
            await self._reply(update, "Format jam salah, contoh: /jam_eve 20:00")
            return
        db.set_shift_eve_time(update.effective_user.id, ctx.args[0])
        await self._reply(update, f"Pesan pengingat D-1 jadi jam {ctx.args[0]}.")

    async def cmd_jam(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        user_id = update.effective_user.id
        settings = db.get_shift_settings(user_id)
        if not ctx.args:
            if not settings["hours"]:
                await self._reply(update, "Jam shift belum diatur. Contoh: /jam malam 22:00-06:00")
                return
            lines = [f"{kind}: {span}" for kind, span in sorted(settings["hours"].items())]
            await self._reply(update, "Jam shift:\n" + "\n".join(lines))
            return
        kind = ctx.args[0].lower()
        if kind not in db.SHIFT_KINDS or kind == "libur":
            await self._reply(update, "Jenis shift harus pagi, siang, atau malam.")
            return
        if len(ctx.args) < 2:
            await self._reply(update, "Pakai: /jam <pagi|siang|malam> HH:MM-HH:MM")
            return
        match = re.fullmatch(r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2})", ctx.args[1])
        bad_span = (
            match is None
            or parse_hhmm(match.group(1)) is None
            or parse_hhmm(match.group(2)) is None
        )
        if bad_span:
            await self._reply(update, "Format jam salah, contoh: /jam siang 12:00-20:00")
            return
        hours = dict(settings["hours"])
        hours[kind] = ctx.args[1]
        db.set_shift_hours(user_id, hours)
        await self._reply(update, f"Jam {kind}: {ctx.args[1]}.")

    async def cmd_sebelum(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        user_id = update.effective_user.id
        if not ctx.args:
            value = db.get_shift_settings(user_id)["before_min"]
            state_line = (
                f"Pengingat sebelum mulai: {value} menit"
                if value
                else "Pengingat sebelum mulai: off"
            )
            await self._reply(update, state_line)
            return
        raw = ctx.args[0].lower()
        if raw == "off":
            db.set_shift_before_min(user_id, None)
            await self._reply(update, "Pengingat sebelum mulai dimatikan.")
            return
        try:
            minutes = int(raw)
        except ValueError:
            await self._reply(update, "Pakai: /sebelum <menit> atau /sebelum off")
            return
        if not 1 <= minutes <= 1440:
            await self._reply(update, "Menit harus antara 1 sampai 1440.")
            return
        db.set_shift_before_min(user_id, minutes)
        await self._reply(update, f"Pengingat {minutes} menit sebelum shift mulai.")

    async def on_shift_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Simpan / Batal on a pasted-schedule preview."""
        query = update.callback_query
        if query is None or query.data is None:
            return
        user_id = query.from_user.id
        data = query.data
        log_event(log, logging.INFO, "shift_callback_received", user_id=user_id, data=data)
        action = data.split(":")[0] if ":" in data else data
        if not self._allowed(user_id):
            await query.answer("Bukan untuk kamu.")
            return
        try:
            parts = data.split(":")
            owner_id = int(parts[1]) if len(parts) > 1 else user_id
        except ValueError:
            owner_id = user_id
        if owner_id != user_id:
            await query.answer("Tombol ini untuk sesi import yang lain.")
            return

        if action == "shift_cancel":
            shifts.clear_pending(user_id)
            await query.answer("Import jadwal dibatalkan.")
            await self._edit_safe(query, "Import jadwal dibatalkan.")
            return

        parsed = shifts.pending_import(user_id)
        if parsed is None:
            await query.answer("Sesi import kedaluwarsa, kirim ulang jadwalmu.")
            await self._edit_safe(query, "Import jadwal kedaluwarsa.")
            return
        try:
            saved = shifts.apply_import(user_id, parsed)
            shifts.clear_pending(user_id)
        except Exception as exc:
            log_event(
                log,
                logging.ERROR,
                "shift_import_failed",
                user_id=user_id,
                error=type(exc).__name__,
                detail=repr(exc),
            )
            await query.answer("Gagal menyimpan jadwal, coba kirim ulang.")
            return
        await query.answer(f"Tersimpan {saved} shift.")
        await self._edit_safe(query, f"Jadwal tersimpan: {saved} shift. Cek /besok.")
        log_event(log, logging.INFO, "shift_import_saved", user_id=user_id, saved=saved)

    async def on_any_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Safety net so no callback query leaves the button spinning forever.

        Logs data that matched nothing; owned by on_shift_callback when it
        starts with shift_. Anything else is an unknown button.
        """
        query = update.callback_query
        if query is None or query.data is None:
            return
        data = query.data
        log_event(log, logging.WARNING, "shift_callback_unmatched", data=data)
        if data.startswith("shift_"):
            return  # on_shift_callback already answered and edited this one.
        await query.answer("Tombol ini sudah tidak aktif.")

    async def _edit_safe(self, query: CallbackQuery, text: str) -> None:
        """Edit the preview message, tolerating a stale or deleted message."""
        try:
            await query.edit_message_text(text)
        except Exception as exc:
            log_event(
                log,
                logging.WARNING,
                "shift_callback_edit_failed",
                error=type(exc).__name__,
                detail=repr(exc),
            )

    async def _handle_shift_import(self, update: Update) -> None:
        """A plain text with >=5 shift-looking lines is an import, not chat."""
        user_id = update.effective_user.id
        tz = self._user_tz(user_id)
        today = datetime.now(tz).date()
        parsed = parse_schedule(update.effective_message.text or "", today)
        if parsed.ambiguous:
            await self._reply(
                update,
                "Bulan jadwal belum jelas untuk baris pertama. Tulis nama bulan di "
                "baris paling atas (misal: OKTOBER), lalu kirim ulang.",
            )
            return
        if not parsed.savable:
            await self._reply(
                update,
                "Tidak ada baris jadwal yang bisa diproses. Cek format dengan /jadwal.",
            )
            return
        shifts.stage_pending(user_id, parsed)
        chunks = split_message(shifts.render_preview(parsed))
        for chunk in chunks:
            await self.application.bot.send_message(chat_id=user_id, text=chunk)
        await self.application.bot.send_message(
            chat_id=user_id,
            text="Kirim /simpan untuk menyimpan, atau /batal untuk membatalkan.",
        )
        log_event(
            log,
            logging.INFO,
            "shift_import_preview",
            user_id=user_id,
            lines=len(parsed.entries),
            savable=len(parsed.savable),
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

        # An active route wizard owns the conversation: names, gates and the
        # save confirm are plain code, not LLM guessing.
        wizard = routes.get_wizard(user_id)
        if wizard is not None:
            reply_text, should_save = routes.handle_wizard_text(wizard, text)
            if should_save:
                saved, route_id = routes.save_wizard(user_id, wizard)
                await self._reply(update, saved)
                # The save confirmation runs outside the agent loop; record it
                # so the model learns the route exists on later turns.
                db.add_message(user_id, "assistant", saved)
                await self._route_eta_after_save(update, user_id, route_id)
            else:
                await self._reply(update, reply_text)
            return

        # "Buat route ..." / "tambah alternatif ..." must start a REAL wizard in
        # plain code: the model once said "sudah disiapkan, kirim lokasinya"
        # without ever calling the tool, so the next 📍 was rejected. Runs after
        # the wizard block so an active wizard still owns the conversation.
        action = routes.route_action_start(user_id, text)
        if action is not None:
            await self._reply(update, action)
            db.add_message(user_id, "assistant", action)
            return

        # "ingetin route pulang jam 07:00" / "hapus reminder route X" /
        # "cek reminder" — a per-route daily reminder, handled in plain code
        # like the wizard: the model never guesses the confirmation.
        reminder = routes.route_reminder_action(user_id, text)
        if reminder is not None:
            await self._reply(update, reminder)
            db.add_message(user_id, "assistant", reminder)
            return

        # Travel-time questions ("berapa lama...") are answered deterministically
        # too: the model once said "ETA belum aktif" from stale memory and kept
        # repeating it even after the API key was configured. ETA only ever comes
        # from Google via plain code; the meeting must run before status_reply
        # below because "berapa" is also a status word.
        eta_group = routes.eta_target(user_id, text)
        if eta_group is not None:
            reply = await self._route_eta_reply(user_id, eta_group)
            if reply is not None:
                await self._reply(update, reply)
                db.add_message(user_id, "assistant", reply)
                return

        # Route status questions are answered in plain code from the DB, never
        # by the LLM: the model was inventing "belum ada route" from memory.
        status = routes.status_reply(user_id, text)
        if status is not None:
            await self._reply(update, status)
            db.add_message(user_id, "assistant", status)
            return

        # A pasted schedule is data, not chat: parse it in plain code, never
        # send it to the LLM, and never store it in the chat history.
        if count_matching_lines(text) >= 5:
            try:
                await self._handle_shift_import(update)
            except Exception as exc:  # noqa: BLE001 - handler must never die
                log_event(
                    log,
                    logging.ERROR,
                    "shift_import_failed",
                    user_id=user_id,
                    error=type(exc).__name__,
                )
                await self._reply(
                    update, "Gagal membaca jadwal. Coba kirim ulang, atau cek /jadwal."
                )
            return

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
            result = await run_and_store(self.agent, user_id, text, owner=self._is_owner(user_id))
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

    async def on_location(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        user_id = update.effective_user.id
        wizard = routes.get_wizard(user_id)
        if wizard is None:
            await self._reply(
                update,
                'Belum ada pembuatan route yang aktif. Bilang "buat route ..." untuk mulai.',
            )
            return
        loc = update.effective_message.location
        if loc is None:
            return
        reply_text, _ = routes.record_location(wizard, loc.latitude, loc.longitude)
        log_event(
            log,
            logging.INFO,
            "route_location",
            user_id=user_id,
            sub=wizard.get("sub"),
            phase=wizard.get("phase"),
        )
        await self._reply(update, reply_text)

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
