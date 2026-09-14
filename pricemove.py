#!/usr/bin/env python3
"""
pricemove.py - track how Australian racing prices move between market open and the jump.

Three modes, one analysis engine:

  watch   keyless. Polls the free demo endpoint on an interval and builds its own
          price series from the snapshots it collects. 0 credits, no API key.
  scan    keyed. Reads /v1/racing/movers - the server's own firming/drifting scan.
  race    keyed. Reads /v1/racing/price-history for one race and prints the
          per-runner open / close / high / low / move_pct table, a terminal
          sparkline, and optionally a standalone SVG chart.

Standard library only. No pip install, no matplotlib. Runs on a bare python3.

Data source: PuntersEdge - https://puntersedge.online
"""

import argparse
import gzip
import http.client
import json
import math
import os
import ssl
import sys
import time
from datetime import datetime, timezone

API_HOST = "api.puntersedge.online"
UA = "racing-price-movement-tracker/1.0 (+https://github.com/Propertyscout001/racing-price-movement-tracker)"

SIGNUP = ("https://puntersedge.online/api"
          "?utm_source=racing-price-movement-tracker&utm_medium=code")

# Credit cost per successful call, for the running tally we print.
COST = {
    "/v1/racing/movers": 3,
    "/v1/racing/price-history": 5,
    "/v1/racing/next-to-go": 2,
}

BLOCKS = "▁▂▃▄▅▆▇█"
ASCII_BLOCKS = "._-=+*#@"


class ApiError(Exception):
    """An RFC 9457 problem+json response, or a transport failure."""

    def __init__(self, status, title, detail=None, type_url=None):
        self.status = status
        self.title = title
        self.detail = detail
        self.type_url = type_url
        super().__init__("HTTP %s: %s" % (status, title))

    def __str__(self):
        out = "HTTP %s: %s" % (self.status, self.title)
        if self.detail and self.detail != self.title:
            out += "\n  detail: %s" % (json.dumps(self.detail)
                                       if not isinstance(self.detail, str) else self.detail)
        return out


class Client:
    """
    Keep-alive HTTPS client with gzip.

    Two deliberate choices, both of which the API docs push you toward:

      * Accept-Encoding: gzip. These responses compress well - a race price
        history is highly repetitive JSON.
      * One connection, reused. A cold TLS handshake costs far more than the
        request itself, and this tool makes many small calls in a loop.

    Retry policy is deliberately narrow. A hidden retry on a price endpoint can
    hand you a price that was recorded before a move, so this client NEVER
    retries a response it actually received - no 5xx retry, no 4xx retry. It
    reconnects and retries exactly once when a *reused* connection was dropped
    before any response byte arrived (a normal keep-alive expiry), and it says
    so on stderr when it does.
    """

    def __init__(self, host=API_HOST, api_key=None, timeout=20.0, verbose=False):
        self.host = host
        self.api_key = api_key
        self.timeout = timeout
        self.verbose = verbose
        self.ctx = ssl.create_default_context()
        self.conn = None
        self.credits_used = 0
        self.calls = 0
        self.last_headers = {}
        self.last_wire_bytes = 0
        self.last_body_bytes = 0

    def _connect(self):
        self.conn = http.client.HTTPSConnection(self.host, timeout=self.timeout, context=self.ctx)

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def _headers(self):
        h = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": UA,
            "Connection": "keep-alive",
        }
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    def _raw_get(self, path):
        self.conn.request("GET", path, headers=self._headers())
        resp = self.conn.getresponse()
        body = resp.read()
        self.last_wire_bytes = len(body)
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        self.last_body_bytes = len(body)
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, headers, body

    def get(self, path, params=None, max_429_retries=4):
        """GET a path. Returns (parsed_json, elapsed_ms). Raises ApiError on 4xx/5xx."""
        if params:
            pairs = []
            for k, v in params.items():
                if v is None:
                    continue
                if isinstance(v, bool):
                    v = "true" if v else "false"
                pairs.append("%s=%s" % (k, _quote(str(v))))
            full = path + ("?" + "&".join(pairs) if pairs else "")
        else:
            full = path

        attempt_429 = 0
        while True:
            fresh = self.conn is None
            if fresh:
                self._connect()
            t0 = time.time()
            try:
                status, headers, body = self._raw_get(full)
            except (http.client.RemoteDisconnected, http.client.BadStatusLine,
                    ConnectionResetError, BrokenPipeError) as exc:
                self.close()
                if fresh:
                    # A brand new connection failed - that is a real error.
                    raise ApiError(0, "connection failed: %s" % exc)
                # A reused keep-alive connection expired before we sent. No
                # response was received, so reconnecting cannot mask a price move.
                if self.verbose:
                    sys.stderr.write("note: keep-alive connection expired, reconnecting once\n")
                self._connect()
                t0 = time.time()
                status, headers, body = self._raw_get(full)

            elapsed_ms = (time.time() - t0) * 1000.0
            self.last_headers = headers

            if headers.get("connection", "").lower() == "close":
                self.close()

            if status == 429:
                if attempt_429 >= max_429_retries:
                    raise ApiError(429, _title(body, "rate limited"), type_url="rate-limit")
                backoff = (2 ** attempt_429) * 3
                try:
                    wait = float(headers.get("retry-after", ""))
                except (TypeError, ValueError):
                    wait = backoff          # absent, or an HTTP-date we will not parse
                wait = max(1.0, min(wait, 60.0))
                sys.stderr.write("rate limited (429), waiting %.0fs\n" % wait)
                time.sleep(wait)
                attempt_429 += 1
                continue

            if status >= 400:
                raise ApiError(status, _title(body, "request failed"),
                               detail=_field(body, "detail"), type_url=_field(body, "type"))

            self.calls += 1
            cost = headers.get("x-credits-cost")
            try:
                self.credits_used += int(cost)
            except (TypeError, ValueError):
                self.credits_used += COST.get(path, 0)

            try:
                return json.loads(body.decode("utf-8")), elapsed_ms
            except ValueError as exc:
                raise ApiError(status, "response was not JSON: %s" % exc)

    def credits_line(self):
        rem = self.last_headers.get("x-credits-remaining")
        base = "%d call%s, %d credit%s" % (self.calls, "" if self.calls == 1 else "s",
                                           self.credits_used,
                                           "" if self.credits_used == 1 else "s")
        if rem:
            base += " (remaining: %s)" % rem
        return base


def _quote(s):
    safe = "-_.~"
    out = []
    for ch in s:
        if ch.isalnum() or ch in safe:
            out.append(ch)
        elif ch == " ":
            out.append("+")
        else:
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
    return "".join(out)


def _title(body, fallback):
    try:
        return json.loads(body.decode("utf-8")).get("title", fallback)
    except Exception:
        return fallback


def _field(body, key):
    try:
        return json.loads(body.decode("utf-8")).get(key)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Normalisation
#
# Both data sources land in the same shape so one analysis engine serves all
# three modes:
#
#   Race   = {race_id, venue, race_number, category, country, start_time,
#             source, runners: [Runner]}
#   Runner = {name, number, books: {book_key: Book}}
#   Book   = {key, open_price, close_price, high, low, move_pct, points_count,
#             open_secs_to_jump, close_secs_to_jump, open_is_baseline, points}
#   Point  = {secs_to_jump, win_price, captured_at}
# --------------------------------------------------------------------------


def parse_iso(s):
    """Parse the API's ISO-8601 timestamps. Python 3.9 will not take a 'Z'."""
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1]
    if "." in s:
        head, frac = s.split(".", 1)
        digits = ""
        i = 0
        while i < len(frac) and frac[i].isdigit():
            digits += frac[i]
            i += 1
        s = head + "." + digits[:6] + frac[i:]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if n == 0:
        return None
    m = n // 2
    return xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2.0


def race_from_price_history(doc):
    """Normalise a /v1/racing/price-history response."""
    runners = []
    for r in doc.get("runners") or []:
        books = {}
        for b in r.get("bookmakers") or []:
            pts = []
            for p in b.get("points") or []:
                if p.get("win_price") is None:
                    continue
                pts.append({
                    "secs_to_jump": p.get("secs_to_jump"),
                    "win_price": float(p["win_price"]),
                    "captured_at": p.get("captured_at"),
                })
            pts.sort(key=lambda p: -(p["secs_to_jump"] or 0))
            books[b["key"]] = {
                "key": b["key"],
                "open_price": b.get("open_price"),
                "close_price": b.get("close_price"),
                "high": b.get("high"),
                "low": b.get("low"),
                "move_pct": b.get("move_pct"),
                "points_count": b.get("points_count", len(pts)),
                "open_secs_to_jump": b.get("open_secs_to_jump"),
                "close_secs_to_jump": b.get("close_secs_to_jump"),
                "open_is_baseline": b.get("open_is_baseline"),
                "points": pts,
            }
        runners.append({"name": r.get("name"), "number": r.get("number"), "books": books})
    return {
        "race_id": doc.get("race_id"),
        "venue": doc.get("venue"),
        "race_number": doc.get("race_number"),
        "category": doc.get("category"),
        "country": doc.get("country"),
        "start_time": doc.get("start_time"),
        "source": "price-history",
        "points_returned": doc.get("points_returned"),
        "truncated": doc.get("truncated"),
        "history_from": doc.get("history_from"),
        "runners": runners,
    }


def recompute_book_stats(book):
    """Derive open/close/high/low/move_pct from collected points (watch mode)."""
    pts = book["points"]
    if not pts:
        return book
    prices = [p["win_price"] for p in pts]
    book["open_price"] = prices[0]
    book["close_price"] = prices[-1]
    book["high"] = max(prices)
    book["low"] = min(prices)
    book["points_count"] = len(pts)
    book["open_secs_to_jump"] = pts[0]["secs_to_jump"]
    book["close_secs_to_jump"] = book.get("last_seen_secs", pts[-1]["secs_to_jump"])
    if prices[0]:
        book["move_pct"] = round((prices[-1] - prices[0]) / prices[0] * 100.0, 2)
    else:
        book["move_pct"] = None
    return book


def merge_demo_snapshot(race, snap, now):
    """
    Fold one demo /v1/demo/racing/next-to-go race object into an accumulating Race.

    The demo endpoint returns a point-in-time snapshot, not history. Polling it
    on an interval is how this mode builds a series: every poll appends one
    point per runner-book, but only when the price actually changed, so a flat
    market does not inflate points_count.
    """
    start = parse_iso(snap.get("start_time"))
    secs = int((start - now).total_seconds()) if start else None

    by_number = {r["number"]: r for r in race["runners"]}
    for sr in snap.get("runners") or []:
        num = sr.get("number")
        runner = by_number.get(num)
        if runner is None:
            runner = {"name": sr.get("name"), "number": num, "books": {}}
            race["runners"].append(runner)
            by_number[num] = runner
        for sb in sr.get("bookmakers") or []:
            price = sb.get("win_price")
            if price is None:
                continue
            price = float(price)
            book = runner["books"].get(sb["key"])
            if book is None:
                book = {
                    "key": sb["key"], "open_price": None, "close_price": None,
                    "high": None, "low": None, "move_pct": None, "points_count": 0,
                    "open_secs_to_jump": None, "close_secs_to_jump": None,
                    "open_is_baseline": True, "points": [],
                }
                runner["books"][sb["key"]] = book
            book["last_seen_secs"] = secs
            if book["points"] and book["points"][-1]["win_price"] == price:
                # Unchanged. Count the hold; do NOT move the point's timestamp -
                # secs_to_jump must stay the moment this price was FIRST seen or
                # the step chart draws every transition late.
                book["points"][-1]["held"] = book["points"][-1].get("held", 1) + 1
            else:
                book["points"].append({
                    "secs_to_jump": secs,
                    "win_price": price,
                    "captured_at": now.isoformat().replace("+00:00", "Z"),
                })
            recompute_book_stats(book)
    return race


def new_demo_race(snap):
    return {
        "race_id": snap.get("race_id"),
        "venue": snap.get("venue"),
        "race_number": snap.get("race_number"),
        "category": snap.get("category"),
        "country": snap.get("country"),
        "start_time": snap.get("start_time"),
        "source": "demo-poll",
        "points_returned": None,
        "truncated": False,
        "history_from": None,
        "runners": [],
    }


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


def runner_summary(runner):
    """
    Collapse a runner's per-book records into one consensus row.

    Consensus open and close are the MEDIAN across books, not the mean: one
    book posting an outlier price should not drag the row. High and low are the
    extremes across every book, because that is the honest range the market
    actually showed.

    'thin' counts books holding a single observation. Such a book reports a
    0.0% move by construction - it never had a second price to move to - so a
    row with many thin books is weaker evidence than its move_pct suggests.
    """
    books = list(runner["books"].values())
    live = [b for b in books if b.get("open_price") and b.get("close_price")]
    if not live:
        return None
    opens = [b["open_price"] for b in live]
    closes = [b["close_price"] for b in live]
    highs = [b["high"] for b in live if b.get("high") is not None]
    lows = [b["low"] for b in live if b.get("low") is not None]

    o = _median(opens)
    c = _median(closes)
    move = ((c - o) / o * 100.0) if o else None
    thin = sum(1 for b in live if (b.get("points_count") or 0) <= 1)

    if move is None:
        direction = "unknown"
    elif move <= -1.0:
        direction = "firming"
    elif move >= 1.0:
        direction = "drifting"
    else:
        direction = "steady"

    return {
        "number": runner["number"],
        "name": runner["name"],
        "books": len(live),
        "thin_books": thin,
        "open": o,
        "close": c,
        "high": max(highs) if highs else None,
        "low": min(lows) if lows else None,
        "move_pct": move,
        "direction": direction,
        "total_points": sum((b.get("points_count") or 0) for b in live),
    }


def race_span(race):
    """(max_secs_to_jump, min_secs_to_jump) across every point in the race."""
    secs = []
    for r in race["runners"]:
        for b in r["books"].values():
            for p in b["points"]:
                if p.get("secs_to_jump") is not None:
                    secs.append(p["secs_to_jump"])
            if b.get("close_secs_to_jump") is not None:
                secs.append(b["close_secs_to_jump"])
    if not secs:
        return None
    return max(secs), min(secs)


def consensus_path(runner, nbuckets, span):
    """
    Median price across books, bucketed by seconds-to-jump.

    Books sample at different moments, so a naive time-ordered merge would
    zig-zag purely from which book happened to report last. Bucketing to a
    shared grid and taking the median per bucket removes that artefact. Empty
    buckets are filled forward (a price holds until it changes), then backward
    for any leading gap before the runner's first observation.
    """
    if not span:
        return []
    smax, smin = span
    width = max(1.0, float(smax - smin))
    buckets = [[] for _ in range(nbuckets)]
    for b in runner["books"].values():
        for p in b["points"]:
            s = p.get("secs_to_jump")
            if s is None:
                continue
            frac = (smax - s) / width
            idx = int(frac * (nbuckets - 1) + 0.5)
            idx = min(nbuckets - 1, max(0, idx))
            buckets[idx].append(p["win_price"])
    series = [_median(vals) for vals in buckets]
    if all(v is None for v in series):
        return []
    last = None
    for i, v in enumerate(series):
        if v is None:
            series[i] = last
        else:
            last = v
    first = next(v for v in series if v is not None)
    for i, v in enumerate(series):
        if v is None:
            series[i] = first
        else:
            break
    return series


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def sparkline(series, ascii_mode=False):
    chars = ASCII_BLOCKS if ascii_mode else BLOCKS
    vals = [v for v in series if v is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return chars[len(chars) // 2] * len(series)
    out = []
    for v in series:
        if v is None:
            out.append(" ")
            continue
        idx = int((v - lo) / (hi - lo) * (len(chars) - 1) + 0.5)
        out.append(chars[min(len(chars) - 1, max(0, idx))])
    return "".join(out)


def fmt_price(p):
    return "%6.2f" % p if p is not None else "     -"


def fmt_secs(s):
    if s is None:
        return "?"
    if s <= 30:
        return "jump"
    if s < 3600:
        return "%dm" % round(s / 60.0)
    return "%.1fh" % (s / 3600.0)


def fmt_out(s):
    """Time-before-jump as a duration. Unlike fmt_secs it never says 'jump'."""
    if s is None:
        return "?"
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % round(s / 60.0)
    return "%.1fh" % (s / 3600.0)


def race_heading(race):
    lines = []
    bits = [race.get("venue") or "?", "R%s" % race.get("race_number")]
    meta = [x for x in (race.get("category"), race.get("country")) if x]
    head = " ".join(bits)
    if meta:
        head += "  (%s)" % ", ".join(meta)
    lines.append(head)
    lines.append("race_id   %s" % race.get("race_id"))
    lines.append("jump      %s" % race.get("start_time"))
    span = race_span(race)
    if span:
        lines.append("window    from %s to %s before the jump  (%s observed)"
                     % (fmt_out(span[0]), fmt_out(span[1]), fmt_out(span[0] - span[1])))
    src = {"price-history": "/v1/racing/price-history (keyed, 5 credits)",
           "demo-poll": "/v1/demo/racing/next-to-go (keyless, 0 credits, polled by this tool)"}
    lines.append("source    %s" % src.get(race["source"], race["source"]))
    if race.get("points_returned") is not None:
        lines.append("points    %s%s" % (race["points_returned"],
                                         "  (TRUNCATED - raise --max-points)" if race.get("truncated") else ""))
    return "\n".join(lines)


def is_thin(s):
    """True when at least half this runner's books posted a single price only."""
    return bool(s["books"]) and s["thin_books"] >= s["books"] / 2.0


def render_table(summaries, paths, ascii_mode, path_label="Path (open -> jump)"):
    """Fixed-width per-runner open/close/high/low/move_pct table."""
    name_w = max([12] + [len(s["name"] or "") for s in summaries])
    name_w = min(name_w, 24)
    head = ("%-3s %-*s %3s  %6s %6s %6s %6s  %7s  %-8s %s"
            % ("#", name_w, "Runner", "Bk", "Open", "Close", "High", "Low",
               "Move%", "Dir", path_label))
    rows = [head, "-" * (len(head) + 22)]
    for s in summaries:
        nm = (s["name"] or "")[:name_w]
        mv = "%+7.2f" % s["move_pct"] if s["move_pct"] is not None else "      -"
        spark = sparkline(paths.get(s["number"], []), ascii_mode)
        flag = "*" if is_thin(s) else " "
        rows.append("%-3s %-*s %3d%s %6s %6s %6s %6s  %7s  %-8s %s"
                    % (s["number"], name_w, nm, s["books"], flag,
                       fmt_price(s["open"]), fmt_price(s["close"]),
                       fmt_price(s["high"]), fmt_price(s["low"]),
                       mv, s["direction"], spark))
    return "\n".join(rows)


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


PALETTE = ["#1f6fb4", "#c0392b", "#1e8449", "#7d3c98",
           "#d97706", "#0e7490", "#7f4f24", "#b03a7a"]

NICE_ODDS = [1.1, 1.2, 1.35, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 5.0, 6.5, 8.0,
             10.0, 13.0, 17.0, 21.0, 26.0, 34.0, 51.0, 67.0, 101.0, 151.0, 201.0]


def render_svg(race, summaries, paths, span, top_n=6):
    """
    Hand-rolled SVG. No matplotlib, no dependency, no font files.

    The y axis is logarithmic. Odds are multiplicative - the distance from 2.00
    to 2.20 is the same 10% move as 10.00 to 11.00 - so a linear axis would
    flatten every short-priced runner into one indistinguishable band.
    """
    W, H = 980, 580
    ML, MR, MT, MB = 70, 214, 66, 74
    PW, PH = W - ML - MR, H - MT - MB

    drawable = [s for s in summaries if paths.get(s["number"])]
    solid = sorted((s for s in drawable if not is_thin(s)),
                   key=lambda s: (s["close"] if s["close"] is not None else 9e9))
    thin = sorted((s for s in drawable if is_thin(s)),
                  key=lambda s: (s["close"] if s["close"] is not None else 9e9))
    chosen = (solid + thin)[:top_n]

    vals = []
    for s in chosen:
        vals.extend(v for v in paths[s["number"]] if v is not None)
    if not vals:
        return None
    ymin, ymax = min(vals), max(vals)
    if ymax / max(ymin, 0.01) < 1.15:
        ymin, ymax = ymin * 0.94, ymax * 1.06
    lo, hi = math.log(ymin * 0.96), math.log(ymax * 1.04)

    def ypx(p):
        return MT + PH - (math.log(p) - lo) / (hi - lo) * PH

    n = max(len(paths[chosen[0]["number"]]), 2)

    def xpx(i):
        return ML + (i / float(n - 1)) * PW

    o = []
    o.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
             'viewBox="0 0 %d %d" font-family="-apple-system,BlinkMacSystemFont,'
             '&quot;Segoe UI&quot;,Helvetica,Arial,sans-serif">' % (W, H, W, H))
    o.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))

    title = "%s R%s - consensus price path" % (race.get("venue") or "?", race.get("race_number"))
    sub = "%s  ·  jump %s  ·  median across %d bookmakers  ·  log price axis" % (
        ", ".join(x for x in (race.get("category"), race.get("country")) if x),
        race.get("start_time") or "?",
        max((s["books"] for s in chosen), default=0))
    o.append('<text x="%d" y="30" font-size="17" font-weight="600" fill="#111827">%s</text>'
             % (ML - 22, _esc(title)))
    o.append('<text x="%d" y="49" font-size="11.5" fill="#6b7280">%s</text>'
             % (ML - 22, _esc(sub)))

    o.append('<rect x="%d" y="%d" width="%d" height="%d" fill="#fcfcfd" stroke="#e5e7eb"/>'
             % (ML, MT, PW, PH))

    for tick in NICE_ODDS:
        if not (ymin * 0.96 <= tick <= ymax * 1.04):
            continue
        y = ypx(tick)
        o.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#eceef1"/>'
                 % (ML, y, ML + PW, y))
        o.append('<text x="%d" y="%.1f" font-size="10.5" fill="#6b7280" text-anchor="end" '
                 'font-family="ui-monospace,SFMono-Regular,Menlo,monospace">%s</text>'
                 % (ML - 8, y + 3.5, ("%.2f" % tick).rstrip("0").rstrip(".")))

    if span:
        smax, smin = span
        for k in range(7):
            i = int(k / 6.0 * (n - 1))
            x = xpx(i)
            s = smax - (i / float(n - 1)) * (smax - smin)
            o.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="#eceef1"/>'
                     % (x, MT, x, MT + PH))
            o.append('<text x="%.1f" y="%d" font-size="10.5" fill="#6b7280" '
                     'text-anchor="middle">%s</text>' % (x, MT + PH + 18, _esc(fmt_secs(s))))
    o.append('<text x="%.1f" y="%d" font-size="11" fill="#374151" text-anchor="middle">'
             'time before jump</text>' % (ML + PW / 2.0, MT + PH + 38))
    o.append('<text x="18" y="%.1f" font-size="11" fill="#374151" text-anchor="middle" '
             'transform="rotate(-90 18 %.1f)">win price (log)</text>'
             % (MT + PH / 2.0, MT + PH / 2.0))

    for idx, s in enumerate(chosen):
        col = PALETTE[idx % len(PALETTE)]
        pts = paths[s["number"]]
        d = []
        for i, v in enumerate(pts):
            if v is None:
                continue
            d.append("%s%.1f,%.1f" % ("M" if not d else "L", xpx(i), ypx(v)))
        if not d:
            continue
        o.append('<path d="%s" fill="none" stroke="%s" stroke-width="2.1" '
                 'stroke-linejoin="round" stroke-linecap="round"/>' % (" ".join(d), col))
        last = [(i, v) for i, v in enumerate(pts) if v is not None][-1]
        o.append('<circle cx="%.1f" cy="%.1f" r="3" fill="%s"/>'
                 % (xpx(last[0]), ypx(last[1]), col))

    lx = ML + PW + 20
    o.append('<text x="%d" y="%d" font-size="11" font-weight="600" fill="#374151">'
             'runner (open -&gt; close)</text>' % (lx, MT + 2))
    for idx, s in enumerate(chosen):
        col = PALETTE[idx % len(PALETTE)]
        y = MT + 24 + idx * 34
        o.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="2.6" '
                 'stroke-linecap="round"/>' % (lx, y, lx + 18, y, col))
        o.append('<text x="%d" y="%d" font-size="11.5" fill="#111827">%s</text>'
                 % (lx + 25, y + 4, _esc("%s. %s" % (s["number"], (s["name"] or "")[:17]))))
        mv = "%+.1f%%" % s["move_pct"] if s["move_pct"] is not None else "-"
        detail = "%.2f -> %.2f  %s" % (s["open"], s["close"], mv)
        mc = "#b91c1c" if (s["move_pct"] or 0) > 1 else ("#15803d" if (s["move_pct"] or 0) < -1 else "#6b7280")
        o.append('<text x="%d" y="%d" font-size="10.5" fill="%s" '
                 'font-family="ui-monospace,SFMono-Regular,Menlo,monospace">%s</text>'
                 % (lx + 25, y + 18, mc, _esc(detail)))

    o.append('<text x="%d" y="%d" font-size="10" fill="#9ca3af">%s</text>'
             % (ML - 22, H - 15, _esc(
                 "Prices from PuntersEdge %s. Chart generated %s. "
                 "A record of what bookmakers did - not a prediction."
                 % ("/v1/racing/price-history" if race["source"] == "price-history"
                    else "/v1/demo/racing/next-to-go",
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")))))
    o.append("</svg>")
    return "\n".join(o)


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


def _find_runner(race, number):
    for r in race["runners"]:
        if r["number"] == number:
            return r
    return None


def _count_points(race):
    return sum(len(b["points"]) for r in race["runners"] for b in r["books"].values())


SORTS = {
    "move": lambda s: -abs(s["move_pct"] or 0),
    "firming": lambda s: (s["move_pct"] if s["move_pct"] is not None else 0),
    "drifting": lambda s: -(s["move_pct"] if s["move_pct"] is not None else 0),
    "price": lambda s: (s["close"] if s["close"] is not None else 9e9),
    "number": lambda s: (s["number"] if s["number"] is not None else 9999),
}


def emit_race(race, args):
    summaries = [x for x in (runner_summary(r) for r in race["runners"]) if x]
    if not summaries:
        print("No runner had two usable prices - nothing to summarise.")
        return
    span = race_span(race)
    spark = {s["number"]: consensus_path(_find_runner(race, s["number"]), args.buckets, span)
             for s in summaries}
    summaries.sort(key=SORTS[args.sort])
    if args.top:
        summaries = summaries[:args.top]

    print()
    print(race_heading(race))
    print()
    path_label = ("Path (open -> jump)" if race["source"] == "price-history"
                  else "Path (polled window)")
    print(render_table(summaries, spark, args.ascii, path_label))
    print()
    thin = sum(1 for s in summaries if is_thin(s))
    if thin:
        print("* = at least half this runner's books posted a single price only. A book with one")
        print("    observation reports a 0.00% move by construction, so it neither confirms nor")
        print("    denies a move. %d of %d rows are flagged." % (thin, len(summaries)))
        print()
    print("Move% is the consensus (median across books) close vs open. Negative = firmed")
    print("(price shortened), positive = drifted (price lengthened).")

    if args.svg:
        fine = {s["number"]: consensus_path(_find_runner(race, s["number"]), 120, span)
                for s in summaries}
        svg = render_svg(race, summaries, fine, span, top_n=args.svg_runners)
        if svg:
            with open(args.svg, "w") as fh:
                fh.write(svg)
            print("\nSVG chart written to %s" % args.svg)
        else:
            print("\nNot enough data to draw an SVG chart.")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(race, fh, indent=1)
        print("Normalised series written to %s" % args.json)


def cmd_race(args):
    key = os.environ.get("PE_API_KEY")
    if not key:
        sys.stderr.write(
            "PE_API_KEY is not set. This mode reads /v1/racing/price-history, which needs a key.\n"
            "Free key (1,500 credits/month, no card): %s\n"
            "To try the tool with no key at all, run:  python3 pricemove.py watch\n" % SIGNUP)
        return 2
    if args.max_points < 100:
        sys.stderr.write("--max-points must be at least 100.\n")
        return 2

    client = Client(api_key=key, verbose=args.verbose)
    params = {"max_points": args.max_points, "include_points": True}
    if args.race_id:
        params["race_id"] = args.race_id
    elif args.venue and args.race_number and args.date:
        params.update({"venue": args.venue, "race_number": args.race_number, "date": args.date})
    else:
        sys.stderr.write("Give --race-id, or all of --venue --race-number --date.\n")
        return 2
    if args.books:
        params["bookmakers"] = args.books

    try:
        doc, ms = client.get("/v1/racing/price-history", params)
    except ApiError as exc:
        sys.stderr.write("%s\n" % exc)
        return 1

    race = race_from_price_history(doc)
    print("fetched in %.0f ms  ·  %s" % (ms, client.credits_line()))
    emit_race(race, args)
    client.close()
    return 0


def cmd_scan(args):
    key = os.environ.get("PE_API_KEY")
    if not key:
        sys.stderr.write(
            "PE_API_KEY is not set. This mode reads /v1/racing/movers, which needs a key.\n"
            "Free key (1,500 credits/month, no card): %s\n"
            "To try the tool with no key at all, run:  python3 pricemove.py watch\n" % SIGNUP)
        return 2

    client = Client(api_key=key, verbose=args.verbose)
    params = {
        "min_move_pct": args.min_move_pct,
        "min_books": args.min_books,
        "max_mins_to_jump": args.max_mins_to_jump,
        "limit": args.limit,
    }
    if args.country:
        params["country"] = args.country
    if args.include_unresolved:
        params["include_unresolved"] = True
    if args.direction:
        params["direction"] = args.direction
    if args.categories:
        params["categories"] = args.categories

    if args.repeat > 1:
        print("%d polls x 3 credits = %d credits, one every %ds.\n"
              % (args.repeat, args.repeat * 3, args.interval))

    for poll in range(args.repeat):
        try:
            rows, ms = client.get("/v1/racing/movers", params)
        except ApiError as exc:
            sys.stderr.write("%s\n" % exc)
            return 1
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
        print("[%s] %d mover%s  ·  %.0f ms  ·  %s"
              % (stamp, len(rows), "" if len(rows) == 1 else "s", ms, client.credits_line()))
        if rows:
            print(render_movers(rows))
        elif args.country and not args.include_unresolved:
            print("  No rows. country=%s alone excludes races whose meeting is not confirmed yet,"
                  % args.country)
            print("  which is most of an Australian card until close to the jump. Retry with")
            print("  --include-unresolved, or lower --min-move-pct / --min-books.")
        else:
            print("  No mover cleared min_move_pct=%s across min_books=%s within %s minutes."
                  % (args.min_move_pct, args.min_books, args.max_mins_to_jump))
        sys.stdout.flush()
        if poll < args.repeat - 1:
            print()
            time.sleep(args.interval)
    client.close()
    return 0


def render_movers(rows):
    name_w = min(22, max([10] + [len(r.get("runner") or "") for r in rows]))
    ven_w = min(18, max([5] + [len(r.get("venue") or "") for r in rows]))
    head = ("  %-*s %-4s %-9s %5s  %-3s %-*s %-8s %6s %6s %8s  %s"
            % (ven_w, "Venue", "Race", "Category", "Jump", "#", name_w, "Runner",
               "Dir", "Open", "Now", "Move%", "f/d/u"))
    out = [head, "  " + "-" * (len(head) - 2)]
    for r in rows:
        out.append("  %-*s %-4s %-9s %5s  %-3s %-*s %-8s %6.2f %6.2f %+8.2f  %d/%d/%d"
                   % (ven_w, (r.get("venue") or "?")[:ven_w], "R%s" % r.get("race_number"),
                      (r.get("category") or "?")[:9],
                      "%dm" % r["mins_to_jump"] if r.get("mins_to_jump") is not None else "?",
                      r.get("number"), name_w, (r.get("runner") or "")[:name_w],
                      r.get("direction") or "?",
                      r.get("open_price") or 0, r.get("current_price") or 0,
                      r.get("move_pct") or 0,
                      r.get("books_firming") or 0, r.get("books_drifting") or 0,
                      r.get("books_unchanged") or 0))
    return "\n".join(out)


def cmd_benchmark(args):
    """
    Reproduce the latency and gzip figures quoted in the README.

    Measures the same request two ways: a fresh TLS connection every call, and
    one connection held open. Reports the median of each.
    """
    key = os.environ.get("PE_API_KEY")
    if not key:
        sys.stderr.write("PE_API_KEY is not set; benchmark uses /v1/racing/price-history.\n")
        return 2
    path = "/v1/racing/price-history"
    params = {"race_id": args.race_id, "max_points": 5000, "include_points": True}
    n = max(3, args.n)

    print("endpoint  %s" % path)
    print("race_id   %s" % args.race_id)
    print("samples   %d per mode\n" % n)

    cold = []
    for _ in range(n):
        c = Client(api_key=key)
        try:
            _, ms = c.get(path, params)
        except ApiError as exc:
            sys.stderr.write("%s\n" % exc)
            return 1
        cold.append(ms)
        c.close()

    c = Client(api_key=key)
    _, _first = c.get(path, params)          # pay the handshake once, then reuse
    warm = [c.get(path, params)[1] for _ in range(n)]
    wire, body = c.last_wire_bytes, c.last_body_bytes
    c.close()

    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2.0

    print("new TLS connection per call   %s  median %6.1f ms"
          % (" ".join("%4.0f" % x for x in cold), med(cold)))
    print("one connection reused         %s  median %6.1f ms"
          % (" ".join("%4.0f" % x for x in warm), med(warm)))
    print("\nhandshake paid once on the reused client: %.0f ms" % _first)
    if wire and body:
        print("gzip: %s bytes on the wire -> %s bytes decompressed (%.1fx)"
              % ("{:,}".format(wire), "{:,}".format(body), float(body) / wire))
    print("\nThis run: %s" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    return 0


def cmd_render(args):
    """
    Re-render a series saved earlier with --json. No network, no key, no credits.

    Price history costs 5 credits a race, so re-running a fetch just to retry a
    chart is wasteful. Save the normalised series once, then iterate on the
    table and the chart offline. The sample committed in docs/ is a real race
    captured with this tool.
    """
    try:
        with open(args.path) as fh:
            race = json.load(fh)
    except (IOError, ValueError) as exc:
        sys.stderr.write("could not read %s: %s\n" % (args.path, exc))
        return 2
    if not isinstance(race, dict) or "runners" not in race:
        sys.stderr.write("%s is not a series saved by --json\n" % args.path)
        return 2
    race.setdefault("source", "price-history")
    emit_race(race, args)
    return 0


def cmd_watch(args):
    """Keyless. Build a price series by polling the free demo endpoint."""
    interval = max(5, args.interval)
    client = Client(api_key=None, verbose=args.verbose)
    deadline = time.time() + args.minutes * 60.0
    race = None
    poll = 0

    print("Polling https://%s/v1/demo/racing/next-to-go every %ds for %g minute(s)."
          % (API_HOST, interval, args.minutes))
    print("No API key, 0 credits. The demo endpoint allows 30 requests/min per IP.")
    print("Ctrl-C stops early and still prints the table.\n")

    try:
        while time.time() < deadline:
            try:
                doc, ms = client.get("/v1/demo/racing/next-to-go")
            except ApiError as exc:
                sys.stderr.write("%s\n" % exc)
                if race is None:
                    return 1
                break

            races = doc.get("races") or []
            if not races:
                sys.stderr.write("demo returned no races; stopping\n")
                break
            if poll == 0:
                print("demo note: %s\n" % doc.get("note", ""))

            if race is None:
                idx = min(max(0, args.race_index), len(races) - 1)
                race = new_demo_race(races[idx])
                snap = races[idx]
            else:
                snap = next((r for r in races if r.get("race_id") == race["race_id"]), None)
                if snap is None:
                    print("tracked race left the demo window (it has jumped) - stopping early")
                    break

            before = _count_points(race)
            merge_demo_snapshot(race, snap, datetime.now(timezone.utc))
            added = _count_points(race) - before
            poll += 1

            start = parse_iso(snap.get("start_time"))
            tt = fmt_secs(int((start - datetime.now(timezone.utc)).total_seconds())) if start else "?"
            plural = "" if added == 1 else "s"
            what = ("%d price%s recorded" % (added, plural) if poll == 1
                    else "%d price change%s" % (added, plural))
            print("poll %-3d %s  %s R%s  %s to jump  ·  %s  ·  %.0f ms"
                  % (poll, datetime.now(timezone.utc).strftime("%H:%M:%SZ"),
                     snap.get("venue"), snap.get("race_number"), tt, what, ms))
            sys.stdout.flush()

            if time.time() + interval >= deadline:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\ninterrupted - summarising what was collected")

    client.close()
    if race is None:
        return 1
    print("\n%d poll(s), %d point(s) collected." % (poll, _count_points(race)))
    emit_race(race, args)
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="pricemove.py",
        description="Track how Australian racing prices move between market open and the jump.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Free API key (1,500 credits/month, no card): %s" % SIGNUP)
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--buckets", type=int, default=40,
                        help="sparkline resolution (default 40)")
        sp.add_argument("--ascii", action="store_true",
                        help="ASCII sparkline instead of Unicode blocks")
        sp.add_argument("--sort", choices=sorted(SORTS), default="move",
                        help="row order (default: move, largest absolute move first)")
        sp.add_argument("--top", type=int, default=0, help="show only the first N rows")
        sp.add_argument("--svg", metavar="PATH", help="write a standalone SVG chart")
        sp.add_argument("--svg-runners", type=int, default=6,
                        help="runners to draw on the chart, shortest-priced first (default 6)")
        sp.add_argument("--json", metavar="PATH", help="write the normalised series as JSON")
        sp.add_argument("--verbose", action="store_true")

    w = sub.add_parser("watch", help="keyless: poll the demo endpoint and build a series")
    w.add_argument("--interval", type=int, default=20, help="seconds between polls (min 5, default 20)")
    w.add_argument("--minutes", type=float, default=5.0, help="how long to poll (default 5)")
    w.add_argument("--race-index", type=int, default=0,
                   help="which of the 3 demo races to follow (0 = next to go)")
    common(w)
    w.set_defaults(func=cmd_watch)

    b = sub.add_parser("benchmark",
                       help="reproduce the README's gzip / connection-reuse figures")
    b.add_argument("--race-id", default="b4b126d9-24a6-4121-9a92-b40dc389a74a",
                   help="race to fetch repeatedly (default: the race in docs/)")
    b.add_argument("--n", type=int, default=5, help="samples per mode (default 5)")
    b.set_defaults(func=cmd_benchmark)

    d = sub.add_parser("render", help="offline: re-render a series saved with --json")
    d.add_argument("path", help="a JSON file written by --json")
    common(d)
    d.set_defaults(func=cmd_render)

    s = sub.add_parser("scan", help="keyed: read /v1/racing/movers (3 credits per poll)")
    s.add_argument("--country", default="AU", help="ISO codes, comma separated (default AU)")
    s.add_argument("--include-unresolved", action="store_true",
                   help="also include races whose meeting is not confirmed yet - see README")
    s.add_argument("--min-move-pct", type=float, default=10.0, help="1-90, default 10")
    s.add_argument("--min-books", type=int, default=3, help="1-12, default 3")
    s.add_argument("--direction", choices=["firming", "drifting"], help="default: both")
    s.add_argument("--categories", help="horse,harness,greyhound (default: all)")
    s.add_argument("--max-mins-to-jump", type=int, default=360, help="5-360, default 360")
    s.add_argument("--limit", type=int, default=50, help="1-200, default 50")
    s.add_argument("--repeat", type=int, default=1, help="poll this many times")
    s.add_argument("--interval", type=int, default=120, help="seconds between polls (default 120)")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("race", help="keyed: /v1/racing/price-history for one race (5 credits)")
    r.add_argument("--race-id")
    r.add_argument("--venue")
    r.add_argument("--race-number", type=int)
    r.add_argument("--date", help="YYYY-MM-DD (UTC)")
    r.add_argument("--books", help="comma-separated bookmaker keys")
    r.add_argument("--max-points", type=int, default=5000, help="minimum 100, default 5000")
    common(r)
    r.set_defaults(func=cmd_race)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
