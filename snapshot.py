"""Daily Etsy research snapshot.

Steps each run:
  1. Make sure the seed shops and our own shop are in the shops table.
  2. Discovery: search each seed keyword, record rankings, snapshot the listings found.
  3. For every tracked shop: snapshot shop totals, all active listings, new reviews.
  4. Fill in any missing views via the batch listing endpoint.
  5. Classify listings we have not seen before (fit / theme / subject / size).
  6. Start tracking shops that keep showing up with fitting listings.

Env vars: ETSY_KEYSTRING, ETSY_SHARED_SECRET, DATABASE_URL, ANTHROPIC_API_KEY
Usage:   python snapshot.py               full run
         python snapshot.py --shops-only  skip keyword discovery
"""
import datetime as dt
import hashlib
import json
import os
import sys
import time

import psycopg
import requests

BASE = "https://openapi.etsy.com/v3/application"
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))
TODAY = dt.date.today()

# Listing text held in memory for this run only (for classification), never stored.
TEXT = {}


# ---------------------------------------------------------------- Etsy client
class BudgetExceeded(Exception):
    pass


class Etsy:
    def __init__(self):
        key = os.environ["ETSY_KEYSTRING"]
        secret = os.environ.get("ETSY_SHARED_SECRET")
        self.s = requests.Session()
        # Etsy now expects "keystring:shared_secret"; falls back to keystring only.
        self.s.headers["x-api-key"] = f"{key}:{secret}" if secret else key
        self.calls = 0
        self.budget = CFG.get("max_calls_per_run", 4000)

    def get(self, path, **params):
        if self.calls >= self.budget:
            raise BudgetExceeded(f"stopped at {self.calls} calls (max_calls_per_run)")
        for attempt in range(6):
            r = self.s.get(BASE + path, params=params, timeout=30)
            self.calls += 1
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            time.sleep(0.25)  # Etsy limit is 5 calls/sec; stay at ~4
            return r.json()
        r.raise_for_status()

    def paged(self, path, max_pages=50, **params):
        offset = 0
        for _ in range(max_pages):
            data = self.get(path, limit=100, offset=offset, **params) or {}
            results = data.get("results", [])
            yield from results
            offset += len(results)
            if len(results) < 100 or offset >= data.get("count", 0):
                break


# ---------------------------------------------------------------- parsing
def ts(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc) if epoch else None


def parse_listing(l):
    p = l.get("price") or {}
    price = p["amount"] / p["divisor"] if p.get("divisor") else None
    if "is_digital" in l:
        digital = l["is_digital"]
    else:
        lt = l.get("listing_type")
        digital = lt in ("download", "both") if lt else None
    TEXT[l["listing_id"]] = {
        "title": l.get("title", ""),
        "tags": (l.get("tags") or [])[:13],
        "price": price,
        "digital": digital,
    }
    return {
        "listing_id": l["listing_id"],
        "shop_id": l["shop_id"],
        "original_created_at": ts(l.get("original_creation_timestamp") or l.get("creation_timestamp")),
        "price": price,
        "currency": p.get("currency_code"),
        "is_digital": digital,
        "views": l.get("views"),
        "num_favorers": l.get("num_favorers"),
        "state": l.get("state"),
    }


# ---------------------------------------------------------------- db writes
def save_listings(conn, rows):
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """insert into listings (listing_id, shop_id, original_created_at, price, currency, is_digital, last_seen)
               values (%(listing_id)s, %(shop_id)s, %(original_created_at)s, %(price)s, %(currency)s, %(is_digital)s, %(today)s)
               on conflict (listing_id) do update set
                 price = excluded.price, currency = excluded.currency,
                 is_digital = excluded.is_digital, last_seen = excluded.last_seen""",
            [{**r, "today": TODAY} for r in rows],
        )
        cur.executemany(
            """insert into listing_snapshots (listing_id, snap_date, views, num_favorers, price, state)
               values (%(listing_id)s, %(today)s, %(views)s, %(num_favorers)s, %(price)s, %(state)s)
               on conflict (listing_id, snap_date) do update set
                 views = coalesce(excluded.views, listing_snapshots.views),
                 num_favorers = excluded.num_favorers, price = excluded.price, state = excluded.state""",
            [{**r, "today": TODAY} for r in rows],
        )
    conn.commit()


def ensure_shop(conn, etsy, name, source, is_own=False):
    row = conn.execute("select shop_id from shops where lower(shop_name) = lower(%s)", (name,)).fetchone()
    if row:
        return row[0]
    data = etsy.get("/shops", shop_name=name) or {}
    match = [s for s in data.get("results", []) if s["shop_name"].lower() == name.lower()]
    if not match:
        print(f"!! shop not found: {name}")
        return None
    sid = match[0]["shop_id"]
    conn.execute(
        """insert into shops (shop_id, shop_name, source, is_own) values (%s, %s, %s, %s)
           on conflict (shop_id) do update set is_own = shops.is_own or excluded.is_own""",
        (sid, match[0]["shop_name"], source, is_own),
    )
    conn.commit()
    return sid


# ---------------------------------------------------------------- steps
def discovery(conn, etsy):
    hits, rows = [], {}
    for kw in CFG["keywords"]:
        rank = 0
        for l in etsy.paged("/listings/active", max_pages=CFG["pages_per_keyword"], keywords=kw, sort_on="score"):
            rank += 1
            row = parse_listing(l)
            rows[row["listing_id"]] = row
            hits.append((kw, TODAY, row["listing_id"], row["shop_id"], rank))
        print(f"  search '{kw}': {rank} listings")
    save_listings(conn, list(rows.values()))
    with conn.cursor() as cur:
        cur.executemany(
            "insert into search_hits values (%s,%s,%s,%s,%s) on conflict do nothing", hits
        )
    conn.commit()


def snapshot_shop(conn, etsy, shop_id):
    s = etsy.get(f"/shops/{shop_id}")
    if not s:
        print(f"!! shop {shop_id} gone, untracking")
        conn.execute("update shops set tracked = false where shop_id = %s", (shop_id,))
        conn.commit()
        return
    conn.execute(
        """insert into shop_snapshots values (%s,%s,%s,%s,%s,%s,%s)
           on conflict (shop_id, snap_date) do update set
             transaction_sold_count = excluded.transaction_sold_count,
             review_count = excluded.review_count, review_average = excluded.review_average,
             num_favorers = excluded.num_favorers, listing_active_count = excluded.listing_active_count""",
        (shop_id, TODAY, s.get("transaction_sold_count"), s.get("review_count"),
         s.get("review_average"), s.get("num_favorers"), s.get("listing_active_count")),
    )
    conn.commit()

    rows = [parse_listing(l) for l in etsy.paged(f"/shops/{shop_id}/listings/active")]
    save_listings(conn, rows)

    last = conn.execute("select max(created_at) from reviews where shop_id = %s", (shop_id,)).fetchone()[0]
    params = {"min_created": int(last.timestamp()) + 1} if last else {}
    revs = []
    for r in etsy.paged(f"/shops/{shop_id}/reviews", **params):
        created = r.get("create_timestamp") or r.get("created_timestamp")
        if not created:
            continue
        key = str(r.get("transaction_id") or hashlib.sha1(
            f"{r.get('listing_id')}|{created}|{r.get('buyer_user_id')}".encode()).hexdigest())
        revs.append((key, shop_id, r.get("listing_id"), r.get("rating"), ts(created)))
    with conn.cursor() as cur:
        cur.executemany("insert into reviews values (%s,%s,%s,%s,%s) on conflict do nothing", revs)
    conn.commit()
    print(f"  shop {s.get('shop_name')}: {len(rows)} listings, {len(revs)} new reviews, "
          f"{s.get('transaction_sold_count')} lifetime sales")


def fill_missing_views(conn, etsy):
    ids = [r[0] for r in conn.execute(
        "select listing_id from listing_snapshots where snap_date = %s and views is null", (TODAY,))]
    for i in range(0, len(ids), 100):
        data = etsy.get("/listings/batch", listing_ids=",".join(map(str, ids[i:i + 100]))) or {}
        with conn.cursor() as cur:
            cur.executemany(
                "update listing_snapshots set views = %s where listing_id = %s and snap_date = %s",
                [(l.get("views"), l["listing_id"], TODAY) for l in data.get("results", [])],
            )
        conn.commit()
    if ids:
        print(f"  filled views for {len(ids)} listings")


def classify(conn):
    known = {r[0] for r in conn.execute("select listing_id from listing_class")}
    todo = [(lid, t) for lid, t in TEXT.items() if lid not in known]
    if not todo:
        return
    import anthropic

    client = anthropic.Anthropic()
    vocab = [r[0] for r in conn.execute(
        "select subject from listing_class where subject is not null "
        "group by subject order by count(*) desc limit 200")]
    for i in range(0, len(todo), 25):
        chunk = todo[i:i + 25]
        items = [{"id": lid, **t} for lid, t in chunk]
        prompt = f"""{CFG['fit_definition']}

Classify each Etsy listing below. Return ONLY a JSON array, one object per listing:
{{"id": <id>, "fit": "yes"|"maybe"|"no", "theme": one of ["christmas","halloween","easter","july4","thanksgiving","valentines","st_patricks","other_holiday","year_round"], "subject": short lowercase canonical noun phrase for what is depicted (e.g. "santa sleigh", "christmas tree", "nutcracker", "pumpkin family"), "size_ft": number or null, "licensed_ip": true|false, "reason": "<10 words"}}

Reuse an existing subject name whenever one fits, so the same product gets the same subject across shops. Existing subjects: {json.dumps(vocab)}

Listings:
{json.dumps(items)}"""
        msg = client.messages.create(
            model=CFG["classifier_model"], max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        try:
            out = json.loads(text[text.find("["): text.rfind("]") + 1])
        except ValueError:
            print(f"!! could not parse classifier output for batch {i}")
            continue
        with conn.cursor() as cur:
            cur.executemany(
                """insert into listing_class (listing_id, fit, theme, subject, size_ft, licensed_ip, reason, model)
                   values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (listing_id) do nothing""",
                [(o["id"], o.get("fit"), o.get("theme"), (o.get("subject") or "").strip().lower() or None,
                  o.get("size_ft"), o.get("licensed_ip"), o.get("reason"), CFG["classifier_model"])
                 for o in out if "id" in o],
            )
        conn.commit()
        vocab = list(dict.fromkeys(vocab + [o.get("subject") for o in out if o.get("subject")]))[:300]
    print(f"  classified {len(todo)} new listings")


def auto_track(conn, etsy):
    a = CFG["auto_track"]
    tracked = conn.execute("select count(*) from shops where tracked").fetchone()[0]
    room = min(a["max_new_per_run"], a["max_tracked_shops"] - tracked)
    if room <= 0:
        return
    cands = conn.execute(
        "select shop_id from candidate_shops where fit_listings >= %s limit %s",
        (a["min_fit_listings"], room)).fetchall()
    for (sid,) in cands:
        s = etsy.get(f"/shops/{sid}")
        if s:
            conn.execute("insert into shops (shop_id, shop_name, source) values (%s,%s,'discovered') "
                         "on conflict do nothing", (sid, s["shop_name"]))
            print(f"  now tracking {s['shop_name']}")
    conn.commit()


def main():
    etsy = Etsy()
    try:
        run(etsy)
    except BudgetExceeded as e:
        print(f"!! API budget reached: {e}. Lower keywords/pages or max_tracked_shops in config.json.")
    print(f"Done. Etsy API calls: {etsy.calls}")


def run(etsy):
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        print("Seeds")
        ensure_shop(conn, etsy, CFG["own_shop"], "seed", is_own=True)
        for name in CFG["seed_shops"]:
            ensure_shop(conn, etsy, name, "seed")

        if "--shops-only" not in sys.argv:
            print("Discovery")
            discovery(conn, etsy)

        print("Tracked shops")
        for (sid,) in conn.execute("select shop_id from shops where tracked order by shop_id").fetchall():
            try:
                snapshot_shop(conn, etsy, sid)
            except requests.HTTPError as e:
                conn.rollback()
                print(f"!! shop {sid} failed: {e}")

        print("Views")
        fill_missing_views(conn, etsy)
        print("Classify")
        classify(conn)
        print("Auto-track")
        auto_track(conn, etsy)


if __name__ == "__main__":
    main()
