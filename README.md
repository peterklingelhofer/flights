# Nonstop weekend fare tracker (multi-route, multi-recipient)

Tracks the cheapest **nonstop round-trip** for **every future weekend** on one or more
routes, builds its own price history, and emails the right people when a weekend hits a
new low, drops below a threshold, or falls sharply. Free to run daily on GitHub Actions.

## How it actually works (read this first)

It does **not** track individual days, and it is **not** limited to Saturday/Sunday.
The unit it prices is a **trip**: an *outbound date* paired with a *return date*. Each
day it asks Google Flights for the cheapest nonstop round-trip for each pair and logs it.

"Weekend" is simply a rule about which **weekdays** those two dates land on. The default
says *leave Thursday or Friday, return Sunday or Monday, 2–4 nights*, which produces four
trip shapes for every week in the horizon:

- Thursday → Sunday (3 nights)
- Thursday → Monday (4 nights)
- Friday → Sunday (2 nights)  ← the classic weekend
- Friday → Monday (3 nights)

So yes — Fri→Sun and Thu→Sun are already covered. Over ~47 weekends that's ~188 priced
trips per route, each with its own tracked history and its own drop alerts.

## "I want a general focus on weekends" — the optimal setup

The default above **is** that setup, and it's the recommended one: it leans firmly into
weekends while still flexing across the few date shapes a real weekend trip can take, so
you catch whichever weekend dips — without drowning you in mid-week itineraries you don't
want. Tune it by editing the weekday/length rules (Mon=0 … Sun=6):

| Goal | OUT_WEEKDAYS | RET_WEEKDAYS | nights | Trips/week | Notes |
|------|:------------:|:------------:|:------:|:----------:|-------|
| **Weekend focus (recommended default)** | {3,4} Thu,Fri | {6,0} Sun,Mon | 2–4 | 4 | Best coverage of real weekends |
| Strict classic weekend | {4} Fri | {6} Sun | 2 | 1 | Cheapest to run, least coverage |
| Long weekends only | {3,4} Thu,Fri | {0} Mon | 3–4 | 2 | Mon return = 3-day weekends |
| Flexible week-long trips | {4,5} Fri,Sat | {6,0} Sun,Mon | 6–9 | varies | Not a "weekend" config |

Fewer trip shapes = fewer Google queries (gentler + faster); more shapes = better odds of
catching a cheap one. Set these globally, or per route (see below).

## Tracking both directions (the reverse route)

Philly→New Orleans is the **same setup with origin/dest swapped** — just add a second
`Watch`. It's worth tracking separately, not assumed identical: a Friday-to-Sunday trip
*from MSY* flies MSY→PHL on Friday and PHL→MSY on Sunday, while the same dates *from PHL*
fly PHL→MSY on Friday and MSY→PHL on Sunday. Different itineraries → often different
prices. The script keeps each route's history isolated and emails each route's own list.

## One script, multiple routes, different recipients

Edit the `WATCHES` list at the top of `track_flights.py`. Each `Watch` is a route plus its
own recipients (and optional per-route threshold / weekday overrides):

```python
WATCHES = [
    Watch(name="New Orleans → Philadelphia",
          origin=Airport.MSY, dest=Airport.PHL,
          recipients=["you@example.com"]),                       # NOLA folks visiting Philly

    Watch(name="Philadelphia → New Orleans",
          origin=Airport.PHL, dest=Airport.MSY,
          recipients=["friend1@example.com", "friend2@example.com"],  # Philly folks visiting NOLA
          abs_threshold=200),                                    # optional per-route override
]
```

One daily run prices every watch and sends a **separate email per route to that route's
recipients only**. Add as many watches and addresses as you like. Recipient addresses live
here in config (fine for a private repo); the sending account stays in Secrets.

## Is this economic?

Running it is **$0** regardless of how many routes/recipients you add — `fli` is free and
GitHub Actions is free. The only real cost is *request volume*: each extra route roughly
doubles the number of Google queries per run (two routes ≈ 380 queries/day at the default
horizon). To keep it lean and avoid throttling: trim the trip shapes (table above), lower
`HORIZON_DAYS` (e.g. 180 ≈ 6 months, plenty for "weeks-out" dips), or run every other day
via the cron. If you ever need stability over thrift, swap the data source (below) — that's
a few dollars/month, not free, but more reliable.

## Setup (~10 minutes)

1. **New GitHub repo** (private is fine). Add the four files; keep the workflow at
   `.github/workflows/track.yml`.
2. **Gmail App Password:** turn on 2-Step Verification
   (https://myaccount.google.com/security), then create one at
   https://myaccount.google.com/apppasswords (16 characters).
3. **Repo Secrets** (*Settings → Secrets and variables → Actions → Secrets*) — note that
   recipients are now in the code, so Secrets only hold the **sending** account:

   | Secret | Value |
   |--------|-------|
   | `SMTP_USER` | your Gmail address (the sender) |
   | `SMTP_PASS` | the 16-char app password |

   (Optional: `SMTP_HOST`/`SMTP_PORT` for non-Gmail, `ALERT_FROM`, or
   `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`.)
4. **Edit `WATCHES`** with your real routes and recipient emails.
5. **Allow commits:** *Settings → Actions → General → Workflow permissions → Read and
   write → Save.*
6. **Run once:** *Actions → Flight price tracker → Run workflow.* It then runs daily and
   commits `prices.db` + a `snapshot_<ROUTE>.md` per route.

## Run locally first (optional)

```bash
pip install -r requirements.txt
python track_flights.py     # with no SMTP creds it just prints what it would send
```

## Tuning knobs (top of `track_flights.py`)

- `DEFAULT_OUT_WEEKDAYS` / `DEFAULT_RET_WEEKDAYS` / `DEFAULT_MIN_NIGHTS` / `DEFAULT_MAX_NIGHTS` — the weekend rules (or override per `Watch`).
- `DEFAULT_ABS_THRESHOLD` ($220) — "this cheap = tell me" (per-route override available; also a repo Variable `ABS_THRESHOLD`).
- `PCT_DROP` (0.15) — alert on a 15% drop vs the last reading.
- `HORIZON_DAYS` (330) — how far ahead to look.

## Swapping the data source for reliability

Only `cheapest_roundtrip()` changes. For SerpApi (free 250/mo; also returns Google's own
price history), call `https://serpapi.com/search?engine=google_flights` with
`departure_id`, `arrival_id`, `outbound_date`, `return_date`, `stops=1` (nonstop),
`currency=USD`, and read `best_flights[0].price`. Storage, alerts, and email are untouched.

## Troubleshooting

- **No email:** check `SMTP_USER`/`SMTP_PASS` Secrets, that 2-Step Verification is on, that
  you used an *app password*, and that the route's `recipients` aren't still `example.com`.
- **`SearchHTTPError` / empty / throttling:** retries + delays are built in; if persistent,
  trim trip shapes, lower `HORIZON_DAYS`, run less often, or `pip install -U flights`.
- **Schedule stopped:** GitHub disables cron after ~60 days of repo inactivity (it emails
  you to re-enable; automated commits may not reset the timer).
- **Prices slightly off:** scraped estimates — every alert has a one-click nonstop verify
  link; confirm on the booking site before buying.
