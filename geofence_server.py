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
from fastapi.responses import HTMLResponse, JSONResponse
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


# ----- Pages -----


@app.get("/tracker", response_class=HTMLResponse)
async def tracker_page(device: Optional[str] = Query(None)):
    # Basic HTML+JS page that sends geolocation updates to the backend.
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>Geofence Tracker</title>
  <style>
    body {{ font-family: system-ui, sans-serif; padding: 1.5rem; }}
    #status {{ margin-top: 1rem; font-weight: 600; }}
  </style>
</head>
<body>
  <h1>Geofence Tracker</h1>
  <p>This page sends your phone's GPS location to the home hub.</p>
  <p><strong>Device:</strong> <span id=\"deviceName\"></span></p>
  <p id=\"status\">Waiting for location permission...</p>

  <script>
    const params = new URLSearchParams(window.location.search);
    let deviceId = {repr(device) if device else 'null'} || params.get('device') || window.prompt('Enter a device name:') || 'device1';
    document.getElementById('deviceName').innerText = deviceId;

    const statusEl = document.getElementById('status');

    function setStatus(text) {{
      statusEl.textContent = text;
    }}

    if (!('geolocation' in navigator)) {{
      setStatus('Geolocation is not supported on this device/browser.');
    }} else {{
      setStatus('Requesting location access...');
      navigator.geolocation.watchPosition(
        (pos) => {{
          const lat = pos.coords.latitude;
          const lon = pos.coords.longitude;
          fetch('/api/update_location', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ device_id: deviceId, lat, lon }})
          }})
          .then(r => r.json())
          .then(data => {{
            if (data.status) {{
              setStatus(`Status: ${'{'}data.status{'}'} | Distance: ${'{'}Math.round(data.distance_m){'}'} m`);
            }} else {{
              setStatus('Sent location, waiting for server response...');
            }}
          }})
          .catch(err => setStatus('Error sending location: ' + err));
        }},
        (err) => {{
          setStatus('Location error: ' + err.message);
        }},
        {{ enableHighAccuracy: true, maximumAge: 5000, timeout: 10000 }}
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
    body {{ font-family: system-ui, sans-serif; padding: 1.5rem; background:#0b1020; color:#f5f5f5; }}
    .card {{
      max-width: 480px;
      margin: 1rem auto;
      padding: 1.5rem;
      border-radius: 12px;
      border: 1px solid #444;
      background: #1e2435;
      text-align: center;
    }}
    .status-far {{ background:#1e2435; }}
    .status-near {{ background:#2a3b5f; }}
    .status-immediate {{ background:#2e7d32; }}
    #map {{
      height: 320px;
      max-width: 640px;
      margin: 1.5rem auto;
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 0 12px rgba(0,0,0,0.5);
    }}
  </style>
  <link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\" integrity=\"sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=\" crossorigin=\"\" />
  <script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\" integrity=\"sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=\" crossorigin=\"\"></script>
</head>
<body>
  <h1>Geofence Dashboard</h1>
  <p>Keep this open on your home PC. It tracks one device by name.</p>

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
    const card = document.getElementById('card');
    const cardTitle = document.getElementById('cardTitle');
    const cardStatus = document.getElementById('cardStatus');
    const cardDistance = document.getElementById('cardDistance');
    const cardUpdated = document.getElementById('cardUpdated');

    deviceInput.value = deviceId;
    cardTitle.textContent = deviceId;

    let lastStatus = 'unknown';

    // Leaflet map setup
    const HOME_LAT = {HOME_LAT};
    const HOME_LON = {HOME_LON};
    const HOME_RADIUS_M = {HOME_RADIUS_M};
    const NEAR_RADIUS_M = {NEAR_RADIUS_M};

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
        cardDistance.textContent = 'Distance: ' + (dist !== null ? dist + ' m' : '--');
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

    async function refreshDeviceList() {{
      try {{
        const resp = await fetch('/api/devices');
        const data = await resp.json();
        const devices = data.devices || [];
        deviceList.innerHTML = '';
        devices.forEach(id => {{
          const li = document.createElement('li');
          const btn = document.createElement('button');
          btn.textContent = id;
          btn.style.margin = '2px';
          btn.addEventListener('click', () => {{
            deviceId = id;
            deviceInput.value = id;
            cardTitle.textContent = id;
            lastStatus = 'unknown';
          }});
          li.appendChild(btn);
          deviceList.appendChild(li);
        }});
      }} catch (err) {{
        // ignore listing errors in UI
      }}
    }}

    poll();
    setInterval(poll, 5000);
    refreshDeviceList();
    setInterval(refreshDeviceList, 7000);
  </script>
</body>
</html>
"""
    )
