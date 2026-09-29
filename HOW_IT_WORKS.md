# Cara Kerja Bot Ini

Penjelasan singkat untuk orang yang baru kenal Python dan LLM agent tapi sudah
paham web development. Tidak butuh baca seluruh kode; bagian ini cukup buat
memahami alur dan pola yang dipakai.

## Gambaran besar

```
Telegram ── pesan ──▶ Bot (app/bot.py)
        ◀─ balasan ──
            │
            ▼  (kalau teks biasa, bukan perintah)
        Agent (app/agent.py)
            │  system prompt + riwayat + waktu sekarang + pesan user
            ▼
        DeepSeek API
            │  jawaban (atau permintaan tool: get_weather, save_note, ...)
            ▼
        Tools (app/tools.py) ──▶ Open-Meteo / RSS feeds / SQLite
```

Bagian paling asing dari stack ini adalah **loop tool calling** di dalam
`app/agent.py`. Sisanya (bot, DB, scheduler) adalah pola yang familiar: event
handler, query berparameter, cron job.

## 1. Loop agent (bagaimana LLM "memakai tool")

`agent.py` bukan sekadar kirim pesan → terima jawaban. Dia menjalankan loop:

1. Kirim `messages` ke DeepSeek. Sekali panggilan, isinya:
   `system prompt` → riwayat percakapan → catatan waktu sekarang → pesan user.
2. Kalau jawabannya hanya teks biasa → selesai, balas user.
3. Kalau jawabannya berisi `tool_calls` (mis. `get_weather` dengan argumen) →
   **jalankan tool itu di kode**, tempel hasilnya ke daftar pesan, lalu
   **kirim lagi ke DeepSeek** supaya model bisa menyusun jawaban akhirnya.
4. Ulangi. Model bisa meminta beberapa tool; hasil tiap tool dibatasi ~6000
   karakter supaya tidak membanjiri context window.
5. Ada **hard cap** `MAX_TOOL_ITERATIONS` (default 6). Kalau model tidak
   selesai-selesai, bot berhenti dengan pesan yang wajar, bukan menggantung.

Ini kenapa teks "cuaca hari ini gimana" bisa menjadi dua HTTP request: sekali
untuk minta tool, sekali lagi untuk jawaban akhir.

### Kenapa urutan pesan penting

Prefix cache DeepSeek mencocokkan prefix request yang sama persis. Kalau
`system prompt` dan daftar tool berubah tiap panggilan, cache tidak kena dan
biaya input token membengkak. Karena itu `SYSTEM_PROMPT` statis, daftar tool
selalu dibangun identik, dan **waktu sekarang** tidak ditaruh di system prompt
melainkan sebagai satu pesan `user` transien yang tidak pernah ditulis ke
riwayat.

## 2. Tools: yang bisa dipanggil model

Semua tool sudah ditentukan di `app/tools.py`. Ini bukan "shell untuk LLM";
model **tidak punya** akses file, shell, atau internet bebas. Tool yang ada:

| Tool | Fungsi | Scope |
|---|---|---|
| `get_weather(city?)` | Open-Meteo (gratis, tanpa key) | semua user |
| `get_news(feed_names?, limit?)` | judul + link + tanggal dari RSS | semua user |
| `save_note(text)` / `list_notes(query?)` | catatan per user | semua user |
| `set_reminder(...)` dsb. | pengingat sekali/harian/mingguan | semua user |

Dua aturan keamanan yang keras:

- **`user_id` tidak pernah dari argumen model.** Ia di-inject oleh runtime
  (`ToolContext`). Model tidak bisa bilang "simpan catatan untuk user 999" —
  argumen seperti itu ditolak. Ini yang membuat isolasi multi-user bertahan
  meskipun model "dibujuk" oleh prompt injection.
- **Output tool diperlakukan sebagai data, bukan perintah.** Sistem prompt
  menyatakan: kalau teks di dalam RSS/berita berisi perintah (mis. "abaikan
  instruksi sebelumnya"), itu tetap hanya berita. Sebuah prompt injection di
  dalam feed tidak bisa mengubah perilaku bot.

## 3. Isolasi multi-user (kenapa aman)

Aturan di `app/db.py`: **setiap** fungsi akses data menerima `user_id` sebagai
parameter pertama dan selalu mem-filter hasilnya.

```python
def list_notes(user_id: int, query=None, limit=20):
    ... WHERE user_id = ? ...
```

Tidak ada SQL bebas di tempat lain di codebase; semua query berparameter.
Efeknya: sama seperti "setiap request web dipisahkan session", di sini setiap
permintaan bot dipisahkan oleh `user_id`. User A tidak bisa membaca/menghapus
catatan user B, karena tidak ada jalur kode yang bisa menyebut user lain.

Gerbang akses ada di `bot.py`: hanya user yang ada di tabel `users` yang
dibalas. Orang asing tidak mendapat balasan — id Telegram-nya justru di-log
sebagai warning supaya owner bisa menambahkannya via `/adduser`.

## 4. Scheduler (kapan recap dikirim)

`main.py` membuat job PTB `run_repeating` yang jalan tiap 60 detik. Satu kali
tick melakukan: recap harian, pengingat, lalu backup.

Cara memutuskan recap harian:

```
jika  jam:menit-lokal-sekarang  >=  time_hhmm   (di zona waktu user)
  dan  last_run_date != tanggal-sekarang
maka  kirim recap, set last_run_date
```

Dua keputusan desain:

- **Perbandingan `>=` bukan `==`.** Kalau container mati dari 05:00 sampai
  09:00, recap 06:30 tetap terkirim begitu hidup lagi — ia "menangkap" jadwal
  yang terlewat, bukan melewatkannya diam-diam.
- **Claim di SQL mencegah dobel.** `claim_schedule_run` hanya menang kalau
  `last_run_date` belum berisi tanggal hari itu. Dua tick yang saling tumpang
  tindih tidak bisa sama-sama mengirim.

Recap di `recap.py` menggabungkan data dengan kode biasa (cuaca kota user +
judul feed), lalu **satu** panggilan LLM untuk meringkas — model tidak boleh
pakai tool di sini. Kalau salah satu sumber gagal, recap tetap terkirim dengan
bagian yang tersedia dan menyebut sumber mana yang hilang. Hasil LLM dihitung
sebagai token, tapi tidak dihitung ke kuota harian pengguna.

## 5. Pengingat (reminders)

`reminders.py` mengubah satu baris SQLite menjadi masalah kalender:

- Waktu jatuh tempo disimpan sebagai **UTC** (untuk dibandingkan), plus
  jam-menit **lokal** user (untuk penerusan harian/mingguan). Ini membuat
  "jam 07:00 WIB" tetap 07:00 tanpa geser.
- `claim_due_reminders` menandai `last_sent_at` dalam satu transaksi dengan
  pembacaan, jadi bot yang crash tidak mengirim dua kali.
- Kalau bot mati 3 hari, pengingat harian terkirim **sekali** saat hidup lagi
  lalu jadwal lanjut — bukan 3 pesan menumpuk. Yang telat lebih dari 7 hari
  dibuang, bukan dikirim.

Waktu relatif ("besok jam 9") di-parse oleh LLM ke ISO 8601 lalu diverifikasi
tool; waktu yang sudah lewat ditolak. Bot selalu menyebut ulang tanggal-hasil
parse supaya kesalahan langsung terlihat.

## 6. Deployment

- Bot memakai **long polling**: container tidak membuka port apa pun, ia
  menanyakan update ke Telegram terus-menerus. Aman tanpa firewall tambahan.
- `restart: unless-stopped` + `mem_limit: 512M`, database di volume `./data`.
- Log adalah JSON terstruktur; di level info hanya id user, nama event, jumlah
  token — bukan key dan bukan isi pesan.