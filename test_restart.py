#!/usr/bin/env python3
"""
Test script to verify OBS restart functionality.

Connects to OBS, auto-detects the capture sources, and triggers a restart on
the first one so you can confirm the restart mechanism works on this machine.
"""

import sys
from obs_display_monitor import OBSDisplayMonitor


def test_restart():
    """Test the restart functionality"""
    print("=== OBS Restart Test ===")

    monitor = OBSDisplayMonitor(all_captures=True)

    if not monitor.connect_to_obs():
        print("❌ Failed to connect to OBS (is OBS open with the WebSocket server enabled?)")
        return False
    print("✅ Connected to OBS")

    monitor.discover_sources(force=True)
    if not monitor.sources:
        print("❌ No capture sources detected")
        monitor.disconnect_from_obs()
        return False
    print(f"✅ Capture sources detected: {monitor.sources}")

    target = monitor.sources[0]
    print(f"\n🔄 Testing restart on '{target}'...")
    success = monitor._restart_capture(target)

    if success:
        print("✅ Restart function executed successfully")
    else:
        print("❌ Restart function failed")

    monitor.disconnect_from_obs()
    return success


if __name__ == "__main__":
    sys.exit(0 if test_restart() else 1)
