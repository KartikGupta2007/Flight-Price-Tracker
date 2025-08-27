#!/usr/bin/env python3
# flight_monitor.py — flights-only alerts, stable times, auto-snap, and rise/drop/both alerts

import os, json, time, math, random
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
import requests

load_dotenv()

API_KEY   = os.getenv("API_KEY","").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN","").strip()
CHAT_ID   = os.getenv("CHAT_ID","").strip()
USE_REAL  = os.getenv("USE_REAL","0").strip()=="1"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL","30"))

# new alert settings
ALERT_MODE = os.getenv("ALERT_MODE","drop").lower()       # drop | rise | both
MIN_CHANGE_PCT = float(os.getenv("MIN_CHANGE_PCT","1.0")) # % move to trigger alert

PRICES_FILE = "prices.json"
SUBS_FILE   = "subscriptions.json"

KIWI_HOST = "kiwi-com-cheap-flights.p.rapidapi.com"
ONE_WAY_ENDPOINT = "one-way"
REQUEST_TIMEOUT = 20

# ---------- io helpers ----------
def jload(path, default):           #####Safely read a JSON file.
    try:
        with open(path,"r") as f: return json.load(f) 
    except FileNotFoundError:
        return default

def jsave(path, data):              #####Safely write a JSON file.
    with open(path,"w") as f: json.dump(data,f,indent=2)

# ---------- subscriptions: only specific flights ----------
def load_selected_flights() -> List[Dict[str,str]]:         ####Load only flight-level subscriptions
    """
    subscriptions.json:
      { "<CHAT_ID>": [
          {"key":"DEL-BLR-2025-08-25","flightNo":"6E200","airline":"IndiGo"},
          ...
      ]}
    We ignore bare strings (route alerts).
    """
    subs = jload(SUBS_FILE, {})
    items = subs.get(CHAT_ID, [])
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("key") and it.get("flightNo"):
            out.append({"key": it["key"], "flightNo": it["flightNo"], "airline": it.get("airline","")})
    return out

# ---------- Telegram ----------
def notify(msg:str):                        ####Send a Telegram message.
    if not (BOT_TOKEN and CHAT_ID):
        print("[notify] (not configured) ->", msg); return
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
        if r.status_code != 200:
            print("[notify] ❌", r.text)
    except Exception as e:
        print("[notify] ❌", e)

# ---------- price store ----------
def get_store() -> Dict[str, Any]:              ####Load prices.json file.
    return jload(PRICES_FILE, {})

def set_store(store: Dict[str, Any]):           ####Save prices.json file.
    jsave(PRICES_FILE, store)

# ---------- stable mock flights (times NEVER change per route+date) ----------
def stable_mock_flights(origin:str, destination:str, date:str) -> List[Dict[str,Any]]:                                 ## Deterministic mock flights with stable times and slight price wiggle
    rnd = random.Random(f"{origin}-{destination}-{date}")
    airlines = [("IndiGo","6E"), ("Air India","AI"), ("Akasa","QP"), ("SpiceJet","SG"), ("Air India Express","IX")]
    # fixed half-hour slots 06:00..22:30
    base_slots = [(h,m) for h in range(6,23) for m in (0,30)]
    rnd.shuffle(base_slots)
    slots = sorted(base_slots[:7])  # 7 flights stable

    base_price = 3200 + (abs(hash((origin,destination,date))) % 3000)
    minute = int(time.time()//60)
    wiggle = math.sin(minute/11.0)*0.04 + math.cos(minute/7.0)*0.02  # ±~6%

    flights=[]
    for idx,(h,m) in enumerate(slots):
        airline, code = airlines[idx % len(airlines)]
        num = 200 + idx*7 + (h*2 + m//30)  # stable-ish number
        depart = datetime.fromisoformat(date) + timedelta(hours=h, minutes=m)
        dur = 110 + (idx*9 % 50)  # 110–159 min, stable
        arrive = depart + timedelta(minutes=dur)
        price = int(max(1200, base_price*(1.0+wiggle)))
        flights.append({
            "airline": airline,
            "flightNo": f"{code}{num}",
            "depart": depart.strftime("%Y-%m-%dT%H:%M"),
            "arrive": arrive.strftime("%Y-%m-%dT%H:%M"),
            "duration": f"{dur//60}h {dur%60:02d}m",
            "price": price
        })
    # remove Vistara for realism (closed)
    return [f for f in flights if f["airline"].lower()!="vistara"]

# ---------- Kiwi (best-effort) ----------
def parse_list_like(js)->List[Dict[str,Any]]:                   ##Defensive parser for RapidAPI/Kiwi-like responses.
    out=[]
    if isinstance(js,dict) and isinstance(js.get("data"),list):
        for it in js["data"][:20]:
            p = it.get("price")
            price = None
            if isinstance(p,(int,float)): price=int(p)
            elif isinstance(p,dict):
                for k in ("amount","raw","value","min","max"):
                    if isinstance(p.get(k),(int,float)): price=int(p[k]); break
            if price is None: continue
            out.append({
                "airline": it.get("airline",""),
                "flightNo": it.get("flightNo",""),
                "depart": it.get("depart",""),
                "arrive": it.get("arrive",""),
                "duration": it.get("duration",""),
                "price": price
            })
    return [x for x in out if x.get("airline","").lower()!="vistara"]

def kiwi_query(params)->List[Dict[str,Any]]:               ##Query RapidAPI/Kiwi for real flights.
    url=f"https://{KIWI_HOST}/{ONE_WAY_ENDPOINT}"
    headers={"x-rapidapi-key":API_KEY,"x-rapidapi-host":KIWI_HOST}
    try:
        r=requests.get(url,headers=headers,params=params,timeout=REQUEST_TIMEOUT); r.raise_for_status()
        return parse_list_like(r.json())
    except Exception:
        return []

def real_flights(origin,to,date)->List[Dict[str,Any]]:                  ##Try multiple parameterizations (airport→airport, city_in→city_in, city→city) to get results.
    if not API_KEY: return []
    base={"currency":"INR","locale":"en","adults":"1","limit":"20","cabinClass":"ECONOMY",
          "sortBy":"QUALITY","sortOrder":"ASCENDING","outboundDepartureDateFrom":date,
          "outboundDepartureDateTo":date,"transportTypes":"FLIGHT"}
    attempts=[
        {**base,"source":f"Airport:{origin}","destination":f"Airport:{to}"},
        {**base,"source":f"City:{origin.lower()}_in","destination":f"City:{to.lower()}_in"},
        {**base,"source":f"City:{origin.lower()}","destination":f"City:{to.lower()}"},
    ]
    for p in attempts:
        res=kiwi_query(p)
        if res: return res
    return []

def fetch_flights(origin,to,date)->List[Dict[str,Any]]:                ##Fetch flights: real if got, else stable mock.
    if USE_REAL:
        res=real_flights(origin,to,date)
        if res: return res
    return stable_mock_flights(origin,to,date)

# ---------- smart matching ----------
def find_exact(flights: List[Dict[str,Any]], flight_no: str) -> Optional[Dict[str,Any]]:    ##Find exact flight number match.
    return next((f for f in flights if (f.get("flightNo") or "") == flight_no), None)

def find_fallback(flights: List[Dict[str,Any]], airline: str, last_depart_iso: Optional[str]) -> Optional[Dict[str,Any]]:               ####If exact not found, match same airline + closest depart to last_depart within ±15 minutes.
    if not airline or not last_depart_iso:
        return None
    try:
        target = datetime.fromisoformat(last_depart_iso)
    except Exception:
        return None
    same_air = [f for f in flights if (f.get("airline") or "").lower() == airline.lower()]
    if not same_air: return None
    best=None; best_delta=None
    for f in same_air:
        try: dep=datetime.fromisoformat(f["depart"])
        except Exception: continue
        delta=abs((dep-target).total_seconds())
        if best is None or delta<best_delta:
            best=f; best_delta=delta
    if best and best_delta<=15*60: return best
    return None

# ---------- main check ----------
def check_once():                                   ###One full monitoring pass over all selected flights.
    selected = load_selected_flights()
    if not selected:
        print("[info] No flight subscriptions yet."); return

    store = get_store()
    changed=False

    for sub in selected:
        key=sub["key"]  # e.g. "DEL-BLR-2025-08-25"
        try:
            origin,dest,date = key.split("-",2)
        except ValueError:
            print(f"[warn] Bad key {key}"); continue

        flight_no=sub["flightNo"]; airline=sub.get("airline","")
        flights=fetch_flights(origin,dest,date)
        price_key=f"{key}#{flight_no}"
        prev=store.get(price_key,{"last_price":None,"last_notified_price":None,"last_notified_dir":None,"last_depart":None})
        last=prev["last_price"]; last_notified=prev["last_notified_price"]; last_dir=prev.get("last_notified_dir")
        last_depart=prev["last_depart"]

        # 1) exact
        found=find_exact(flights,flight_no)
        # 2) fallback by airline+last_depart
        if not found: found=find_fallback(flights,airline,last_depart)
        # 3) auto-snap on first run to cheapest same-airline flight
        if not found and last is None and airline:
            same=[f for f in flights if (f.get("airline") or "").lower()==airline.lower()]
            if same:
                found=min(same,key=lambda x:x["price"])
                print(f"[snap] {price_key} snapped to {found['flightNo']} ({found['depart']})")

        ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not found:
            print(f"[{ts}] {price_key} | not found — keeping last={last}")
            continue

        price=int(found["price"]); depart_iso=found.get("depart")
        print(f"[{ts}] {price_key} | price={price} | last={last} | notif={last_notified} dir={last_dir}")

        # ---- alert logic (drop / rise / both with de-dupe) ----
        if last is None:
            # first observation: set baseline
            store[price_key] = {
                "last_price": price,
                "last_notified_price": None,
                "last_notified_dir": None,
                "last_depart": depart_iso
            }
            changed = True
            continue

        pct_move = 0.0 if last <= 0 else 100.0 * abs(price - last) / last
        direction = "drop" if price < last else ("rise" if price > last else "flat")

        should_alert = False
        if direction == "drop" and ALERT_MODE in ("drop","both") and pct_move >= MIN_CHANGE_PCT:
            should_alert = (price != last_notified or last_dir != "drop")
        elif direction == "rise" and ALERT_MODE in ("rise","both") and pct_move >= MIN_CHANGE_PCT:
            should_alert = (price != last_notified or last_dir != "rise")

        if should_alert:
            if direction == "drop":
                msg = (f"✈️ Price drop\n{origin} → {dest} {date}\n"
                       f"Flight {flight_no} ({airline})\n"
                       f"Now ₹{price} (was ₹{last}, −{pct_move:.1f}%)")
            else:
                msg = (f"✈️ Price rise\n{origin} → {dest} {date}\n"
                       f"Flight {flight_no} ({airline})\n"
                       f"Now ₹{price} (was ₹{last}, +{pct_move:.1f}%)")
            notify(msg)
            store[price_key] = {
                "last_price": price,
                "last_notified_price": price,
                "last_notified_dir": direction,
                "last_depart": depart_iso
            }
            changed = True
        else:
            # no alert; just update last seen price & depart
            store[price_key] = {
                "last_price": price,
                "last_notified_price": last_notified,
                "last_notified_dir": last_dir,
                "last_depart": depart_iso
            }
            changed = True

    if changed: set_store(store)

if __name__=="__main__":
    print(f"🛫 Monitor (flights-only, USE_REAL={'ON' if USE_REAL else 'OFF'}) every {CHECK_INTERVAL}s. Ctrl+C to stop.")
    try:
        while True:
            check_once()
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        print("Stopping.")