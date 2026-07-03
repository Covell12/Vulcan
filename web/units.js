// Unit handling at the UI boundary (CLAUDE.md rule 4). Internal units are
// millimeters everywhere; this is the ONE place lengths are converted, right
// before a value is sent to the API. All functions are pure so they can be
// exercised in the browser console (or a node harness).

const MM_PER_UNIT = {
  mm: 1,
  cm: 10,
  in: 25.4,
};

const UNIT_ORDER = ["mm", "cm", "in"];

// Convert a value entered in `unit` to millimeters. Returns NaN for junk input.
function toMm(value, unit) {
  const n = typeof value === "number" ? value : parseFloat(value);
  const factor = MM_PER_UNIT[unit];
  if (!Number.isFinite(n) || factor === undefined) return NaN;
  return n * factor;
}

// Trim trailing zeros for display: 203.2 -> "203.2", 200 -> "200".
function trimNumber(n) {
  return Number.parseFloat(n.toFixed(2)).toString();
}

// A dual display string for what the user typed, e.g. "8 in = 203.2 mm".
// For mm input it stays single ("203 mm"). Empty/invalid -> "".
function formatDual(value, unit) {
  const raw = typeof value === "number" ? value : parseFloat(value);
  if (!Number.isFinite(raw)) return "";
  const mm = toMm(raw, unit);
  if (!Number.isFinite(mm)) return "";
  if (unit === "mm") return `${trimNumber(mm)} mm`;
  return `${trimNumber(raw)} ${unit} = ${trimNumber(mm)} mm`;
}

// The unit remembered for this session (defaults to mm).
let _sessionUnit = "mm";
function getSessionUnit() {
  return _sessionUnit;
}
function setSessionUnit(unit) {
  if (MM_PER_UNIT[unit] !== undefined) _sessionUnit = unit;
}
