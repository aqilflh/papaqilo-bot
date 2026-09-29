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
│   ├── actions.py     # penghapusan berkonfirmasi (/hapus)
│   ├── shifts.py      # jadwal jaga, impor, teks jadwal, pengingat shift
│   ├── routes.py      # wizard route: nama -> 📍 lokasi -> simpan
│   ├── geo.py         # Google Routes API (ETA + jarak)
│   ├── shift_parser.py# parse teks jadwal jaga
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

`GOOGLE_MAPS_API_KEY` (opsional) dipakai untuk menghitung ETA + jarak lewat Google
Routes API. Aktifkan "Routes API" di Google Cloud Console. Tanpa key, route tetap
tersimpan dan tampil — hanya ETA yang dilewati.

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
| `/besok` | jadwal jaga besok |
| `/jadwal_saya` | seluruh jadwal jaga tersimpan |
| `/jam <shift> HH:MM-HH:MM` | atur jam tiap shift (malam, pagi, siang) |
| `/sebelum <menit\|off>` | pengingat H-0 beberapa menit sebelum jam mulai |
| `/pengingat on\|off` | aktifkan/matikan pengingat shift |
| `/jam_eve HH:MM` | jam pengingat D-1 (default 20:00) |
| `/hapus_jaga <dd/mm>` | hapus satu jadwal jaga (langsung) |
| `/simpan` | simpan jadwal yang sudah dipreview |
| `/batal` | batal simpan jadwal / batal penghapusan |
| `/hapus` | konfirmasi penghapusan yang diusulkan lewat chat |
| `/routes` | daftar route tersimpan |
| `/adduser <id> <nama>` | (owner) tambah pengguna |
| `/removeuser <id>` | (owner) hapus pengguna |
| `/users` | (owner) daftar pengguna |

Selain perintah, bebas ngobrol:

```
cuaca hari ini gimana        -> get_weather pakai kota tersimpan
catat: beli susu             -> save_note
ingetin aku besok jam 9 meeting -> set_reminder (otomatis parse waktu)
hapus pengingat sarapan      -> list lalu delete_reminder
kamis ada jadwal apa         -> get_schedule (jadwal shift)
tempel jadwal jaga           -> preview per minggu, lalu /simpan
hapus semua jadwal jaga      -> usulkan daftar, minta /hapus
hapus semua catatan          -> usulkan daftar, minta /hapus
hapus semua pengingat        -> usulkan daftar, minta /hapus
buat route pulang            -> wizard: nama -> 📍 A -> waypoint -> done -> 📍 B -> /simpan
tambah alternatif route pulang via TB Simatupang -> wizard alternatif
lihat route gue              -> list_routes (atau /routes)
cek route pulang via gempol  -> detail: A, tiap waypoint, B (+ ETA kalau ada)
ETA route pulang             -> route_eta: hitung & simpan ETA + jarak (Google Routes API)
hapus route via Pasar Minggu -> delete_route (group besar minta /hapus)
```

Saat route baru disimpan, bot otomatis menghitung ETA & jaraknya (kalau
`GOOGLE_MAPS_API_KEY` diset) dan menyimpannya, lalu menampilkannya di
`/routes` dan `lihat route X`. Angka ETA **hanya** berasal dari Google Routes
API — model tidak pernah mengarang waktu tempuh.

Penghapusan lewat chat selalu **berkonfirmasi**: tool hanya menyiapkan daftar,
lalu bot minta `/hapus` untuk menjalankan (atau `/batal` untuk batal). Satu
target yang jelas (id catatan/pengingat, satu jadwal) tetap dihapus langsung.

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

Status terakhir tercatat (2026-09-29, 288 test hijau):

1. [x] `docker compose up -d --build` start & sehat; log `bot_started`, tanpa secrets.
2. [x] Owner kirim pesan dapat jawaban (live). Akun asing tidak dibalas & muncul warning — terverifikasi via kode + test, **belum dicoba live**.
3. [x] `/adduser` menambah user kedua; catatan & riwayatnya terpisah dari owner — live.
4. [x] "Cuaca hari ini?" memicu `get_weather` (live); "catat: beli susu" memicu `save_note`; `/notes` menampilkan.
5. [x] `/recap_now` terkirim; jadwal recap tervalidasi test + log — live.
6. [ ] Batas harian bekerja — terverifikasi via test, belum diuji live.
7. [x] `docker stats` ~55 MB (limit 512 MB) — live.
8. [x] Ada berkas backup di `data/backups/` — live.

Phase 9 (shift jaga):

9. [x] Tempel jadwal → preview per minggu → `/simpan` menyimpan (live: 10 shift Oktober tersimpan). Alur tombol inline diganti perintah `/simpan` & `/batal` (callback tidak pernah diterima; handler tombol lama dipertahankan sebagai cadangan).
10. [x] Diperlukan bulan ambigu → bot tanya, tidak menebak (manual).
11. [x] `/besok` dan `/jadwal_saya` menampilkan jadwal tersimpan (manual).
12. [x] Reminder D-1 terkirim live (eve_time diinjeksi 07:00 untuk shift besok); libur → "Besok libur.", sebelum mulai, dan peringatan "Jadwal tersimpan sampai ..." via test.
13. [x] `/jaga`, `/hapus_jaga`, `/jam`, `/sebelum`, `/pengingat`, `/jam_eve` memakai & menulis jam shift masing-masing user (manual).
14. [x] Tanya jadwal lewat chat ("kamis ada jadwal apa") dijawab via `get_schedule` (manual).
15. [x] Hapus berkonfirmasi via chat + `/hapus` untuk jadwal/pengingat/catatan (manual).

Phase 9 selesai & terverifikasi manual.

Phase 10 (route):

16. [x] "Buat route ..." → wizard nama → 📍 titik A → waypoint 📍 → "done" → 📍 titik B → `/simpan` tersimpan (live: "Pulang via Gempol", 8 waypoint).
17. [ ] "Tambah alternatif" untuk group yang sama menyimpan label alternatif — didukung kode + test, belum diuji live.
18. [x] `/routes`, "lihat route X", "rename route", "hapus route" berfungsi dan ter-scope per user (list/show/hapus live; rename via test).
19. [x] ETA + jarak via Google Routes API (live: route 8 waypoint → 14 menit, 4,6 km), dihitung saat `/simpan` dan lewat "ETA route ...". Tanpa key, route tetap jalan.

Status: [x] = terverifikasi, [ ] = belum diuji live.