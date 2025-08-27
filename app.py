#!/usr/bin/env python3
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os, json, re, math, random
import requests

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
CHAT_ID = os.getenv("CHAT_ID", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()
USE_REAL = os.getenv("USE_REAL", "0").strip() == "1"

PRICES_FILE = "prices.json"
SUBS_FILE   = "subscriptions.json"
ROUTES_FILE = "routes.json"

KIWI_HOST = "kiwi-com-cheap-flights.p.rapidapi.com"
ONE_WAY_ENDPOINT = "one-way"
REQUEST_TIMEOUT = 20

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

# ---------- helpers ----------
def load_json(path, default):               ##Safely read JSON from disk. 
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save_json(path, data):          ##Write JSON to disk (pretty-printed).
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def load_routes():                  ##Load & validate routes from routes.json.
    routes = load_json(ROUTES_FILE, [])
    out = []
    for r in routes:
        f = (r.get("from","") or "").upper().strip()
        t = (r.get("to","") or "").upper().strip()
        d = (r.get("date","") or "").strip()
        if re.fullmatch(r"[A-Z]{3}", f) and re.fullmatch(r"[A-Z]{3}", t) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            out.append({"from": f, "to": t, "date": d})
    return out

def split_subs_for_user(all_subs):      ##The current user’s subscription list 
    route_keys = set()
    flights_map = {}
    for item in all_subs:
        if isinstance(item, str):
            route_keys.add(item)
        elif isinstance(item, dict) and "key" in item:
            route_keys.add(item["key"])
            fn = (item.get("flightNo") or "").strip()
            if fn:
                flights_map.setdefault(item["key"], [])
                flights_map[item["key"]].append({
                    "flightNo": fn,
                    "airline": (item.get("airline") or "").strip()
                })
    return route_keys, flights_map

# ---------- UI data ----------
@app.get("/api/routes")             ##Return all saved routes + whether this user is subscribed, and any flight filters saved for them.
def get_routes():
    prices = load_json(PRICES_FILE, {})
    subs   = load_json(SUBS_FILE, {})
    my_route_keys, my_flights_map = split_subs_for_user(subs.get(CHAT_ID, []))
    routes = load_routes()
    out = []
    for r in routes:
        key = f"{r['from']}-{r['to']}-{r['date']}"
        entry = prices.get(key, {})
        out.append({
            "key": key, "from": r["from"], "to": r["to"], "date": r["date"],
            "last_price": entry.get("last_price"),
            "subscribed": key in my_route_keys,
            "flight_filters": my_flights_map.get(key, [])
        })
    return jsonify({"updated_at": datetime.now().isoformat(timespec="seconds"), "routes": out})

@app.post("/api/routes")            ## @ is flask decorator
def add_route():                    ## Add a new route if valid and not duplicate.
    body = request.get_json(force=True)
    f = (body.get("from","") or "").upper().strip()
    t = (body.get("to","") or "").upper().strip()
    d = (body.get("date","") or "").strip()
    if not re.fullmatch(r"[A-Z]{3}", f): return jsonify({"ok": False, "error": "Invalid FROM"}), 400
    if not re.fullmatch(r"[A-Z]{3}", t): return jsonify({"ok": False, "error": "Invalid TO"}), 400
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d): return jsonify({"ok": False, "error": "Invalid DATE"}), 400

    routes = load_json(ROUTES_FILE, [])
    if any((r.get("from","").upper(), r.get("to","").upper(), r.get("date","")) == (f, t, d) for r in routes):
        return jsonify({"ok": True, "message": "Route already exists", "key": f"{f}-{t}-{d}"})
    routes.append({"from": f, "to": t, "date": d})
    save_json(ROUTES_FILE, routes)
    return jsonify({"ok": True, "key": f"{f}-{t}-{d}"})

@app.delete("/api/routes")
def delete_route():                 ## Delete a route by key.
    key = request.args.get("key","").strip()
    m = re.fullmatch(r"([A-Z]{3})-([A-Z]{3})-(\d{4}-\d{2}-\d{2})", key)
    if not m: return jsonify({"ok": False, "error": "Invalid key"}), 400
    f, t, d = m.group(1), m.group(2), m.group(3)

    routes = load_json(ROUTES_FILE, [])
    new_routes = [r for r in routes if not ((r.get("from","").upper()==f) and (r.get("to","").upper()==t) and (r.get("date","")==d))]
    save_json(ROUTES_FILE, new_routes)

    subs = load_json(SUBS_FILE, {})
    new_items = []
    for item in subs.get(CHAT_ID, []):
        if isinstance(item, str):
            if item != key: new_items.append(item)
        elif isinstance(item, dict):
            if item.get("key") != key: new_items.append(item)
    subs[CHAT_ID] = new_items
    save_json(SUBS_FILE, subs)
    return jsonify({"ok": True})

@app.post("/api/subscriptions")
def set_subscriptions():            ## tells which flight to add alert
    body = request.get_json(force=True)
    keys = body.get("keys", [])
    subs = load_json(SUBS_FILE, {})
    existing = subs.get(CHAT_ID, [])
    flight_objs = [it for it in existing if isinstance(it, dict) and "key" in it]
    subs[CHAT_ID] = list(dict.fromkeys(keys)) + flight_objs
    save_json(SUBS_FILE, subs)
    return jsonify({"ok": True, "count": len(subs[CHAT_ID])})

@app.post("/api/flight-filters")
def set_flight_filters():                           ## those how have flight no. and tells which flight to add alert
    body = request.get_json(force=True)
    key = (body.get("key","") or "").strip()
    flights = body.get("flights", [])
    if not re.fullmatch(r"[A-Z]{3}-[A-Z]{3}-\d{4}-\d{2}-\d{2}", key):
        return jsonify({"ok": False, "error": "Invalid route key"}), 400

    clean = []
    for f in flights:
        if not isinstance(f, dict): continue
        fn = (f.get("flightNo","") or "").strip()
        if not fn: continue
        airline = (f.get("airline","") or "").strip()
        if airline.lower() == "vistara":
            continue
        clean.append({"flightNo": fn, "airline": airline})

    subs = load_json(SUBS_FILE, {})
    items = subs.get(CHAT_ID, [])
    items = [it for it in items if not (isinstance(it, dict) and it.get("key")==key and it.get("flightNo"))]
    for f in clean:
        items.append({"key": key, "flightNo": f["flightNo"], "airline": f.get("airline","")})
    subs[CHAT_ID] = items
    save_json(SUBS_FILE, subs)
    return jsonify({"ok": True, "count": len(clean)})

# ---------- mock / real search ----------
def _mock_flights(origin, destination, date):       ##Deterministic, realistic flight list for the UI when API is unavailable.
    """
    Deterministic but realistic list per route/day:
    - unique flight numbers and time slots
    - varied durations 95–170 mins
    - 5–8 flights between 06:00–22:55
    - Vistara hidden
    """
    r = random.Random()
    r.seed(f"{origin}-{destination}-{date}")

    airlines = [
        ("IndiGo", "6E"),
        ("Air India", "AI"),
        ("Akasa", "QP"),
        ("SpiceJet", "SG"),
        ("Air India Express", "IX"),
    ]

    n = r.randint(5, 8)
    flights = []
    used_nums = set()
    used_slots = set()

    # base for prices, varies per route/day
    base = 3200 + (abs(hash((origin, destination, date))) % 3000)

    for _ in range(n):
        airline, code = r.choice(airlines)
        # ensure unique flight number
        num = r.randint(200, 899)
        while (code, num) in used_nums:
            num = r.randint(200, 899)
        used_nums.add((code, num))

        # unique depart slot
        dep_hour = r.randint(6, 21)
        dep_min = r.choice([0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55])
        while (dep_hour, dep_min) in used_slots:
            dep_hour = r.randint(6, 21)
            dep_min = r.choice([0, 15, 30, 45])
        used_slots.add((dep_hour, dep_min))

        depart_dt = datetime.fromisoformat(date) + timedelta(hours=dep_hour, minutes=dep_min)
        duration_min = r.randint(95, 170)
        arrive_dt = depart_dt + timedelta(minutes=duration_min)

        price = int(base + r.randint(-600, 800))

        flights.append({
            "airline": airline,
            "flightNo": f"{code}{num}",
            "depart": depart_dt.strftime("%Y-%m-%dT%H:%M"),
            "arrive": arrive_dt.strftime("%Y-%m-%dT%H:%M"),
            "duration": f"{duration_min//60}h {duration_min%60:02d}m",
            "price": max(1200, price),
        })

    # hide Vistara just in case it appears
    flights = [f for f in flights if f["airline"].lower() != "vistara"]
    flights.sort(key=lambda x: (x["depart"], x["price"]))
    return flights

def _extract_price_list_like(data):                     ##Parse a API-like response into your normalized flight dict.
    out = []
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        for item in data["data"][:20]:
            price = None
            p = item.get("price")
            if isinstance(p, (int, float)): price = int(p)
            elif isinstance(p, dict):
                for k in ("amount","raw","value","min","max"):
                    if isinstance(p.get(k), (int,float)): price = int(p[k]); break
            out.append({
                "airline": item.get("airline",""),
                "flightNo": item.get("flightNo",""),
                "depart": item.get("depart",""),
                "arrive": item.get("arrive",""),
                "duration": item.get("duration",""),
                "price": price
            })
    out = [o for o in out if o.get("price") is not None]
    if out:
        out.sort(key=lambda x: x["price"])
        out = [o for o in out if (o.get("airline","").lower() != "vistara")]
        return out
    return None

def search_flights_real(origin, destination, date):                     ##Search for real flights using the Kiwi API.
    headers = {"x-rapidapi-key": API_KEY, "x-rapidapi-host": KIWI_HOST}
    url = f"https://{KIWI_HOST}/{ONE_WAY_ENDPOINT}"
    base = {
        "currency": "INR", "locale": "en", "adults": "1", "limit": "20",
        "cabinClass": "ECONOMY", "sortBy": "QUALITY", "sortOrder": "ASCENDING",
        "transportTypes": "FLIGHT", "outboundDepartureDateFrom": date, "outboundDepartureDateTo": date,
    }
    attempts = [
        {**base, "source": f"Airport:{origin}", "destination": f"Airport:{destination}"},
        {**base, "source": f"City:{origin.lower()}_in", "destination": f"City:{destination.lower()}_in"},
        {**base, "source": f"City:{origin.lower()}", "destination": f"City:{destination.lower()}"},
    ]
    for params in attempts:
        try:
            r = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            parsed = _extract_price_list_like(data)
            if parsed:
                return {"ok": True, "flights": parsed}
        except Exception:
            continue
    return {"ok": True, "flights": _mock_flights(origin, destination, date)}

@app.get("/api/search")
def api_search():               ## Search for flights (mock or real depending on config).
    origin = (request.args.get("from","") or "").upper().strip()
    destination = (request.args.get("to","") or "").upper().strip()
    date = (request.args.get("date","") or "").strip()
    if not (re.fullmatch(r"[A-Z]{3}", origin) and re.fullmatch(r"[A-Z]{3}", destination) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)):
        return jsonify({"ok": False, "error": "Invalid from/to/date"}), 400
    if USE_REAL and API_KEY:
        return jsonify(search_flights_real(origin, destination, date))
    return jsonify({"ok": True, "flights": _mock_flights(origin, destination, date)})

# ---------- Active tracker ----------
@app.get("/api/active")
def api_active():           ##Merge users selections with latest known prices from prices.json (written by the monitor).
    prices = load_json(PRICES_FILE, {})
    subs   = load_json(SUBS_FILE, {})
    routes = load_routes()
    route_keys, flights_map = split_subs_for_user(subs.get(CHAT_ID, []))
    active = []
    for r in routes:
        key = f"{r['from']}-{r['to']}-{r['date']}"
        if key not in route_keys:
            continue
        entry = prices.get(key, {})
        item = {
            "key": key, "from": r["from"], "to": r["to"], "date": r["date"],
            "last_price": entry.get("last_price"), "flights": []
        }
        for f in flights_map.get(key, []):
            fkey = f"{key}#{f.get('flightNo')}"
            fprice = prices.get(fkey, {}).get("last_price")
            item["flights"].append({"flightNo": f.get("flightNo"), "airline": f.get("airline"), "last_price": fprice})
        active.append(item)
    return jsonify({"updated_at": datetime.now().isoformat(timespec="seconds"), "active": active})

@app.get("/")
def index():
    return send_from_directory(".", "index.html")

if __name__ == "__main__":
    app.run(debug=True, port=5057)