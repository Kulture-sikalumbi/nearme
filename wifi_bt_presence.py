"""Concurrent Wi-Fi + Bluetooth presence monitor.

- Wi-Fi scanner: uses scapy.all.arping to detect if a given MAC is on 192.168.1.0/24.
- Bluetooth scanner: uses bleak to read RSSI for a given BLE MAC.
- State machine:
    Far       -> Wi-Fi seen (or default) but no Bluetooth.
    Near      -> Bluetooth detected, RSSI <= -60 dB.
    Immediate -> Bluetooth detected, RSSI > -60 dB.
- Audio: edge-tts speaks an alert only when status changes INTO "Immediate".

Note: Scapy ARP scanning usually requires administrator privileges.
"""

import asyncio
from dataclasses import dataclass
from typing import Optional

from bleak import BleakScanner
from scapy.all import arping, conf  # type: ignore
import edge_tts
import pygame
from pathlib import Path


# ---------------- Configuration ----------------

SUBNET = "192.168.1.0/24"  # Adjust to your LAN
TARGET_WIFI_MAC = "AA:BB:CC:DD:EE:FF"  # <-- replace with actual Wi-Fi MAC
TARGET_BT_MAC = "11:22:33:44:55:66"    # <-- replace with actual Bluetooth MAC

IMMEDIATE_THRESHOLD = -60  # dB
WIFI_SCAN_INTERVAL = 10    # seconds
BT_SCAN_INTERVAL = 5       # seconds

VOICE_NAME = "en-GB-SoniaNeural"  # or "en-US-GuyNeural"
ALERT_AUDIO_FILE = Path("presence_alert.mp3")


# ---------------- State ----------------


@dataclass
class PresenceState:
    wifi_present: bool = False
    bt_rssi: Optional[int] = None
    status: str = "Far"
    last_status: str = "Far"


state = PresenceState()
_state_lock = asyncio.Lock()


# ---------------- Helpers ----------------


def init_audio() -> None:
    if not pygame.mixer.get_init():
        pygame.mixer.init()


async def speak_immediate_alert() -> None:
    """Generate and play an edge-tts alert for Immediate status."""
    message = "Alert: user is approaching the hub."

    try:
        communicate = edge_tts.Communicate(message, VOICE_NAME)
        await communicate.save(str(ALERT_AUDIO_FILE))
    except Exception as exc:  # noqa: BLE001
        print(f"[TTS] Error generating audio: {exc}")
        return

    try:
        init_audio()
        sound = pygame.mixer.Sound(str(ALERT_AUDIO_FILE))
        sound.play()
        print("[AUDIO] Playing Immediate alert.")
    except Exception as exc:  # noqa: BLE001
        print(f"[AUDIO] Error playing audio: {exc}")


def compute_status(s: PresenceState) -> str:
    """Derive status from Wi-Fi presence and BT RSSI."""
    if s.bt_rssi is not None:
        if s.bt_rssi > IMMEDIATE_THRESHOLD:
            return "Immediate"
        return "Near"
    # No Bluetooth; treat Wi-Fi presence as Far presence state
    if s.wifi_present:
        return "Far"
    return "Far"


async def update_status_and_maybe_alert() -> None:
    global state
    async with _state_lock:
        new_status = compute_status(state)
        if new_status != state.status:
            print(f"[STATE] {state.status} -> {new_status} (Wi-Fi={state.wifi_present}, RSSI={state.bt_rssi})")
            state.last_status = state.status
            state.status = new_status

            # Fire alert only on transition into Immediate
            if new_status == "Immediate" and state.last_status != "Immediate":
                asyncio.create_task(speak_immediate_alert())


# ---------------- Scanners ----------------


def is_wifi_mac_present(mac: str, subnet: str) -> bool:
    """Synchronous ARP scan to check if a MAC is on the subnet."""
    mac = mac.lower()
    conf.verb = 0
    try:
        ans, _ = arping(subnet, timeout=2, verbose=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[Wi-Fi] ARP scan failed: {exc}")
        return False

    for _, rcv in ans:
        if getattr(rcv, "hwsrc", "").lower() == mac:
            return True
    return False


async def wifi_scanner_loop() -> None:
    global state
    while True:
        present = await asyncio.to_thread(is_wifi_mac_present, TARGET_WIFI_MAC, SUBNET)
        async with _state_lock:
            state.wifi_present = present
        print(f"[Wi-Fi] {TARGET_WIFI_MAC} present={present}")
        await update_status_and_maybe_alert()
        await asyncio.sleep(WIFI_SCAN_INTERVAL)


async def bt_scanner_loop() -> None:
    global state
    while True:
        try:
            devices = await BleakScanner.discover(timeout=BT_SCAN_INTERVAL)
            rssi: Optional[int] = None
            for d in devices:
                if d.address.upper() == TARGET_BT_MAC.upper():
                    rssi = d.rssi
                    break
            async with _state_lock:
                state.bt_rssi = rssi
            print(f"[BT] {TARGET_BT_MAC} RSSI={rssi}")
        except Exception as exc:  # noqa: BLE001
            print(f"[BT] Scan error: {exc}")
        await update_status_and_maybe_alert()
        await asyncio.sleep(BT_SCAN_INTERVAL)


# ---------------- Main ----------------


async def main() -> None:
    print("Starting Wi-Fi + Bluetooth presence monitor...")
    print(f" Target Wi-Fi MAC: {TARGET_WIFI_MAC}")
    print(f" Target BLE MAC : {TARGET_BT_MAC}")

    wifi_task = asyncio.create_task(wifi_scanner_loop(), name="wifi_scanner")
    bt_task = asyncio.create_task(bt_scanner_loop(), name="bt_scanner")

    try:
        await asyncio.gather(wifi_task, bt_task)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down monitor...")
