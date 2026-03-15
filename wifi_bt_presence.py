"""Wi-Fi-only presence monitor with neural voice alerts.

    Far       -> Device not seen on Wi-Fi subnet.
    Immediate -> Device seen on Wi-Fi subnet.

Note: Scapy ARP scanning usually requires administrator privileges.
"""

import asyncio
import platform
import subprocess
from dataclasses import dataclass
from typing import Optional
import edge_tts
import pygame
from pathlib import Path


# ---------------- Configuration ----------------

SUBNET = "192.168.1.0/24"  # Adjust to your LAN
TARGET_WIFI_MAC = "AA:BB:CC:DD:EE:FF"  # <-- replace with actual Wi-Fi MAC

WIFI_SCAN_INTERVAL = 10    # seconds

VOICE_NAME = "en-GB-SoniaNeural"  # or "en-US-GuyNeural"
ALERT_AUDIO_FILE = Path("presence_alert.mp3")


# ---------------- State ----------------


@dataclass
class PresenceState:
    wifi_present: bool = False
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
    """Derive status from Wi-Fi presence only.

    - Far: device not seen on Wi-Fi subnet.
    - Immediate: device seen on Wi-Fi subnet.
    """
    return "Immediate" if s.wifi_present else "Far"


async def update_status_and_maybe_alert() -> None:
    global state
    async with _state_lock:
        new_status = compute_status(state)
        if new_status != state.status:
            print(f"[STATE] {state.status} -> {new_status} (Wi-Fi={state.wifi_present})")
            state.last_status = state.status
            state.status = new_status

            # Fire alert only on transition into Immediate
            if new_status == "Immediate" and state.last_status != "Immediate":
                asyncio.create_task(speak_immediate_alert())


# ---------------- Scanners ----------------


def is_wifi_mac_present(mac: str, subnet: str) -> bool:  # subnet kept for signature compatibility
    """Check if a MAC is in the local ARP table.

    On Windows this uses `arp -a` so it does not require WinPcap/Npcap.
    The `subnet` argument is currently unused but retained for compatibility.
    """
    mac = mac.lower()

    # Windows implementation via system ARP cache
    if platform.system().lower() == "windows":
        try:
            result = subprocess.run(
                ["arp", "-a"],
                capture_output=True,
                text=True,
                check=True,
            )
            output = result.stdout.lower()
            return mac in output
        except Exception as exc:  # noqa: BLE001
            print(f"[Wi-Fi] arp -a failed: {exc}")
            return False

    # Fallback: not Windows; simply report False instead of raising
    # (can be extended later with platform-specific logic)
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


# ---------------- Main ----------------


async def main() -> None:
    print("Starting Wi-Fi presence monitor (no Bluetooth)...")
    print(f" Target Wi-Fi MAC: {TARGET_WIFI_MAC}")

    wifi_task = asyncio.create_task(wifi_scanner_loop(), name="wifi_scanner")

    try:
        await asyncio.gather(wifi_task)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down monitor...")
