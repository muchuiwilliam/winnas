import asyncio, time
from datetime import datetime, timedelta, timezone
import httpx
from fastapi import FastAPI, HTTPException, Query

app = FastAPI()
cl = httpx.AsyncClient(timeout=10)
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

async def cn(g):
    j = await get(f"https://restcountries.com/v3.1/alpha/{g['country_code']}",
                  fields="name,currencies,languages,population")
    return j[0] if isinstance(j, list) else j

S = {
    "weather": (600, wx),
    "air": (1800, aq),
    "quakes": (3600, eq),
    "country": (86400, cn),
}

@app.get("/city/{name}")
async def city(name: str, src: list[str] = Query(None)):
    want = src or list(S)
    bad = [s for s in want if s not in S]
    if bad:
        raise HTTPException(400, f"unknown src: {bad}")
    g, _ = await cached(f"geo:{name.lower()}", 30 * 86400, lambda: geo(name))

    async def one(s):
        ttl, fn = S[s]
        try:
            v, st = await cached(f"{s}:{g['id']}", ttl, lambda: fn(g))
            return s, {"data": v, "cache": st}
        except Exception as e:
            return s, {"data": None, "err": repr(e)}

    res = dict(await asyncio.gather(*(one(s) for s in want)))
    return {"city": {k: g.get(k) for k in ("name", "country", "latitude", "longitude")}, "res": res}
