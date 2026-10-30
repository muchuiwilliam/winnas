import asyncio, time
from datetime import datetime, timedelta, timezone
import httpx
from fastapi import FastAPI, HTTPException, Query

app = FastAPI()
cl = httpx.AsyncClient(
    timeout=httpx.Timeout(15, connect=5),
    transport=httpx.AsyncHTTPTransport(retries=2, local_address="0.0.0.0"),
    headers={"user-agent": "curl"},
)
C, L = {}, {}

import json, sqlite3

db = sqlite3.connect("cache.db", check_same_thread=False)
db.execute("pragma journal_mode=wal")
db.execute("create table if not exists c(k text primary key, exp real, val text)")
db.execute("delete from c where exp < ?", (time.time() - 7 * 86400,))
db.commit()

def dget(k):
    r = db.execute("select exp,val from c where k=?", (k,)).fetchone()
    return (r[0], json.loads(r[1])) if r else None

def dset(k, exp, v):
    db.execute("insert or replace into c values(?,?,?)", (k, exp, json.dumps(v)))
    db.commit()

def look(k):
    e = C.get(k)
    if not e:
        e = dget(k)
        if e:
            C[k] = e
    return e

async def cached(k, ttl, fn):
    e = look(k)
    if e and e[0] > time.time():
        return e[1], "hit"
    async with L.setdefault(k, asyncio.Lock()):
        e = look(k)
        if e and e[0] > time.time():
            return e[1], "hit"
        try:
            v = await fn()
        except Exception:
            if e:
                return e[1], "stale"
            raise
        x = time.time() + ttl
        C[k] = (x, v)
        dset(k, x, v)
        return v, "miss"

async def get(u, **p):
    r = await cl.get(u, params=p)
    r.raise_for_status()
    return r.json()

async def cn(g):
    j = await get(f"https://restcountries.com/v3.1/alpha/{g['country_code']}",
                  fields="name,currencies,languages,population,cca3")
    return j[0] if isinstance(j, list) else j

def cnc(g):
    return cached(f"country:{g['country_code']}", 86400, lambda: cn(g))

async def fx(g):
    c, _ = await cnc(g)
    cur = next(iter(c.get("currencies") or {}), None)
    if not cur:
        return None
    if cur == "USD":
        return {"cur": cur, "per_usd": 1.0}
    try:
        j = await get("https://api.frankfurter.dev/v1/latest", base="USD", symbols=cur)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (404, 422):
            return {"cur": cur, "per_usd": None, "note": "unsupported"}
        raise
    return {"cur": cur, "per_usd": j["rates"][cur], "date": j["date"]}

async def ds(g):
    la, lo = g["latitude"], g["longitude"]
    j = await get("https://eonet.gsfc.nasa.gov/api/v3/events", status="open", days=30,
                  bbox=f"{lo-3},{la+3},{lo+3},{la-3}")
    e = j["events"]
    return {"n": len(e), "items": [{"t": x["title"], "cat": x["categories"][0]["title"]} for x in e[:5]]}

async def cr(g):
    cc = g["country_code"]
    j = await get(f"https://api.worldbank.org/v2/country/{cc}/indicator/VC.IHR.PSRC.P5",
                  format="json", mrnev=1)
    r = j[1][0] if len(j) > 1 and j[1] else None
    out = {"homicide_100k": {"v": r["value"], "yr": r["date"]} if r else None}
    if cc == "GB":
        c = await get("https://data.police.uk/api/crimes-street/all-crime",
                      lat=g["latitude"], lng=g["longitude"])
        n = {}
        for x in c:
            n[x["category"]] = n.get(x["category"], 0) + 1
        out["local"] = {"total": len(c), "by_cat": n}
    return out

V = {"dtp3": "WHS4_100", "mcv1": "WHS8_110"}

async def vac(g):
    c, _ = await cnc(g)
    i3 = c["cca3"]

    async def one(k):
        j = await get(f"https://ghoapi.azureedge.net/api/{V[k]}",
                      **{"$filter": f"SpatialDim eq '{i3}'", "$orderby": "TimeDim desc", "$top": 1})
        r = j["value"]
        return k, ({"v": r[0]["NumericValue"], "yr": r[0]["TimeDim"]} if r else None)

    return dict(await asyncio.gather(*map(one, V)))

async def geo(n):
    j = await get("https://geocoding-api.open-meteo.com/v1/search", name=n, count=1)
    if not j.get("results"):
        raise HTTPException(404, "city not found")
    return j["results"][0]

async def wx(g):
    j = await get("https://api.open-meteo.com/v1/forecast", latitude=g["latitude"],
                  longitude=g["longitude"], current="temperature_2m,wind_speed_10m,weather_code")
    return j["current"]

async def aq(g):
    j = await get("https://air-quality-api.open-meteo.com/v1/air-quality", latitude=g["latitude"],
                  longitude=g["longitude"], current="us_aqi,pm2_5")
    return j["current"]

async def eq(g):
    t = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    j = await get("https://earthquake.usgs.gov/fdsnws/event/1/query", format="geojson",
                  latitude=g["latitude"], longitude=g["longitude"], maxradiuskm=300,
                  starttime=t, minmagnitude=2.5)
    m = [f["properties"]["mag"] for f in j["features"]]
    return {"n": len(m), "max": max(m, default=None)}

S = {
    "weather": (600, wx, "city"),
    "air": (1800, aq, "city"),
    "quakes": (3600, eq, "city"),
    "disasters": (1800, ds, "city"),
    "crime": (86400, cr, "city"),
    "country": (86400, cn, "cc"),
    "fx": (3600, fx, "cc"),
    "vaccines": (86400 * 7, vac, "cc"),
}


@app.get("/city/{name}")
async def city(name: str, src: list[str] = Query(None)):
    want = src or list(S)
    bad = [s for s in want if s not in S]
    if bad:
        raise HTTPException(400, f"unknown src: {bad}")
    g, _ = await cached(f"geo:{name.lower()}", 30 * 86400, lambda: geo(name))

    async def one(s):
        ttl, fn, sc = S[s]
        k = g["country_code"] if sc == "cc" else g["id"]
        try:
            v, st = await cached(f"{s}:{k}", ttl, lambda: fn(g))
            return s, {"data": v, "cache": st}
        except Exception as e:
            return s, {"data": None, "err": repr(e)}

    res = dict(await asyncio.gather(*(one(s) for s in want)))
    return {"city": {k: g.get(k) for k in ("name", "country", "latitude", "longitude")}, "res": res}
