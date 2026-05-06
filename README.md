# Starboard ★

Pi-hosted public leaderboard for the daily Stars puzzle. Two parallel
scoring systems run side by side:

- **APR (Adjusted Performance Rating)** — long-term skill rating that's
  rating-aware and attendance-blind. Cherry-picking weak fields can't
  inflate it.
- **Weekly Skirmish** — five locked-in awards each Monday (🏆 Champion,
  🔥 Iron Man, ⚡ Lightning, 🎯 Steady, 🗡️ Giant Slayer) so different
  players win different things.

## Stack

Python 3.11 · Flask · SQLite (one file, WAL) · Flask-Login + argon2 ·
Flask-Limiter · Jinja + Tailwind CDN + Chart.js + HTMX · Gunicorn · systemd.

## Layout

```
starboard/                   # Python package
  app.py                     # Flask factory + public routes
  auth.py                    # signup / login / account
  admin.py                   # admin blueprint
  submit.py                  # /submit (admin always; users behind a flag)
  apr.py                     # rating math
  weekly.py                  # award math
  queries.py                 # shared read queries for views
  db.py                      # schema + connection helper
  config.py                  # env loading
  jobs.py                    # `python -m starboard.jobs close_last_week`
  extensions.py              # shared Flask extensions
templates/                   # Jinja templates (incl. admin/)
static/                      # app.css + chart-init.js
deploy/                      # systemd units, .env.example, cloudflared
seed.py                      # CSV import
wsgi.py                      # gunicorn entrypoint
tests/                       # pytest suite (31 cases)
```

## 15-minute Pi setup

```bash
sudo useradd -r -m -d /opt/starboard -s /bin/bash starboard
sudo mkdir -p /var/log/starboard /var/backups/starboard
sudo chown starboard:starboard /var/log/starboard /var/backups/starboard

sudo -u starboard -H bash <<'EOF'
cd /opt/starboard
git clone <your-repo-url> .
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp deploy/.env.example .env
# Edit .env — at minimum set SECRET_KEY, ADMIN_EMAIL, ADMIN_USERNAME,
# ADMIN_PASSWORD, and DATABASE_PATH=/opt/starboard/starboard.db.
EOF

# Seed Season 1 (with the CSV you provide):
sudo -u starboard /opt/starboard/.venv/bin/python /opt/starboard/seed.py \
    --reset --yes \
    --roster /opt/starboard/season1-roster.csv \
    /opt/starboard/season1-submissions.csv

# Install systemd units:
sudo cp deploy/starboard.service /etc/systemd/system/
sudo cp deploy/starboard-weekly-close.service /etc/systemd/system/
sudo cp deploy/starboard-weekly-close.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now starboard.service
sudo systemctl enable --now starboard-weekly-close.timer

# Backups (cron):
sudo crontab -u starboard -e
# Add:  5 2 * * *  /opt/starboard/deploy/backup.sh

# Log rotation:
sudo cp deploy/logrotate.starboard /etc/logrotate.d/starboard
```

The app is now serving on `127.0.0.1:5000`. Expose it via Cloudflare
Tunnel (preferred, no port-forwarding needed):

```bash
sudo apt install cloudflared
cloudflared tunnel login
cloudflared tunnel create starboard
sudo cp deploy/cloudflared.yml.example /etc/cloudflared/config.yml
# Edit config.yml — set credentials-file to the path printed by `tunnel create`.
cloudflared tunnel route dns starboard starboard.day
sudo systemctl enable --now cloudflared
```

DNS for `starboard.day` is now pointed through the tunnel; HTTPS is
handled by Cloudflare automatically.

## CSV formats

**Roster** (`name,display_name,joined_date`):

```csv
name,display_name,joined_date
"Mendiola, Julian",Julian,2026-01-06
"Smith, Anne",Anne,2026-01-06
```

**Submissions** (`date,player_name,time_seconds`):

```csv
date,player_name,time_seconds
2026-01-06,"Mendiola, Julian",94.3
2026-01-06,"Smith, Anne",112.8
```

`time_seconds` accepts raw seconds (`94.3`) or `m:ss` (`1:34.3`).

## Permissions model

| Tier              | Capabilities                                                                    |
|-------------------|---------------------------------------------------------------------------------|
| Public (no login) | All read pages: standings, weekly, profiles, h2h, records.                      |
| Authenticated     | Same as public + claim a player profile from `/account`.                        |
| Admin             | Full CRUD; recompute APR; recompute weekly awards; manage users.                |

Any signed-up user matching `ADMIN_EMAIL` is auto-promoted to admin on
signup. Existing admins can promote others from `/admin/users`.

## Feature flag: user submissions

`ENABLE_USER_SUBMISSIONS=false` (the default) keeps `/submit`
admin-only. Flip it to `true` to let any authenticated user with a
claimed player submit times for that player only — within the
`SUBMISSION_LOOKBACK_DAYS` window (admins are not capped).

## How the math works

APR replaces standard ELO with a generalization of FIDE's Tournament
Performance Rating to time-based events. For each day:

```
actual_z   = -(your_time - mean_time) / std_time
expected_z = (your_rating - field_avg_R) / 400
delta      = 22 × (actual_z - expected_z)
```

Non-submitters' ratings are unchanged. Cherry-pickers don't gain rating
just by winning a weak field — they have to outperform their rating's
predicted z.

Weekly Skirmish awards five trophies. Eligibility:

| Award         | Trigger                                        | Eligibility            |
|---------------|------------------------------------------------|------------------------|
| 🏆 Champion    | Highest mean z across the week's submissions   | ≥ 4 days played        |
| 🔥 Iron Man    | Most submissions that week                     | ≥ 1 submission         |
| ⚡ Lightning   | Highest single-day z that week                 | ≥ 1 submission         |
| 🎯 Steady      | Lowest std of z-scores that week               | ≥ 3 days played        |
| 🗡️ Giant Slayer | Biggest APR-rating-gap upset on a single day  | gap ≥ 200              |

Awards lock immutable once the week closes. The systemd timer fires
Mondays at 00:05 (`WEEK_TIMEZONE`) and runs
`python -m starboard.jobs close_last_week`.

## Day-to-day operations

- **Submit a time as admin**: `/submit` (visible in nav once signed in
  as admin), or `/admin` → manage day.
- **Fix bad data**: `/admin/days/<id>/submissions` — every edit triggers
  an automatic APR recompute.
- **Restate a closed week's awards** (e.g. you fixed an old time):
  `/admin` → "Recompute one week" with that Monday's date.
- **Manual close** (skip waiting for the timer):
  `python -m starboard.jobs close_last_week`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Headline tests:
- `test_cherry_picker_gains_less_in_weak_field` — proves identical
  performance in a weak field yields strictly less rating gain.
- `test_giant_slayer_threshold_excludes_small_upsets` — 199 fails, 200
  qualifies.
- Award eligibility + tie-break chain across all 5 categories.

## License

Private league software. Use as you like internally.
