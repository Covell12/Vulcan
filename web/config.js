// Vulcan site configuration.
//
// This static frontend is a standalone client — it talks to the Vulcan API
// purely over HTTP (through web/api.js) and shares no code with it. Point it at
// wherever the API lives by setting `apiBase`:
//
//   ""                          -> same origin (the default; FastAPI serves
//                                  this site in local dev)
//   "https://api.vulcan.app"    -> a separately-hosted API
//
// At deploy time you can set it without editing this file in three ways
// (checked in order): define `window.VULCAN_API_BASE` before this script loads,
// add `<meta name="vulcan-api-base" content="https://api…">` to the page, or
// just edit the fallback string below. See web/README.md.
window.VULCAN_CONFIG = {
  apiBase:
    window.VULCAN_API_BASE ||
    (document.querySelector('meta[name="vulcan-api-base"]') || {}).content ||
    "",
};
