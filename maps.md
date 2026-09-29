# Create Route — Telegram Route Manager

## Overview

Add a route management feature to the Telegram personal assistant.

Users can create and manage custom routes directly from Telegram using natural language and Telegram Location messages.

Primary use case:

> Monitor the user's preferred routes from point A → B, where multiple route alternatives may exist.

Example:

```text
Route Pulang #1

A (Office)
 ↓
P1
 ↓
P2
 ↓
P3
 ↓
B (Route merge point)
```

```text
Route Pulang #2

A (Office)
 ↓
Q1
 ↓
Q2
 ↓
Q3
 ↓
B (Route merge point)
```

The section after B does not need to be monitored because both routes share the same path afterward.

---

# Goals

* Create a route through Telegram.
* Accept GPS coordinates from Telegram Location messages.
* Support multiple waypoints.
* Support multiple route alternatives.
* Allow routes to have a human-readable name.
* Store routes persistently.
* Allow users to list, inspect, rename, and delete routes.
* Prepare route data for future traffic/ETA monitoring.
* Keep the interaction simple enough to use from a phone.

---

# Example User Flow

## 1. Start creating a route

User:

```text
buat route pulang
```

Bot:

```text
🛣️ Create Route

Nama route:
```

User:

```text
Pulang Kantor
```

Bot:

```text
Oke.

Sekarang kirim 📍 lokasi titik A
```

User sends Telegram Location.

Bot:

```text
✅ Titik A tersimpan.

Sekarang kirim lokasi waypoint pertama.
Kirim setiap titik yang ingin dilewati.

Contoh:
📍 Belokan pertama
📍 Belokan kedua
📍 Belokan ketiga

Ketik "done" kalau sudah sampai titik B.
```

---

# Waypoint Recording

Every Telegram Location received while route creation is active becomes a waypoint.

Example:

```text
A
 ↓
P1
 ↓
P2
 ↓
P3
 ↓
B
```

The bot should preserve the order in which locations were received.

Each waypoint should contain:

```json
{
  "sequence": 1,
  "latitude": -6.123456,
  "longitude": 106.789012
}
```

The sequence must be deterministic and start from `1`.

---

# Route End

User:

```text
done
```

Bot:

```text
📍 Titik B sudah?

Kirim lokasi titik B.
```

User sends Location.

Bot:

```text
Route berhasil dibuat.

🛣️ Pulang Kantor
A → 3 waypoint → B

Total titik:
5

Simpan route ini?
[✅ Simpan] [❌ Batal]
```

After confirmation:

```text
✅ Route "Pulang Kantor" berhasil disimpan.
```

---

# Multiple Route Alternatives

A route name represents a route group.

Example:

```text
Pulang
├── via Pasar Minggu
└── via TB Simatupang
```

The user can create another alternative:

```text
tambah alternatif route pulang
```

Bot:

```text
🛣️ Tambah Alternatif

Untuk route:
Pulang

Nama alternatif:
```

User:

```text
via TB Simatupang
```

Then the bot starts waypoint recording again.

---

# Suggested Data Model

Use two entities:

## Route

```text
routes
------
id
user_id
name
description
origin_lat
origin_lng
destination_lat
destination_lng
created_at
updated_at
```

## Route Waypoints

```text
route_waypoints
---------------
id
route_id
sequence
latitude
longitude
created_at
```

If route alternatives are required, add:

```text
route_groups
------------
id
user_id
name
description
created_at
updated_at
```

Then:

```text
route_groups
     │
     ├── route 1
     │      ├── waypoint 1
     │      ├── waypoint 2
     │      └── waypoint 3
     │
     └── route 2
            ├── waypoint 1
            ├── waypoint 2
            └── waypoint 3
```

This allows:

```text
Pulang
├── Via Pasar Minggu
└── Via TB Simatupang
```

while keeping each route independently configurable.

---

# Telegram Location Handling

Telegram location messages should be detected separately from normal text messages.

Conceptually:

```javascript
if (message.location) {
    handleRouteLocation(message.location)
}
```

Telegram provides:

```json
{
  "latitude": -6.123456,
  "longitude": 106.789012
}
```

Do not ask users to manually type latitude/longitude.

The mobile Telegram location picker becomes the primary UI for route creation.

---

# Route Creation State

Because route creation is multi-step, the bot needs temporary conversation state.

Example:

```json
{
  "state": "creating_route",
  "route_group_id": 123,
  "route_name": "Pulang",
  "route_name_pending": "Via Pasar Minggu",
  "step": "waypoints",
  "waypoints": [
    {
      "sequence": 1,
      "latitude": -6.123,
      "longitude": 106.789
    },
    {
      "sequence": 2,
      "latitude": -6.125,
      "longitude": 106.791
    }
  ]
}
```

State should expire automatically after a reasonable timeout.

Example:

```text
route_creation_expires_at
```

This prevents abandoned route creation sessions from remaining active forever.

---

# Commands / Natural Language

The assistant should support natural language first.

Examples:

```text
buat route pulang
```

```text
buat rute baru
```

```text
tambah route pulang
```

```text
tambah alternatif route pulang
```

```text
lihat route gue
```

```text
lihat route pulang
```

```text
hapus route via pasar minggu
```

```text
rename route via pasar minggu jadi lewat ciputat
```

Natural language should eventually be mapped into internal actions:

```text
CREATE_ROUTE
ADD_ROUTE_ALTERNATIVE
LIST_ROUTES
SHOW_ROUTE
DELETE_ROUTE
RENAME_ROUTE
CANCEL_ROUTE_CREATION
```

DeepSeek can be used for intent extraction, but route state transitions should remain deterministic in application code.

---

# Route Preview

Before saving, show a textual summary.

Example:

```text
🛣️ Route Preview

Group:
Pulang

Alternative:
Via Pasar Minggu

📍 Origin
-6.123456, 106.789012

📍 Waypoints
1. -6.124000, 106.790000
2. -6.126000, 106.793000
3. -6.129000, 106.796000

📍 Destination
-6.135000, 106.801000

Total waypoints: 3

Simpan route?
```

Future improvement:

Send a map/static map preview if a suitable map API is available.

---

# Validation

Validate every location:

```text
latitude >= -90
latitude <= 90

longitude >= -180
longitude <= 180
```

Also prevent accidental duplicate points.

For example, if the user sends the same location twice within a very small distance, the application may ignore the duplicate.

Recommended threshold:

```text
distance < 10 meters
```

However, do not remove legitimate nearby turns automatically if the user intentionally records them.

---

# Route Editing

After creation, users should be able to modify routes.

Example:

```text
edit route pulang via pasar minggu
```

Bot:

```text
🛠️ Edit Route

What do you want to change?

1. Add waypoint
2. Remove waypoint
3. Replace waypoint
4. Rename route
5. Delete route
```

For the first implementation, only support:

```text
Rename
Delete
Re-record route
```

Waypoint-level editing can be added later.

---

# Future Traffic Monitoring

The route structure is intentionally designed for future traffic monitoring.

Example:

```text
Route A
A → P1 → P2 → P3 → B

Route B
A → Q1 → Q2 → Q3 → B
```

The traffic checker can request:

```text
A → P1 → P2 → P3 → B
```

and:

```text
A → Q1 → Q2 → Q3 → B
```

from the routing provider.

The system can then compare:

```text
Route A: 42 min
Route B: 31 min

Difference: 11 min
```

Future Telegram notification:

```text
🛵 Route Pulang

Via Pasar Minggu
⏱️ 42 min

Via TB Simatupang
⏱️ 31 min

⚡ Via TB Simatupang lebih cepat 11 menit.
```

The application should not let the LLM invent ETA or traffic information.

Traffic/ETA must come from the routing provider.

DeepSeek should only interpret the returned data and generate the natural-language response.

---

# Automation Integration

Once routes exist, they can be referenced by automation.

Example:

```text
"cek route pulang tiap jam 5 sore"
```

The assistant creates an automation:

```text
Schedule:
Weekdays at 17:00

Action:
Check route group = "Pulang"

Compare:
All active alternatives

Notify:
Telegram
```

Another example:

```text
"kasih tau gue kalau route pulang lebih dari 60 menit"
```

Becomes:

```text
Condition:
ETA > 60 minutes

Action:
Send Telegram notification
```

---

# Security

Routes are private user data.

Every route query must be scoped to:

```text
user_id
```

Never allow:

```text
user A → route user B
```

All CRUD operations should include user ownership checks.

Example:

```sql
SELECT *
FROM routes
WHERE id = ?
AND user_id = ?
```

Do not rely only on the route ID.

---

# MVP Scope

### Phase 1

Implement:

* Create route group
* Create route alternative
* Telegram Location input
* Origin
* Multiple waypoints
* Destination
* Route preview
* Save
* Cancel
* List routes
* Delete route
* Rename route

### Phase 2

Implement:

* Google Routes API integration
* ETA calculation
* Traffic-aware route comparison
* `/route pulang`
* Manual route check

### Phase 3

Implement:

* Scheduled route checking
* Conditional notifications
* "Should I leave now?"
* Compare current ETA vs expected ETA
* Automatic recommendation based on user-defined rules

---

# Example End-to-End

User:

```text
buat route pulang
```

Bot:

```text
Nama route?
```

User:

```text
Pulang
```

Bot:

```text
Kirim lokasi titik A.
```

User sends 📍

Bot:

```text
✅ A tersimpan.

Kirim waypoint berikutnya.
```

User sends:

```text
📍
📍
📍
```

Bot:

```text
3 waypoint tersimpan.

Kirim "done" kalau sudah sampai titik B.
```

User:

```text
done
```

Bot:

```text
Kirim lokasi titik B.
```

User sends 📍

Bot:

```text
🛣️ Route "Pulang"

A → 3 waypoint → B

Simpan?
```

User:

```text
iya
```

Bot:

```text
✅ Route tersimpan.

Mau tambah alternatif route?
```

User:

```text
iya, via TB Simatupang
```

Bot langsung masuk recording mode lagi.

Result:

```text
Pulang
├── Via Pasar Minggu
│   └── A → P1 → P2 → P3 → B
│
└── Via TB Simatupang
    └── A → Q1 → Q2 → Q3 → B
```

This becomes the foundation for the future **automatic route-to-home traffic checker**.
