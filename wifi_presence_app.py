import asyncio
import platform
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import edge_tts
import pygame
import streamlit as st

# ---------- Config ----------

DEFAULT_WIFI_MAC = "AA:BB:CC:DD:EE:FF"  # replace with your device MAC
VOICE_NAME = "en-GB-SoniaNeural"  # or "en-US-GuyNeural"
ALERT_AUDIO_FILE = Path("wifi_presence_alert.mp3")
ALERT_COOLDOWN_SECONDS = 300  # 5 minutes
CHECK_INTERVAL_SECONDS = 10


# ---------- Low-level helpers ----------


def get_arp_entries() -> list[dict]:
    """Return parsed ARP table entries as a list of {ip, mac, type} dicts.

    Implemented for Windows via `arp -a`. MACs are normalised to lowercase
    with ':' separators.
    """
    entries: list[dict] = []

    if platform.system().lower() != "windows":
        st.info("Network scan is currently implemented for Windows only.")
        return entries

    try:
        result = subprocess.run(
            ["arp", "-a"],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception as exc:  # noqa: BLE001
        st.warning(f"arp -a failed: {exc}")
        return entries

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("interface") or line.lower().startswith("internet address"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        ip = parts[0]
        mac = parts[1].replace("-", ":").lower()
        entry_type = parts[2].lower() if len(parts) > 2 else ""

        # Only keep dynamic entries so the list reflects currently
        # learned neighbours instead of old/static records.
        if entry_type != "dynamic":
            continue

        entries.append({"ip": ip, "mac": mac, "type": entry_type})

    return entries


def is_wifi_mac_present(mac: str) -> bool:
    """Check if a MAC is present in the local ARP table on Windows."""
    mac = mac.lower()
    entries = get_arp_entries()
    return any(e["mac"] == mac for e in entries)


def init_audio() -> None:
    if not pygame.mixer.get_init():
        pygame.mixer.init()


async def synthesize_alert(message: str, voice: str, out_path: Path) -> None:
    communicate = edge_tts.Communicate(message, voice)
    await communicate.save(str(out_path))


def play_immediate_alert(nickname: str) -> None:
    now = datetime.utcnow()
    last_alert_ts = st.session_state.get("last_alert_ts")
    if last_alert_ts is not None:
        last_alert_time = datetime.fromisoformat(last_alert_ts)
        if now - last_alert_time < timedelta(seconds=ALERT_COOLDOWN_SECONDS):
            return

    message = f"Alert: {nickname} is approaching the hub."

    try:
        asyncio.run(synthesize_alert(message, VOICE_NAME, ALERT_AUDIO_FILE))
        init_audio()
        sound = pygame.mixer.Sound(str(ALERT_AUDIO_FILE))
        sound.play()
        st.toast("Immediate alert spoken.")
        st.session_state["last_alert_ts"] = now.isoformat()
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Unable to play alert audio: {exc}")


# ---------- Streamlit App ----------


def ensure_state() -> None:
    if "status" not in st.session_state:
        st.session_state.status = "Far"
    if "wifi_present" not in st.session_state:
        st.session_state.wifi_present = False
    if "monitoring" not in st.session_state:
        st.session_state.monitoring = False
    if "last_alert_ts" not in st.session_state:
        st.session_state.last_alert_ts = None
    if "arp_devices" not in st.session_state:
        st.session_state.arp_devices = []
    if "tracked_mac" not in st.session_state:
        st.session_state.tracked_mac = DEFAULT_WIFI_MAC


def main() -> None:
    st.set_page_config(page_title="Wi-Fi Presence Monitor", page_icon="📶", layout="centered")
    ensure_state()

    st.title("Wi-Fi Presence Monitor")
    st.caption("Neural-voice alerts when a device appears on your Wi-Fi.")

    with st.sidebar:
        st.header("Settings")
        nickname = st.text_input("Device Nickname", value="User")
        # Text input is bound to the tracked_mac value but does not use
        # a custom key so we can safely update tracked_mac in state.
        wifi_mac_input = st.text_input("Wi-Fi MAC Address", value=st.session_state.tracked_mac).strip().upper()
        if wifi_mac_input:
            st.session_state.tracked_mac = wifi_mac_input

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Start", disabled=st.session_state.monitoring):
                st.session_state.monitoring = True
                st.session_state.status = "Far"
        with col2:
            if st.button("Stop", disabled=not st.session_state.monitoring):
                st.session_state.monitoring = False

        st.write(f"Current status: **{st.session_state.status}**")
        st.write(f"Monitoring: **{st.session_state.monitoring}**")

        st.markdown("---")
        st.subheader("Scan Network Devices")
        if st.button("Scan ARP Table"):
            st.session_state.arp_devices = get_arp_entries()

        devices = st.session_state.arp_devices
        if devices:
            # Build labels like "IP - MAC (type)" for selection
            labels = [f"{d['ip']} - {d['mac'].upper()} ({d['type']})" for d in devices]
            label_to_mac = {label: devices[idx]["mac"].upper() for idx, label in enumerate(labels)}
            selected_label = st.selectbox("Pick a device to track", labels, key="arp_select")
            if st.button("Use Selected MAC") and selected_label:
                st.session_state.tracked_mac = label_to_mac[selected_label]
                st.success(f"Tracking MAC {label_to_mac[selected_label]}")

    # Core status card
    wifi_mac = st.session_state.tracked_mac
    status_color = "#e0e0e0" if st.session_state.status == "Far" else "#4caf50"
    st.markdown(
        f"""
        <div style='border-radius:12px;padding:16px;border:1px solid #ccc;background-color:{status_color};text-align:center;color:#000000;'>
            <h2 style='color:#000000;'>{nickname}</h2>
            <p style='color:#000000;'><b>Wi-Fi MAC:</b> {wifi_mac or 'Not set'}</p>
            <p style='color:#000000;'><b>Status:</b> {st.session_state.status}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if st.session_state.monitoring and wifi_mac:
        present = is_wifi_mac_present(wifi_mac)
        previous_status = st.session_state.status
        st.session_state.wifi_present = present
        st.session_state.status = "Immediate" if present else "Far"

        st.write(f"Wi-Fi present: **{present}** (checked at {datetime.now().strftime('%H:%M:%S')})")

        # Fire alert only on transition into Immediate
        if previous_status != "Immediate" and st.session_state.status == "Immediate":
            play_immediate_alert(nickname)

        # Simple polling loop: wait then rerun
        time.sleep(CHECK_INTERVAL_SECONDS)
        st.rerun()
    else:
        st.info("Set MAC and click Start to begin monitoring.")


if __name__ == "__main__":
    main()
