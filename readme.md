# CityAPI

One request, one city, a full snapshot: weather, air quality, earthquakes, natural disasters, crime, currency vs USD, and vaccination coverage. Built with FastAPI. No API keys needed.

```
GET /city/Tokyo
GET /city/Nairobi?src=weather&src=fx
```

## Why

Public data APIs are scattered, inconsistent, and flaky. CityAPI fans out to the relevant free sources, normalizes the results, and caches every source independently, so one failing provider never forces a refetch of the others.

## Features

- 8 data sources behind one endpoint
- Pick only the sources you need with `?src=`
- Independent cache per source, each with its own TTL
- Two tier cache: in-memory (L1) over SQLite (L2), survives restarts
- Stale-on-error: if a refresh fails, the last good value is served and flagged `stale`
- Per-key locking: concurrent identical requests cause one upstream call
- Partial results: a failed source returns `data: null` plus an error, the rest still come back
- Zero API keys
- Auto generated docs at `/docs` (Swagger) and `/redoc`

## Quick start

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn httpx babel
uvicorn main:app --reload
```

Then:

```bash
curl localhost:8000/city/Tokyo
curl "localhost:8000/city/Nairobi?src=weather&src=air"
```

Interactive docs: http://localhost:8000/docs

## API

### `GET /city/{name}`

| Param  | In    | Required | Description                                                      |
|--------|-------|----------|------------------------------------------------------------------|
| `name` | path  | yes      | City name. The first geocoding match is used.                    |
| `src`  | query | no       | Repeatable. One of the src keys above. Omitted means all of them.|

Errors:

| Code | When                                         |
|------|----------------------------------------------|
| 400  | Unknown `src` value                          |
| 404  | City not found                               |
| 502  | Geocoding upstream failed (see body for why) |

Per source failures do not fail the request. They appear inside `res`.

### Response

```json
{
  "city": {
    "name": "Tokyo",
    "country": "Japan",
    "latitude": 35.6895,
    "longitude": 139.69171
  },
  "res": {
    "weather": {
      "data": { "time": "2026-10-30T23:00", "interval": 900, "temperature_2m": 14.9, "wind_speed_10m": 5.0, "weather_code": 0 },
      "cache": "miss"
    },
    "air": {
      "data": { "time": "2026-10-30T23:00", "interval": 3600, "us_aqi": 151, "pm2_5": 54.8 },
      "cache": "hit"
    },
    "quakes": {
      "data": { "n": 19, "max": 5.5 },
      "cache": "miss"
    },
    "crime": {
      "data": { "homicide_100k": { "v": 0.229, "yr": "2023" } },
      "cache": "hit"
    },
    "fx": {
      "data": null,
      "err": "ConnectTimeout: "
    }
  }
}
```

Each entry in `res` has:

| Field   | Meaning                                                              |
|---------|----------------------------------------------------------------------|
| `data`  | Normalized payload, or `null` on failure                             |
| `cache` | `hit` (fresh cache), `miss` (just fetched), `stale` (refresh failed, old value served) |
| `err`   | Present only when `data` is `null`. Exception type and message.      |

Weather code values follow the WMO weather interpretation codes used by Open-Meteo.

## How caching works

```
request -> L1 memory -> L2 SQLite -> upstream
                 ^           ^           |
                 +-----------+-----------+  write back on success
```

- Cache key: `{src}:{id}` where `id` is the geocoded city id, or the ISO2 country code for country scoped sources
- Fresh entry in L1 or L2: return it (`hit`)
- Expired or missing: take the key lock, recheck, fetch, store in both tiers (`miss`)
- Fetch fails and an expired entry exists: return it (`stale`)
- Fetch fails and nothing cached: the source reports an error, other sources are unaffected
- Failures are never cached, so the next request retries only the failed source
- Startup purges rows that expired more than 7 days ago. Fresher expired rows are kept on purpose as stale fallbacks.

SQLite schema:

```sql
create table c(k text primary key, exp real, val text)
```

`val` is the JSON payload, `exp` is the expiry as a unix timestamp. The file is `cache.db`, created next to `main.py`. WAL mode is on.

## Project layout

```
main.py     app, cache, fetchers, source registry, endpoint
cache.db    created at runtime
```

Adding a source takes 2 steps:

1. Write `async def fn(g)` where `g` is the geocoding result (`id`, `latitude`, `longitude`, `country_code`, ...) and return a JSON serializable value
2. Register it: `S["name"] = (ttl_seconds, fn, "city")` or `"cc"` for country scope

Sources that need country info (like `fx`) call `cnc(g)`, which reads the shared `country` cache entry.

## HTTP client notes

- `httpx.AsyncClient` with a 15 s total and 5 s connect timeout
- `AsyncHTTPTransport(retries=2, local_address="0.0.0.0")` retries connection failures and forces IPv4
- Send an honest `User-Agent` such as `cityapi/0.1 (you@example.com)`. Several providers rate limit or block anonymous or spoofed agents.
- A custom transport makes httpx ignore proxy environment variables. If you are behind a proxy, pass `proxy=os.environ.get("HTTPS_PROXY")` to the transport.
- Upstream `httpx` errors outside the per source wrapper (for example geocoding) return a 502 with `err`, `upstream`, `url`, and `body`.

## Verify before relying on

Upstream APIs change. These items were not confirmed against live responses in every environment:

- WHO GHO host (`ghoapi.azureedge.net`) and indicator codes `WHS4_100` (DTP3) and `WHS8_110` (MCV1). List codes with `GET /api/Indicator?$filter=contains(IndicatorName,'DTP3')`.
- EONET `bbox` parameter order: `lon_min,lat_max,lon_max,lat_min`
- World Bank homicide indicator `VC.IHR.PSRC.P5` has gaps for many countries and lags by 1 to 2 years

REST Countries v1 to v4 are retired and v5 needs an account and key, so this project does not use it.

## Limitations

- Crime is country level homicide rate only, except street level counts for UK cities. There is no free global city level crime source.
- Geocoding takes the first match. "Paris" resolves to whatever the geocoder ranks first. A `country` query param is a natural next step.
- `fx` covers only ECB published currencies
- L1 cache is per process. Multiple workers share L2 via SQLite and may duplicate some fetches.
- SQLite calls are synchronous on the event loop. Fine at hackathon scale, wrap in `asyncio.to_thread` or use `aiosqlite` for more load.
- No auth and no rate limiting on the API itself
- Disaster data (EONET) lists major events only, so an empty list is common
