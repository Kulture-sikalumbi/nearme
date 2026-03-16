"""Simple geofencing demo backend for portfolio.

Run locally with:
    uvicorn geofence_server:app --reload --host 0.0.0.0 --port 8000

Then:
- On your phone (same Wi-Fi): open http://<PC-LAN-IP>:8000/tracker?device=YourName
- On your PC: open http://localhost:8000/dashboard?device=YourName

The phone page sends GPS updates; the dashboard shows Far/Near/Immediate
based on distance to HOME_LAT/HOME_LON and uses browser speech to say an
alert when the device becomes Immediate.
"""

from datetime import datetime
from math import asin, cos, radians, sin, sqrt
from typing import Dict, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware


# ----- Config -----

# Set these to your real home location (use Google Maps to get coords).
HOME_LAT = -12.9434775
HOME_LON = 28.639831

HOME_RADIUS_M = 150.0  # Immediate zone
NEAR_RADIUS_M = 500.0  # Near zone


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class DeviceState(Dict[str, object]):
    """Typed alias for device state dict."""


# In-memory store: device_id -> state
STATES: Dict[str, DeviceState] = {}


# ----- Helpers -----


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance between two points on Earth in meters."""
    r = 6371000.0  # Earth radius in meters
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)

    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    c = 2 * asin(sqrt(a))
    return r * c


def classify_distance(dist_m: float) -> str:
    if dist_m <= HOME_RADIUS_M:
        return "immediate"
    if dist_m <= NEAR_RADIUS_M:
        return "near"
    return "far"


# ----- API -----


@app.get("/", include_in_schema=False)
async def root():
  """Redirect the base URL to the tracker without forcing a device name.

  The tracker page will ask once for a custom device name (and can
  remember it per browser), so viewers see their own label instead of
  a hardcoded "MyPhone".
  """
  return RedirectResponse(url="/tracker")


@app.post("/api/update_location")
async def update_location(request: Request):
    data = await request.json()
    device_id: str = data.get("device_id") or "unknown"
    lat: Optional[float] = data.get("lat")
    lon: Optional[float] = data.get("lon")

    if lat is None or lon is None:
        return JSONResponse({"error": "lat and lon required"}, status_code=400)

    dist = haversine_m(HOME_LAT, HOME_LON, lat, lon)
    status = classify_distance(dist)

    prev_status = STATES.get(device_id, {}).get("status", "far")
    now = datetime.utcnow().isoformat()

    STATES[device_id] = {
        "device_id": device_id,
        "lat": lat,
        "lon": lon,
        "distance_m": dist,
        "status": status,
        "previous_status": prev_status,
        "updated_at": now,
    }

    return {"status": status, "distance_m": dist, "updated_at": now}


@app.get("/api/state")
async def get_state(device: str = Query("default", alias="device_id")):
    state = STATES.get(device)
    if not state:
        return {"device_id": device, "status": "unknown"}
    return state


@app.get("/api/devices")
async def list_devices():
  """Return a list of known device IDs (for the dashboard picker)."""
  return {"devices": sorted(STATES.keys())}


@app.get("/api/config")
async def get_config():
  """Return current home coordinates and radius settings."""
  return {
    "home_lat": HOME_LAT,
    "home_lon": HOME_LON,
    "immediate_radius_m": HOME_RADIUS_M,
    "near_radius_m": NEAR_RADIUS_M,
  }


@app.post("/api/config")
async def update_config(request: Request):
  """Update home coordinates and/or radius settings at runtime."""
  global HOME_LAT, HOME_LON, HOME_RADIUS_M, NEAR_RADIUS_M
  data = await request.json()

  if "home_lat" in data and "home_lon" in data:
    try:
      HOME_LAT = float(data["home_lat"])
      HOME_LON = float(data["home_lon"])
    except (TypeError, ValueError):
      return JSONResponse({"error": "invalid home_lat/home_lon"}, status_code=400)

  if "immediate_radius_m" in data:
    try:
      HOME_RADIUS_M = float(data["immediate_radius_m"])
    except (TypeError, ValueError):
      return JSONResponse({"error": "invalid immediate_radius_m"}, status_code=400)

  if "near_radius_m" in data:
    try:
      NEAR_RADIUS_M = float(data["near_radius_m"])
    except (TypeError, ValueError):
      return JSONResponse({"error": "invalid near_radius_m"}, status_code=400)

  return {
    "home_lat": HOME_LAT,
    "home_lon": HOME_LON,
    "immediate_radius_m": HOME_RADIUS_M,
    "near_radius_m": NEAR_RADIUS_M,
  }


@app.post("/api/set_home_from_device")
async def set_home_from_device(request: Request):
  """Use the latest location of a device as the new home anchor."""
  global HOME_LAT, HOME_LON
  data = await request.json()
  device_id: Optional[str] = data.get("device_id")
  if not device_id:
    return JSONResponse({"error": "device_id required"}, status_code=400)

  state = STATES.get(device_id)
  if not state or state.get("lat") is None or state.get("lon") is None:
    return JSONResponse({"error": "no location available for device"}, status_code=404)

  try:
    HOME_LAT = float(state["lat"])  # type: ignore[index]
    HOME_LON = float(state["lon"])  # type: ignore[index]
  except (TypeError, ValueError):
    return JSONResponse({"error": "invalid device location"}, status_code=400)

  return {"home_lat": HOME_LAT, "home_lon": HOME_LON}


# ----- Pages -----


@app.get("/tracker", response_class=HTMLResponse)
async def tracker_page(device: Optional[str] = Query(None)):
    # Mobile-optimised HTML+JS page that sends geolocation updates to the backend.
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>Geofence Tracker</title>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, \"SF Pro Text\", sans-serif;
      background: radial-gradient(circle at top, #1d4ed8 0, #020617 55%, #000 100%);
      color: #e5e7eb;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .app {{
      width: 100%;
      max-width: 480px;
      padding: 16px;
    }}
    .header h1 {{
      margin: 0;
      font-size: 1.45rem;
      letter-spacing: 0.02em;
    }}
    .subtitle {{
      margin: 4px 0 16px;
      font-size: 0.9rem;
      color: #9ca3af;
    }}
    .card {{
      background: rgba(15, 23, 42, 0.92);
      border-radius: 24px;
      padding: 20px 18px 18px;
      box-shadow: 0 18px 45px rgba(0, 0, 0, 0.7);
      border: 1px solid rgba(148, 163, 184, 0.4);
      backdrop-filter: blur(18px);
    }}
    .chip-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.9);
      border: 1px solid rgba(96, 165, 250, 0.8);
      font-size: 0.9rem;
    }}
    .live-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      background: rgba(22, 101, 52, 0.9);
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #bbf7d0;
    }}
    .dot {{
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: #bbf7d0;
      box-shadow: 0 0 8px #bbf7d0;
      animation: pulse 1.2s infinite ease-out;
    }}
    .status-text {{
      margin-top: 18px;
      font-size: 1.02rem;
      font-weight: 600;
      transition: color 0.25s ease, text-shadow 0.25s ease;
    }}
    .status-text.status-far {{
      color: #e5e7eb;
      text-shadow: none;
    }}
    .status-text.status-near {{
      color: #fde68a;
      text-shadow: 0 0 12px rgba(250, 204, 21, 0.6);
    }}
    .status-text.status-immediate {{
      color: #bbf7d0;
      text-shadow: 0 0 16px rgba(16, 185, 129, 0.9);
    }}
    .meta-grid {{
      display: flex;
      margin-top: 16px;
      gap: 12px;
    }}
    .meta-item {{
      flex: 1;
      background: rgba(15, 23, 42, 0.9);
      border-radius: 14px;
      padding: 10px 12px;
      border: 1px solid rgba(55, 65, 81, 0.9);
    }}
    .meta-label {{
      display: block;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: #9ca3af;
      margin-bottom: 4px;
    }}
    .meta-value {{
      font-size: 0.95rem;
      font-weight: 500;
    }}
    .hint {{
      margin-top: 16px;
      font-size: 0.78rem;
      color: #9ca3af;
      line-height: 1.5;
    }}
    .name-row {{
      margin-top: 14px;
      display: flex;
      gap: 8px;
      align-items: center;
    }}
    .name-row input {{
      flex: 1;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid rgba(148,163,184,0.7);
      background: rgba(15,23,42,0.95);
      color: #e5e7eb;
    }}
    .name-row button {{
      padding: 6px 12px;
      border-radius: 999px;
      border: 1px solid #4f46e5;
      background:#4f46e5;
      color:#e5e7eb;
      font-size:0.8rem;
      cursor:pointer;
    }}
    @keyframes pulse {{
      0% {{ transform: scale(1); opacity: 1; }}
      100% {{ transform: scale(1.8); opacity: 0; }}
    }}
  </style>
</head>
<body>
  <div class=\"app\">
    <header class=\"header\">
      <h1>Near Home Tracker</h1>
      <p class=\"subtitle\">Live geofence from your phone</p>
    </header>

    <main class="card">
      <div class="chip-row">
        <div class="chip">
          <span>📱</span>
          <span id="deviceName"></span>
        </div>
        <div class="live-pill">
          <span class="dot"></span>
          <span>Live</span>
        </div>
      </div>

      <div class="name-row">
        <input id="deviceInput" placeholder="Name this device (e.g. Sam's Phone)" />
        <button id="saveDeviceName">Save</button>
      </div>

      <p id="status" class="status-text">Waiting for location permission...</p>

      <div class=\"meta-grid\">
        <div class=\"meta-item\">
          <span class=\"meta-label\">Last GPS</span>
          <span id=\"lastGps\" class=\"meta-value\">--</span>
        </div>
        <div class=\"meta-item\">
          <span class=\"meta-label\">Last server update</span>
          <span id=\"lastServer\" class=\"meta-value\">--</span>
        </div>
      </div>

      <p class=\"hint\">
        Keep this tab open while you move around. We'll keep sending location
        updates in the background so your home dashboard stays in sync.
      </p>
    </main>
  </div>

  <script>
    const params = new URLSearchParams(window.location.search);
    let storedDevice = null;
    try {
      storedDevice = window.localStorage.getItem('geofence_device_name') || null;
    } catch (e) {
      storedDevice = null;
    }
    let deviceId = params.get('device') || storedDevice || 'device1';
    const deviceNameEl = document.getElementById('deviceName');
    const deviceInputEl = document.getElementById('deviceInput');
    deviceNameEl.innerText = deviceId;
    if (deviceInputEl) {
      deviceInputEl.value = deviceId;
    }

    const statusEl = document.getElementById('status');
    const lastGpsEl = document.getElementById('lastGps');
    const lastServerEl = document.getElementById('lastServer');

    function setStatus(text) {{
      statusEl.textContent = text;
    }}

    const saveDeviceBtn = document.getElementById('saveDeviceName');
    if (saveDeviceBtn && deviceInputEl) {
      saveDeviceBtn.addEventListener('click', () => {
        const newId = deviceInputEl.value.trim() || 'device1';
        deviceId = newId;
        deviceNameEl.textContent = newId;
        try {
          window.localStorage.setItem('geofence_device_name', newId);
        } catch (e) {}
      });
    }

    if (!('geolocation' in navigator)) {
      setStatus('Geolocation is not supported on this device/browser.');
    }} else {{
      setStatus('Requesting location access...');
      navigator.geolocation.watchPosition(
        (pos) => {{
          const lat = pos.coords.latitude;
          const lon = pos.coords.longitude;
          const ts = pos.timestamp || Date.now();
          try {{
            lastGpsEl.textContent = new Date(ts).toLocaleTimeString();
          }} catch (e) {{
            lastGpsEl.textContent = 'now';
          }}

          fetch('/api/update_location', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ device_id: deviceId, lat, lon }})
          }})
          .then(r => r.json())
          .then(data => {{
            if (data.status) {{
              let text = 'Status: ' + data.status;
              if (data.distance_m != null) {{
                text += ' • ' + Math.round(data.distance_m) + ' m away';
              }}
              setStatus(text);

              if (data.updated_at) {{
                try {{
                  lastServerEl.textContent = new Date(data.updated_at).toLocaleTimeString();
                }} catch (e) {{
                  lastServerEl.textContent = data.updated_at;
                }}
              }}
            }} else {{
              setStatus('Sent location, waiting for server response...');
            }}
          }})
          .catch(err => setStatus('Error sending location: ' + err));
        }},
        (err) => {{
          setStatus('Location error: ' + err.message);
        }},
        {{ enableHighAccuracy: true, maximumAge: 1000, timeout: 10000 }}
      );
    }}
  </script>
</body>
</html>
"""
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(device: Optional[str] = Query(None)):
    # Dashboard that polls backend and speaks when status becomes Immediate.
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>Geofence Dashboard</title>
  <style>
    body {{ font-family: system-ui, sans-serif; padding: 1.5rem; background: radial-gradient(circle at top, #020617 0, #020617 40%, #000 100%); color:#f5f5f5; }}
    .page {{ max-width: 1120px; margin: 0 auto; }}
    .title {{ font-size: 2rem; margin: 0; background: linear-gradient(120deg,#60a5fa,#a855f7,#ec4899); -webkit-background-clip: text; color: transparent; letter-spacing: 0.03em; }}
    .subtitle {{ margin: 0.2rem 0 1.2rem; color:#9ca3af; }}
    .card {{
      max-width: 480px;
      margin: 1rem auto;
      padding: 1.5rem;
      border-radius: 18px;
      border: 1px solid rgba(148,163,184,0.4);
      background: radial-gradient(circle at top,#1f2937,#020617);
      text-align: center;
      box-shadow: 0 20px 45px rgba(0,0,0,0.7);
    }}
    .status-far {{ background: radial-gradient(circle at top,#111827,#020617); }}
    .status-near {{ background: radial-gradient(circle at top,#1d3557,#020617); }}
    .status-immediate {{ background: radial-gradient(circle at top,#14532d,#020617); }}
    .settings {{
      max-width: 640px;
      margin: 1rem auto 0.5rem;
      padding: 0.75rem 1rem;
      border-radius: 10px;
      border: 1px solid #374151;
      background: #111827;
      font-size: 0.9rem;
    }}
    .settings h2 {{
      margin: 0 0 0.5rem;
      font-size: 0.95rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #9ca3af;
    }}
    .settings-row {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.5rem;
    }}
    .settings-row input[type="number"] {{
      width: 110px;
      padding: 0.25rem 0.4rem;
      border-radius: 6px;
      border: 1px solid #4b5563;
      background: #020617;
      color: #e5e7eb;
    }}
    .settings-row button {{
      padding: 0.25rem 0.75rem;
      border-radius: 999px;
      border: 1px solid #2563eb;
      background: #1d4ed8;
      color: #e5e7eb;
      cursor: pointer;
      font-size: 0.8rem;
    }}
    .settings small {{
      display: block;
      margin-top: 0.35rem;
      color: #6b7280;
    }}
    #deviceInput {{
      padding: 0.3rem 0.5rem;
      border-radius: 999px;
      border: 1px solid #4b5563;
      background:#020617;
      color:#e5e7eb;
    }}
    #saveDevice {{
      padding: 0.3rem 0.9rem;
      border-radius: 999px;
      border: 1px solid #6366f1;
      background:#4f46e5;
      color:#e5e7eb;
      cursor:pointer;
    }}
    #deviceList button {{
      margin: 2px;
      padding: 0.25rem 0.7rem;
      border-radius: 999px;
      border: 1px solid #374151;
      background:#020617;
      color:#e5e7eb;
      cursor:pointer;
      font-size:0.8rem;
    }}
    #deviceList button.active-device {{
      border-color:#22c55e;
      background:rgba(34,197,94,0.15);
      color:#bbf7d0;
    }}
    #map {{
      height: 480px;
      max-width: 960px;
      margin: 1.5rem auto;
      border-radius: 16px;
      overflow: hidden;
      box-shadow: 0 0 18px rgba(0,0,0,0.6);
    }}
  </style>
  <link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\" integrity=\"sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=\" crossorigin=\"\" />
  <script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\" integrity=\"sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=\" crossorigin=\"\"></script>
</head>
<body>
  <h1>Geofence Dashboard</h1>
  <p>Keep this open on your home PC. It tracks one device by name.</p>

  <section class="settings">
    <h2>Home &amp; near‑me alert</h2>
    <div class="settings-row">
      <label>
        Near‑me alert radius (m):
        <input id="immediateRadiusInput" type="number" min="10" max="10000" step="10" />
      </label>
      <button id="saveConfig">Save radius</button>
      <button id="setHomeFromDevice">Set home from this device</button>
    </div>
    <small>This controls when the dashboard says the device is "home" based on distance to your home anchor.</small>
  </section>

  <div>
    <label>Device name: <input id=\"deviceInput\" /></label>
    <button id=\"saveDevice\">Use</button>
  </div>

  <div style=\"margin-top:1rem;\">
    <strong>Active devices:</strong>
    <ul id=\"deviceList\" style=\"list-style:none;padding-left:0;\"></ul>
  </div>

  <div id=\"card\" class=\"card status-far\">
    <h2 id=\"cardTitle\">Device</h2>
    <p id=\"cardStatus\">Status: unknown</p>
    <p id=\"cardDistance\">Distance: -- m</p>
    <p id=\"cardUpdated\">Last update: --</p>
  </div>

  <div id=\"map\"></div>

  <script>
    const params = new URLSearchParams(window.location.search);
    let deviceId = {repr(device) if device else 'null'} || params.get('device') || 'device1';

    const deviceInput = document.getElementById('deviceInput');
    const saveBtn = document.getElementById('saveDevice');
    const deviceList = document.getElementById('deviceList');
    const immediateRadiusInput = document.getElementById('immediateRadiusInput');
    const saveConfigBtn = document.getElementById('saveConfig');
    const setHomeBtn = document.getElementById('setHomeFromDevice');
    const card = document.getElementById('card');
    const cardTitle = document.getElementById('cardTitle');
    const cardStatus = document.getElementById('cardStatus');
    const cardDistance = document.getElementById('cardDistance');
    const cardUpdated = document.getElementById('cardUpdated');

    deviceInput.value = deviceId;
    cardTitle.textContent = deviceId;

    let lastStatus = 'unknown';

    // Leaflet map setup
    let HOME_LAT = {HOME_LAT};
    let HOME_LON = {HOME_LON};
    let HOME_RADIUS_M = {HOME_RADIUS_M};
    let NEAR_RADIUS_M = {NEAR_RADIUS_M};

    if (immediateRadiusInput) {{
      immediateRadiusInput.value = HOME_RADIUS_M;
    }}

    const map = L.map('map').setView([HOME_LAT, HOME_LON], 14);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }}).addTo(map);

    const homeMarker = L.marker([HOME_LAT, HOME_LON]).addTo(map).bindPopup('Home');
    const homeCircleImmediate = L.circle([HOME_LAT, HOME_LON], {{ radius: HOME_RADIUS_M, color: '#2e7d32', fillOpacity: 0.15 }}).addTo(map);
    const homeCircleNear = L.circle([HOME_LAT, HOME_LON], {{ radius: NEAR_RADIUS_M, color: '#1976d2', fillOpacity: 0.08 }}).addTo(map);

    let deviceMarker = null;

    function speak(text) {{
      if (!('speechSynthesis' in window)) return;
      const utter = new SpeechSynthesisUtterance(text);
      utter.rate = 1.0;
      window.speechSynthesis.speak(utter);
    }}

    function applyStatus(status) {{
      card.classList.remove('status-far', 'status-near', 'status-immediate');
      if (status === 'immediate') card.classList.add('status-immediate');
      else if (status === 'near') card.classList.add('status-near');
      else card.classList.add('status-far');
      cardStatus.textContent = 'Status: ' + status;
    }}

    async function poll() {{
      try {{
        const resp = await fetch('/api/state?device_id=' + encodeURIComponent(deviceId));
        const data = await resp.json();
        const status = data.status || 'unknown';
        const dist = data.distance_m != null ? Math.round(data.distance_m) : null;
        const updated = data.updated_at || '--';

        if (data.lat != null && data.lon != null) {{
          const lat = data.lat;
          const lon = data.lon;
          if (!deviceMarker) {{
            deviceMarker = L.marker([lat, lon]).addTo(map).bindPopup('Device: ' + deviceId);
          }} else {{
            deviceMarker.setLatLng([lat, lon]);
          }}
        }}

        applyStatus(status);
        if (dist !== null) {
          if (dist <= 1) {
            cardDistance.textContent = 'Distance: at home anchor';
          } else {
            cardDistance.textContent = 'Distance: ' + dist + ' m';
          }
        } else {
          cardDistance.textContent = 'Distance: -- m';
        }
        cardUpdated.textContent = 'Last update: ' + updated;

        if (lastStatus !== 'immediate' && status === 'immediate') {{
          speak('Alert: ' + deviceId + ' has arrived home.');
        }}
        lastStatus = status;
      }} catch (err) {{
        cardStatus.textContent = 'Status: error contacting server';
      }}
    }}

    saveBtn.addEventListener('click', () => {{
      deviceId = deviceInput.value || 'device1';
      cardTitle.textContent = deviceId;
      lastStatus = 'unknown';
    }});

    if (saveConfigBtn && immediateRadiusInput) {{
      saveConfigBtn.addEventListener('click', async () => {{
        const val = parseFloat(immediateRadiusInput.value);
        if (!isFinite(val) || val <= 0) return;
        const originalText = saveConfigBtn.textContent;
        saveConfigBtn.textContent = 'Saving…';
        saveConfigBtn.disabled = true;
        try {{
          const resp = await fetch('/api/config', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ immediate_radius_m: val }})
          }});
          const data = await resp.json();
          if (resp.ok && data.immediate_radius_m != null) {{
            HOME_RADIUS_M = data.immediate_radius_m;
            homeCircleImmediate.setRadius(HOME_RADIUS_M);
          }}
        }} catch (err) {{
          // ignore config errors in UI
        }} finally {{
          saveConfigBtn.disabled = false;
          saveConfigBtn.textContent = originalText;
        }}
      }});
    }}

    if (setHomeBtn) {{
      setHomeBtn.addEventListener('click', async () => {{
        const originalText = setHomeBtn.textContent;
        setHomeBtn.textContent = 'Setting…';
        setHomeBtn.disabled = true;
        try {{
          const resp = await fetch('/api/set_home_from_device', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ device_id: deviceId }})
          }});
          const data = await resp.json();
          if (resp.ok && data.home_lat != null && data.home_lon != null) {{
            HOME_LAT = data.home_lat;
            HOME_LON = data.home_lon;
            map.setView([HOME_LAT, HOME_LON], 14);
            homeMarker.setLatLng([HOME_LAT, HOME_LON]);
            homeCircleImmediate.setLatLng([HOME_LAT, HOME_LON]);
            homeCircleNear.setLatLng([HOME_LAT, HOME_LON]);
          }}
        }} catch (err) {{
          // ignore errors; user can retry
        }} finally {{
          setHomeBtn.disabled = false;
          setHomeBtn.textContent = originalText;
        }}
      }});
    }}

    async function refreshDeviceList() {{
      try {{
        const resp = await fetch('/api/devices');
        const data = await resp.json();
        const devices = data.devices || [];
        deviceList.innerHTML = '';
        let firstBtn = null;
        let matched = false;
        devices.forEach(id => {{
          const li = document.createElement('li');
          const btn = document.createElement('button');
          btn.textContent = id;
          if (!firstBtn) firstBtn = btn;
          if (id === deviceId) {{
            btn.classList.add('active-device');
            matched = true;
          }}
          btn.addEventListener('click', () => {{
            deviceId = id;
            deviceInput.value = id;
            cardTitle.textContent = id;
            lastStatus = 'unknown';
            document.querySelectorAll('#deviceList button').forEach(b => b.classList.remove('active-device'));
            btn.classList.add('active-device');
          }});
          li.appendChild(btn);
          deviceList.appendChild(li);
        }});
        if (!matched && firstBtn) {{
          firstBtn.click();
        }}
      }} catch (err) {{
        // ignore listing errors in UI
      }}
    }}

    poll();
    setInterval(poll, 1000);
    refreshDeviceList();
    setInterval(refreshDeviceList, 7000);
  </script>
</body>
</html>
"""
    )
