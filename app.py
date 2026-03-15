import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import streamlit as st
from bleak import BleakScanner
import pygame
import edge_tts


DEVICES_FILE = Path("devices.json")
SCAN_INTERVAL_SECONDS = 5
IMMEDIATE_THRESHOLD = -60  # dB
NEAR_THRESHOLD = -80  # dB
ALERT_COOLDOWN_SECONDS = 300  # only alert once per 5 minutes per device
RSSI_HISTORY_LENGTH = 50
VOICE_NAME = "en-GB-SoniaNeural"


@dataclass
class TrackedDevice:
    bt_mac: str
    nickname: str
    wifi_mac: Optional[str] = None
    last_rssi: Optional[int] = None
    last_seen: Optional[datetime] = None
    last_alert: Optional[datetime] = None
    status: str = "Scanning..."  # Scanning..., Device Detected, Immediate
    rssi_history: List[int] = field(default_factory=list)


@dataclass
class ScannerState:
    devices: Dict[str, TrackedDevice] = field(default_factory=dict)
    recent_arrivals: List[Dict] = field(default_factory=list)
    most_recent_mac: Optional[str] = None
    scanning: bool = False


# ---------- Persistence ----------


def load_registered_devices() -> Dict[str, TrackedDevice]:
    devices: Dict[str, TrackedDevice] = {}
    if DEVICES_FILE.exists():
        try:
            data = json.loads(DEVICES_FILE.read_text("utf-8"))
            for item in data:
                # Backward-compatible: accept old {"mac": ...} and new {"bt_mac": ..., "wifi_mac": ...}
                bt_mac_raw = item.get("bt_mac") or item.get("mac")
                wifi_mac_raw = item.get("wifi_mac")
                bt_mac = bt_mac_raw.upper() if bt_mac_raw else ""
                wifi_mac = wifi_mac_raw.upper() if wifi_mac_raw else None
                nickname = item.get("nickname") or bt_mac or wifi_mac or "Device"
                if bt_mac:
                    devices[bt_mac] = TrackedDevice(bt_mac=bt_mac, wifi_mac=wifi_mac, nickname=nickname)
        except Exception:
            pass
    return devices


def save_registered_devices(devices: Dict[str, TrackedDevice]) -> None:
    payload = [
        {"bt_mac": td.bt_mac, "wifi_mac": td.wifi_mac, "nickname": td.nickname}
        for td in devices.values()
    ]
    DEVICES_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------- Audio ----------


@st.cache_resource
def init_pygame():
    pygame.mixer.init()
    return True


async def play_alert(nickname: str, device: TrackedDevice):
    now = datetime.utcnow()
    if device.last_alert and now - device.last_alert < timedelta(seconds=ALERT_COOLDOWN_SECONDS):
        return

    message = f"Alert: {nickname} is approaching the hub."

    # Synthesize speech with neural voice using edge-tts
    try:
        communicate = edge_tts.Communicate(message, VOICE_NAME)
        output_path = str(Path("tts_alert.mp3"))
        await communicate.save(output_path)

        # Play synthesized audio via pygame
        try:
            init_pygame()
            sound = pygame.mixer.Sound(output_path)
            sound.play()
        except Exception:
            pass
    except Exception:
        # Swallow TTS errors to avoid breaking the scan loop
        pass

    device.last_alert = now


# ---------- BLE Scanning ----------


async def scan_loop(state: ScannerState, stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            devices = await BleakScanner.discover(timeout=SCAN_INTERVAL_SECONDS)
            now = datetime.utcnow()
            for d in devices:
                mac = d.address.upper()
                if mac in state.devices:
                    tracked = state.devices[mac]
                    rssi = d.rssi or -100
                    tracked.last_rssi = rssi
                    tracked.last_seen = now
                    tracked.rssi_history.append(rssi)
                    if len(tracked.rssi_history) > RSSI_HISTORY_LENGTH:
                        tracked.rssi_history = tracked.rssi_history[-RSSI_HISTORY_LENGTH:]

                    if rssi > IMMEDIATE_THRESHOLD:
                        prev_status = tracked.status
                        tracked.status = "Immediate"
                        state.most_recent_mac = mac
                        if prev_status != "Immediate":
                            await play_alert(tracked.nickname, tracked)
                            state.recent_arrivals.insert(0, {
                                "nickname": tracked.nickname,
                                "timestamp": now.isoformat(timespec="seconds"),
                            })
                            state.recent_arrivals = state.recent_arrivals[:20]
                    elif NEAR_THRESHOLD <= rssi <= IMMEDIATE_THRESHOLD:
                        tracked.status = "Device Detected"
                    else:
                        tracked.status = "Scanning..."
        except Exception:
            # Ignore scan errors and continue
            pass

        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


def start_scanner_background(state: ScannerState):
    if getattr(state, "_scanner_thread", None):
        return

    stop_event = threading.Event()
    state._stop_event = stop_event

    def _runner():
        asyncio.run(scan_loop(state, stop_event))

    thread = threading.Thread(target=_runner, daemon=True)
    state._scanner_thread = thread
    thread.start()


def stop_scanner_background(state: ScannerState):
    stop_event = getattr(state, "_stop_event", None)
    thread = getattr(state, "_scanner_thread", None)
    if stop_event:
        stop_event.set()
    if thread and thread.is_alive():
        thread.join(timeout=1.0)
    state._scanner_thread = None
    state._stop_event = None


# ---------- Streamlit UI ----------


def ensure_state():
    if "scanner_state" not in st.session_state:
        st.session_state.scanner_state = ScannerState()
        st.session_state.scanner_state.devices = load_registered_devices()


def sidebar(state: ScannerState):
    st.sidebar.header("Device Management")

    with st.sidebar.form("add_device"):
        bt_mac = st.text_input("Bluetooth MAC Address").strip().upper()
        wifi_mac = st.text_input("Wi-Fi MAC Address (optional)").strip().upper()
        nickname = st.text_input("Nickname")
        submitted = st.form_submit_button("Add / Update Device")
        if submitted and bt_mac:
            state.devices[bt_mac] = TrackedDevice(
                bt_mac=bt_mac,
                wifi_mac=wifi_mac or None,
                nickname=nickname or bt_mac,
            )
            save_registered_devices(state.devices)
            st.sidebar.success(f"Saved {nickname or bt_mac}")

    if state.devices:
        st.sidebar.subheader("Registered Devices")
        for bt_mac, dev in state.devices.items():
            wifi_info = f" | Wi-Fi: {dev.wifi_mac}" if dev.wifi_mac else ""
            st.sidebar.write(f"{dev.nickname} (BT: {dev.bt_mac}{wifi_info})")

    st.sidebar.markdown("---")

    col1, col2 = st.sidebar.columns(2)
    if col1.button("Start Scan"):
        state.scanning = True
        start_scanner_background(state)
    if col2.button("Stop Scan"):
        state.scanning = False
        stop_scanner_background(state)

    if st.sidebar.button("Scan Nearby (One-shot)"):
        with st.spinner("Scanning..."):
            devices = asyncio.run(BleakScanner.discover(timeout=5.0))
        st.sidebar.write("**Visible Devices:**")
        for d in devices:
            st.sidebar.write(f"{d.name or 'Unknown'} - {d.address} (RSSI {d.rssi})")


def main_dashboard(state: ScannerState):
    st.title("Near Me Alert")

    st.caption("Bluetooth proximity alerts for your marked devices.")

    # Status cards
    if state.devices:
        cols = st.columns(min(3, len(state.devices)))
        for idx, dev in enumerate(state.devices.values()):
            col = cols[idx % len(cols)]
            status_color = "gray"
            if dev.status == "Immediate":
                status_color = "green"
            elif dev.status == "Device Detected":
                status_color = "orange"

            with col:
                st.markdown(
                    f"""
                    <div style='border-radius:8px;padding:12px;margin:4px;\
                    border:1px solid #ccc;background-color:{status_color if status_color!='gray' else '#f5f5f5'};'>
                        <h4>{dev.nickname}</h4>
                        <p><b>Bluetooth MAC:</b> {dev.bt_mac}</p>
                        <p><b>Wi-Fi MAC:</b> {dev.wifi_mac or 'N/A'}</p>
                        <p><b>Status:</b> {dev.status}</p>
                        <p><b>RSSI:</b> {dev.last_rssi if dev.last_rssi is not None else 'N/A'}</p>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
    else:
        st.info("Register at least one device in the sidebar to start tracking.")

    st.markdown("---")

    # Signal graph for most recent device
    if state.most_recent_mac and state.most_recent_mac in state.devices:
        dev = state.devices[state.most_recent_mac]
        st.subheader(f"Signal Strength: {dev.nickname}")
        if dev.rssi_history:
            st.line_chart(dev.rssi_history)
        else:
            st.write("No RSSI data yet.")
    else:
        st.subheader("Signal Strength")
        st.write("Waiting for immediate detections...")

    # Activity log
    st.markdown("---")
    st.subheader("Recent Arrivals")
    if state.recent_arrivals:
        st.table(state.recent_arrivals)
    else:
        st.write("No recent arrivals yet.")

    # Auto-refresh when scanning (lightweight loop)
    if state.scanning:
        time.sleep(1)
        st.experimental_rerun()


def main():
    ensure_state()
    state: ScannerState = st.session_state.scanner_state

    sidebar(state)
    main_dashboard(state)


if __name__ == "__main__":
    main()
