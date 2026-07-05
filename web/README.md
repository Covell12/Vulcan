# Vulcan — web frontend

This directory is the **Vulcan production site**. It is a **standalone static
frontend**: plain HTML / CSS / JS with no build step and no framework. It shares
no code with the API — it talks to the Vulcan API purely over HTTP through a
single client module, [`api.js`](api.js). That means you can serve it from the
API in dev, or deploy it to any static host (Netlify, S3+CloudFront, GitHub
Pages, nginx…) pointed at the API wherever it runs.

## How the site talks to the API

Everything — every request and every API-served asset URL (images, STEP/3MF/STL
downloads, the STL the 3D viewer fetches) — goes through `api.js`. Nothing else
in the site calls `fetch()` on an API path directly. To move the site to a
different origin than the API you change **one setting** and add CORS; no other
edits.

### Point the site at an API origin — `config.js`

[`config.js`](config.js) sets `window.VULCAN_CONFIG.apiBase`:

- `""` (empty, the default) → **same origin.** This is what happens when the
  FastAPI app serves the site in local dev (`uvicorn api.main:app`). Root-relative
  paths like `/intents` and `/exports/…/part.stl` are used as-is.
- `"https://api.vulcan.example"` → the site calls that API from wherever it's
  hosted.

Three ways to set it without editing code (checked in order):

1. `window.VULCAN_API_BASE = "https://api…"` defined before `config.js` loads.
2. `<meta name="vulcan-api-base" content="https://api…">` in the page `<head>`.
3. Edit the fallback string in `config.js`.

### Let the API accept the site — CORS

When the site is on a different origin than the API, the API must allow that
origin. Set `VULCAN_CORS_ORIGINS` (comma-separated) on the API:

```
VULCAN_CORS_ORIGINS=https://vulcan.app,https://www.vulcan.app
```

Default is `*` (fine for local dev / same-origin). No cookies are used — the
founder review token is a request header — so credentials stay off.

## Files

| File | Role |
| --- | --- |
| `index.html` | The product site: nav, hero, "how it works", the studio (photo flow + template studio), footer. |
| `review.html` | Founder review dashboard (approve/reject freeform designs, download CAD). |
| `config.js` | Where `apiBase` is resolved. Load first. |
| `api.js` | The one API client (`window.VulcanAPI`) + shared `describeFetchError`/`errorText`. |
| `app.js` | The "Template studio" tab (dynamic form from `GET /templates`). |
| `units.js` | mm/cm/in conversion at the UI boundary (internal units are mm). |
| `intents.js` | The "Start with a photo" flow (upload → questions → part). |
| `viewer3d.js` | Interactive 3D STL viewer (`window.Vulcan3D`). |
| `site.js` | Presentation-only chrome (nav scroll, mobile menu, reveal-on-scroll). |
| `style.css` | The dark "forge" theme. |
| `assets/` | Logo + favicons. |
| `vendor/` | Vendored, offline deps: Three.js (+ STLLoader/OrbitControls) and the fonts (no CDN). |

Script load order matters: `three → STLLoader → OrbitControls → viewer3d →
config → api → app → units → intents → site`. `config.js`/`api.js` must load
before `app.js`/`intents.js` (which use `VulcanAPI` and the shared error
helpers). `units.js` before `intents.js`.

## Run it

```bash
uvicorn api.main:app --reload   # API + site at http://localhost:8000
```

To host the site separately, copy `web/` to your static host, set `apiBase`
(above), set `VULCAN_CORS_ORIGINS` on the API, and you're done.
