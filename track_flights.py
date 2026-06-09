#!/usr/bin/env python3
"""
Nonstop round-trip weekend fare tracker — multi-route, multi-recipient.

It tracks ROUND TRIPS, not single days. Each thing it prices is a *trip*: an
outbound date + a return date. "Weekend" is just a rule about which weekdays those
two dates land on (e.g. leave Thu/Fri, come back Sun/Mon). So Fri->Sun and Thu->Sun
are exactly the kind of trips it already covers.

Define one or more `Watch` entries below — each is a route (origin -> dest) with its
own list of email recipients. One run prices every weekend for every watch, keeps a
per-route price history in SQLite, and emails *that route's* recipients when a weekend
hits a new low, crosses your threshold, or drops sharply.

Data source: `fli` (PyPI "flights") — free, unofficial Google Flights client.
"""

from __future__ import annotations

import os
import ssl
import json
import time
import random
import sqlite3
import smtplib
import datetime as dt
from dataclasses import dataclass
from email.mime.text import MIMEText
from urllib.parse import quote
from urllib.request import urlopen, Request

from fli.models import (
    Airport, PassengerInfo, SeatType, MaxStops, SortBy, TripType,
    FlightSearchFilters, FlightSegment,
)
from fli.search import SearchFlights

# =========================================================================== #
# 1) WHAT TO WATCH  —  add a Watch per route; give each its own recipients.
# =========================================================================== #
@dataclass
class Watch:
    name: str                       # shown in the email subject
    origin: Airport
    dest: Airport
    recipients: list[str]           # who gets THIS route's alerts
    # Optional per-route overrides; leave as None to use the global defaults below.
    abs_threshold: float | None = None
    out_weekdays: set[int] | None = None
    ret_weekdays: set[int] | None = None
    min_nights: int | None = None
    max_nights: int | None = None

    @property
    def code(self) -> str:          # e.g. "MSY-PHL"
        return f"{self.origin.name}-{self.dest.name}"


WATCHES = [
    # New Orleans -> Philadelphia round-trip, weekends (depart MSY):
    Watch(
        name="New Orleans → Philadelphia",
        origin=Airport.MSY, dest=Airport.PHL,
        recipients=["peterklingelhofer@gmail.com"],
    ),
]

# =========================================================================== #
# 2) GLOBAL DEFAULTS  (apply to any Watch that doesn't override them)
# =========================================================================== #
ADULTS = 1
SEAT = SeatType.ECONOMY
EXCLUDE_BASIC_ECONOMY = False

# "Weekend" = leave Thu/Fri, return Sun/Mon, 2–4 nights. Weekdays are Mon=0 … Sun=6.
# This yields four trip shapes per week: Thu->Sun, Thu->Mon, Fri->Sun, Fri->Mon.
DEFAULT_OUT_WEEKDAYS = {3, 4}        # Thu, Fri
DEFAULT_RET_WEEKDAYS = {6, 0}        # Sun, Mon
DEFAULT_MIN_NIGHTS = 2
DEFAULT_MAX_NIGHTS = 4

DEFAULT_ABS_THRESHOLD = float(os.environ.get("ABS_THRESHOLD") or 180)
PCT_DROP = float(os.environ.get("PCT_DROP") or 0.20)
HORIZON_DAYS = int(os.environ.get("HORIZON_DAYS") or 330)

# Politeness / reliability when hitting Google (doubles in volume per extra route).
DELAY_MIN, DELAY_MAX = 2.0, 4.5
RETRIES = 3
TOP_N = 2

SEND_DIGEST_EVEN_IF_NO_ALERTS = False
DB_PATH = os.environ.get("DB_PATH") or "prices.db"
TODAY = dt.date.today().isoformat()


# =========================================================================== #
# Date-pair generation (per watch)
# =========================================================================== #
def weekend_date_pairs(w: Watch) -> list[tuple[str, str]]:
    out_wd = w.out_weekdays or DEFAULT_OUT_WEEKDAYS
    ret_wd = w.ret_weekdays or DEFAULT_RET_WEEKDAYS
    lo = w.min_nights if w.min_nights is not None else DEFAULT_MIN_NIGHTS
    hi = w.max_nights if w.max_nights is not None else DEFAULT_MAX_NIGHTS

    pairs: list[tuple[str, str]] = []
    start = dt.date.today() + dt.timedelta(days=1)
    for offset in range(HORIZON_DAYS):
        out = start + dt.timedelta(days=offset)
        if out.weekday() not in out_wd:
            continue
        for nights in range(lo, hi + 1):
            ret = out + dt.timedelta(days=nights)
            if ret.weekday() in ret_wd:
                pairs.append((out.isoformat(), ret.isoformat()))
    return pairs


# =========================================================================== #
# Flight search — the one function to swap if you change data sources.
# =========================================================================== #
def cheapest_roundtrip(origin: Airport, dest: Airport,
                       out_date: str, ret_date: str) -> dict | None:
    """Cheapest NONSTOP round-trip total (origin->dest->origin) for the dates."""
    filters = FlightSearchFilters(
        trip_type=TripType.ROUND_TRIP,
        passenger_info=PassengerInfo(adults=ADULTS),
        seat_type=SEAT,
        stops=MaxStops.NON_STOP,                 # both legs nonstop
        sort_by=SortBy.CHEAPEST,
        exclude_basic_economy=EXCLUDE_BASIC_ECONOMY,
        flight_segments=[
            FlightSegment(departure_airport=[[origin, 0]],
                          arrival_airport=[[dest, 0]], travel_date=out_date),
            FlightSegment(departure_airport=[[dest, 0]],
                          arrival_airport=[[origin, 0]], travel_date=ret_date),
        ],
    )
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            results = SearchFlights().search(filters, top_n=TOP_N, currency="USD")
            if not results:
                return None
            best = None
            for item in results:                 # round-trip => tuples (out, ret)
                legs = item if isinstance(item, tuple) else (item,)
                total = legs[-1].price if legs[-1].price is not None else legs[0].price
                if total is None:
                    continue
                airline = legs[0].primary_airline_name
                if airline is None and legs[0].legs:
                    airline = legs[0].legs[0].airline.value
                if best is None or total < best["price"]:
                    best = {"price": float(total),
                            "currency": legs[-1].currency or "USD",
                            "airline": airline or "?"}
            return best
        except Exception as e:
            last_err = e
            time.sleep(min(30, 2 ** attempt) + random.uniform(0, 1.5))
    print(f"   ! giving up on {origin.name}->{dest.name} {out_date}->{ret_date}: {last_err}")
    return None


# =========================================================================== #
# Storage  (history is isolated PER ROUTE via origin+dest)
# =========================================================================== #
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            origin TEXT, dest TEXT, out_date TEXT, ret_date TEXT,
            price REAL, currency TEXT, airline TEXT,
            observed_at TEXT, observed_date TEXT
        )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_route_pair
                    ON observations(origin, dest, out_date, ret_date, observed_date)""")
    return conn


def record(conn, o, d, out_date, ret_date, info):
    conn.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                 (o, d, out_date, ret_date, info["price"], info["currency"],
                  info["airline"], dt.datetime.utcnow().isoformat(timespec="seconds"),
                  TODAY))


def prior_stats(conn, o, d, out_date, ret_date):
    """(all-time min before today, most recent price before today) for THIS route+pair."""
    pmin = conn.execute(
        """SELECT MIN(price) FROM observations
           WHERE origin=? AND dest=? AND out_date=? AND ret_date=? AND observed_date<?""",
        (o, d, out_date, ret_date, TODAY)).fetchone()[0]
    row = conn.execute(
        """SELECT price FROM observations
           WHERE origin=? AND dest=? AND out_date=? AND ret_date=? AND observed_date<?
           ORDER BY observed_at DESC LIMIT 1""",
        (o, d, out_date, ret_date, TODAY)).fetchone()
    return pmin, (row[0] if row else None)


# =========================================================================== #
# Links + alert logic
# =========================================================================== #
def kayak_link(o, d, out_date, ret_date):
    return f"https://www.kayak.com/flights/{o}-{d}/{out_date}/{ret_date}?fs=stops=0"


def evaluate(price, pmin, plast, threshold):
    reasons = []
    new_low_floor = max(5.0, (pmin or 0) * 0.01)        # ignore trivial new lows
    if pmin is not None and price <= pmin - new_low_floor:
        reasons.append(f"new low (was ${pmin:.0f})")
    if price <= threshold and (plast is None or plast > threshold):
        reasons.append(f"under ${threshold:.0f}")
    if plast and plast > 0 and (plast - price) / plast >= PCT_DROP:
        reasons.append(f"-{(plast - price)/plast*100:.0f}% vs last (${plast:.0f}→${price:.0f})")
    if pmin is None and plast is None and price <= threshold:
        reasons.append("first look, already cheap")
    return reasons


# =========================================================================== #
# Notifications
# =========================================================================== #
def send_email(subject: str, html: str, to: list[str]) -> None:
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or 465)
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    sender = os.environ.get("ALERT_FROM") or user
    to = [a for a in (to or []) if a and "example.com" not in a]

    if not (user and pw and to):
        print(f"\n[not emailing — missing creds or recipients] would send: {subject}\n")
        return
    msg = MIMEText(html, "html")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(to)
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.sendmail(sender, to, msg.as_string())
    print(f"   emailed -> {', '.join(to)}")


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    body = f"chat_id={quote(chat)}&parse_mode=HTML&text={quote(text)}".encode()
    urlopen(Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body), timeout=20).read()


def create_github_issue(subject: str, body_md: str) -> None:
    """Open a GitHub Issue so GitHub emails you (no SMTP creds needed).

    Uses the Actions-provided GITHUB_TOKEN and GITHUB_REPOSITORY. The body
    @mentions the repo owner so the notification (and email) is guaranteed
    regardless of watch settings. No-op when those env vars are absent
    (e.g. running locally), so it never blocks a local dry-run.
    """
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")          # "owner/name"
    if not (token and repo):
        print("   [no GITHUB_TOKEN/REPOSITORY — skipping issue]")
        return
    owner = repo.split("/")[0]
    payload = json.dumps({
        "title": subject,
        "body": f"@{owner}\n\n{body_md}",
        "labels": ["fare-alert"],
    }).encode()
    req = Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "flight-tracker",
        },
    )
    try:
        with urlopen(req, timeout=30) as r:
            num = json.loads(r.read()).get("number")
        print(f"   opened issue #{num} (GitHub will email watchers)")
    except Exception as e:
        print(f"   ! failed to open GitHub issue: {e}")


# =========================================================================== #
# Per-route snapshot (committed each run; browse/chart later)
# =========================================================================== #
def write_snapshot(w: Watch, rows):
    rows = sorted([r for r in rows if r["price"] is not None], key=lambda r: r["price"])
    lines = [f"# Cheapest nonstop {w.origin.name} → {w.dest.name} round-trips  ({w.name})",
             f"_Updated {dt.datetime.utcnow():%Y-%m-%d %H:%M} UTC_\n",
             "| Out | Return | Nights | Price | Airline |",
             "|-----|--------|:------:|------:|---------|"]
    for i, r in enumerate(rows):
        n = (dt.date.fromisoformat(r["ret"]) - dt.date.fromisoformat(r["out"])).days
        mark = " 🏆" if i == 0 else ""
        lines.append(f"| {r['out']} | {r['ret']} | {n} | ${r['price']:.0f}{mark} | {r['airline']} |")
    with open(f"snapshot_{w.code}.md", "w") as f:
        f.write("\n".join(lines) + "\n")


# =========================================================================== #
# Main — loop every watch, alert its own recipients
# =========================================================================== #
def process_watch(conn, w: Watch):
    threshold = w.abs_threshold if w.abs_threshold is not None else DEFAULT_ABS_THRESHOLD
    pairs = weekend_date_pairs(w)
    print(f"\n=== {w.name} ({w.code}) — {len(pairs)} weekend pairs, "
          f"alert<=${threshold:.0f} -> {', '.join(w.recipients)} ===")
    alerts, snapshot = [], []

    for idx, (out_date, ret_date) in enumerate(pairs, 1):
        info = cheapest_roundtrip(w.origin, w.dest, out_date, ret_date)
        if not info:
            print(f"[{idx}/{len(pairs)}] {out_date}->{ret_date}: no nonstop")
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX)); continue

        price = info["price"]
        pmin, plast = prior_stats(conn, w.origin.name, w.dest.name, out_date, ret_date)
        record(conn, w.origin.name, w.dest.name, out_date, ret_date, info)
        conn.commit()
        snapshot.append({"out": out_date, "ret": ret_date, "price": price, "airline": info["airline"]})

        reasons = evaluate(price, pmin, plast, threshold)
        print(f"[{idx}/{len(pairs)}] {out_date}->{ret_date}: ${price:.0f} ({info['airline']})"
              + ("  <<< " + "; ".join(reasons) if reasons else ""))
        if reasons:
            alerts.append({"out": out_date, "ret": ret_date, "price": price,
                           "airline": info["airline"], "reasons": reasons})
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    write_snapshot(w, snapshot)
    cheapest = min(snapshot, key=lambda r: r["price"]) if snapshot else None
    if not alerts and not SEND_DIGEST_EVEN_IF_NO_ALERTS:
        print(f"   no alerts for {w.code} today"); return

    alerts.sort(key=lambda a: a["price"])
    rows_html = "".join(
        f"<tr><td>{a['out']} → {a['ret']}</td><td align='right'><b>${a['price']:.0f}</b></td>"
        f"<td>{a['airline']}</td><td>{'; '.join(a['reasons'])}</td>"
        f"<td><a href='{kayak_link(w.origin.name, w.dest.name, a['out'], a['ret'])}'>verify</a></td></tr>"
        for a in alerts)
    foot = (f"Cheapest tracked weekend right now: <b>${cheapest['price']:.0f}</b> "
            f"({cheapest['out']} → {cheapest['ret']}, {cheapest['airline']})" if cheapest else "")
    html = (f"<p>{len(alerts)} nonstop <b>{w.name}</b> weekend(s) moved:</p>"
            f"<table border='1' cellpadding='6' cellspacing='0'>"
            f"<tr><th>Dates</th><th>Price</th><th>Airline</th><th>Why</th><th>Book</th></tr>"
            f"{rows_html}</table><p>{foot}</p>"
            f"<p style='color:#888'>Scraped estimates — confirm on the booking site before buying.</p>")
    subj = (f"✈️ {w.name}: {len(alerts)} weekend deal(s)"
            + (f" — cheapest ${cheapest['price']:.0f}" if cheapest else ""))

    # Markdown variant for the GitHub Issue notifier (GitHub renders tables).
    md_rows = "".join(
        f"| {a['out']} → {a['ret']} | ${a['price']:.0f} | {a['airline']} | "
        f"{'; '.join(a['reasons'])} | "
        f"[verify]({kayak_link(w.origin.name, w.dest.name, a['out'], a['ret'])}) |\n"
        for a in alerts)
    md_foot = (f"\nCheapest tracked weekend right now: **${cheapest['price']:.0f}** "
               f"({cheapest['out']} → {cheapest['ret']}, {cheapest['airline']})\n"
               if cheapest else "")
    body_md = (f"{len(alerts)} nonstop **{w.name}** weekend(s) moved:\n\n"
               "| Dates | Price | Airline | Why | Book |\n"
               "|---|---|---|---|---|\n"
               f"{md_rows}{md_foot}\n"
               "_Scraped estimates — confirm on the booking site before buying._")

    send_email(subj, html, w.recipients)
    send_telegram(subj)
    create_github_issue(subj, body_md)


def main():
    conn = db()
    for w in WATCHES:
        process_watch(conn, w)
    conn.close()


if __name__ == "__main__":
    main()
