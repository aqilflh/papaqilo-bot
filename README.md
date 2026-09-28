# Asisten Pribadi Telegram

Bot asisten pribadi multi-user yang berjalan di Docker (VPS Ubuntu), ditenagai API DeepSeek. Dipakai untuk: ringkasan harian (cuaca + berita), catatan, pengingat, dan ngobrol biasa.

## 1. Struktur

```
agent/
├── app/
│   ├── main.py        # entrypoint: config, db, build bot, start polling
│   ├── config.py      # parsing & validasi env (fail fast)
│   ├── log.py         # log terstruktur
│   ├── db.py          # koneksi, migrasi, akses data (semua di-filter user_id)
│   ├── bot.py         # handler Telegram, gate auth, perintah
│   ├── agent.py       # loop LLM + tool calling
│   ├── tools.py       # implementasi & skema tool
│   ├── recap.py       # kumpulkan data, minta LLM merangkum
│   ├── scheduler.py   # tick tiap 60 detik
│   ├── reminders.py   # logika pengingat (sekali/harian/mingguan)
│   ├── backup.py      # backup SQLite harian
│   └── tz.py          # resolusi timezone user
├── feeds.json         # daftar RSS sumber berita
├── requirements.txt   # runtime
├── requirements-dev.txt  # pytest + ruff (tidak masuk image)
├── Dockerfile
├── docker-compose.yml
├── .env.example       # contoh konfigurasi (nilai kosong)
├── docs/HOW_IT_WORKS.md  # penjelasan cara kerja (pemula)
└── data/              # agent.db + backup (gitignored, volume)
```

## 2. Cara menjalankan

### Prasyarat
- VPS dengan Docker + Docker Compose.
- Token bot dari [@BotFather](https://t.me/BotFather).
- API key dari [platform.deepseek.com](https://platform.deepseek.com).
- Telegram ID kamu (minta ke [@userinfobot](https://t.me/userinfobot)).

### Setup pertama
```bash
# masuk ke direktori proyek
cd /home/user/agent

# siapkan env
cp .env.example .env
nano .env    # isi sesuai bagian bawah

# pastikan APP_UID/APP_GID sama dengan pemilik folder ./data
id -u   # misal 1003 -> isi APP_UID=1003
id -g   # misal 1003 -> isi APP_GID=1003

# build + mulai
docker compose up -d --build

# lihat log (jangan pernah log secrets)
docker compose logs -f
```

`.env` wajib diisi minimal 4 key berikut, selain itu akan error saat start:

| Key | Isi |
|---|---|
| `DEEPSEEK_API_KEY` | API key DeepSeek |
| `DEEPSEEK_MODEL` | `deepseek-flash` (diuji) |
| `TELEGRAM_BOT_TOKEN` | token dari BotFather |
| `OWNER_TELEGRAM_ID` | ID Telegram owner (pemilik bot) |

Sisanya opsional dengan default aman. File `.env` **tidak boleh di-commit**; sudah masuk `.gitignore`.

### Memulai / membuka bot
```bash
docker compose up -d    # mulai
docker compose logs -f  # ikuti log
docker compose restart  # restart
docker compose down     # berhenti
```

## 3. Menambah pengguna

Bot hanya membalas orang yang terdaftar. Cara menambah — dari chat bot, sebagai owner:

```
/adduser <telegram_id> <nama>
```

Punya id orang lain? Minta orang itu chat [@userinfobot](https://t.me/userinfobot). Hapus dengan `/removeuser <telegram_id>`, daftar dengan `/users`.

Orang yang belum terdaftar tidak mendapat balasan, tapi id-nya muncul di log server sebagai warning.

## 4. Perintah

| Perintah | Fungsi |
|---|---|
| `/start`, `/help` | perkenalan & bantuan |
| `/city <kota>` | set kota default untuk cuaca |
| `/recap_on [HH:MM]` | aktifkan ringkasan harian (default 06:30) |
| `/recap_off` | matikan ringkasan harian |
| `/recap_now` | kirim ringkasan sekarang |
| `/notes [kata kunci]` | lihat / cari catatan |
| `/usage` | pemakaian pesan & token hari ini |
| `/forget` | hapus riwayat chat (catatan tetap) |
| `/reminders` | daftar pengingat aktif |
| `/remind_del <no>` | batalkan pengingat |
| `/adduser <id> <nama>` | (owner) tambah pengguna |
| `/removeuser <id>` | (owner) hapus pengguna |
| `/users` | (owner) daftar pengguna |

Selain perintah, bebas ngobrol:

```
cuaca hari ini gimana        -> get_weather pakai kota tersimpan
catat: beli susu             -> save_note
ingetin aku besok jam 9 meeting -> set_reminder (otomatis parse waktu)
hapus pengingat sarapan      -> delete_reminder
```

## 5. Update / upgrade

Tidak ada git remote; penyebaran lewat source di server:

```bash
cd /home/user/agent
# (aplikasikan perubahan kode di sini — biasanya lewat scp/tar dari local)
git status && git diff    # pastikan yang berubah memang yang dimaksud
git commit -am "deskripsi perubahan"
docker compose up -d --build
docker compose logs -f | head -20   # pastikan `bot_started` muncul
```

## 6. Restore backup

Backup otomatis tiap hari 03:00 WIB ke `data/backups/agent-YYYY-MM-DD.db` (simpan 7 hari terakhir).

```bash
# di server
docker compose down

# kembalikan pilihan backup ke data aktif (cadangkan dulu yang lama)
cp data/backups/agent-2026-09-28.db data/agent.db

docker compose up -d
```

Salin backup keluar server (cadangan kedua, mis. ke laptop):

```bash
scp user@server:/home/user/agent/data/backups/agent-*.db .
```

## 7. Acceptance checklist

Status terakhir tercatat (2026-09-28, 179 test hijau):

1. [x] `docker compose up -d --build` start & sehat; log `bot_started`, tanpa secrets.
2. [x] Owner kirim pesan dapat jawaban (live). Akun asing tidak dibalas & muncul warning — terverifikasi via kode + test, **belum dicoba live**.
3. [ ] `/adduser` menambah user kedua; catatan & riwayatnya terpisah dari owner — belum diuji live.
4. [x] "Cuaca hari ini?" memicu `get_weather` (live); "catat: beli susu" memicu `save_note`; `/notes` menampilkan.
5. [ ] `/recap_now` dan jadwal recap — terverifikasi via test + log, **belum dikonfirmasi live** pengiriman pesannya.
6. [ ] Batas harian bekerja — terverifikasi via test, belum diuji live.
7. [x] `docker stats` ~55 MB (limit 512 MB) — live.
8. [x] Ada berkas backup di `data/backups/` — live.

Status: [x] = terverifikasi, [ ] = belum diuji live.