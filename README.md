# OBS Display Capture Monitor for macOS

🔧 **Solution for frozen display capture in OBS on macOS!**

This script automatically detects and fixes the common issue where OBS display capture freezes on macOS. It works by monitoring the display capture output and automatically restarting it when frozen.

## The Problem This Solves

On macOS, OBS display capture sources often freeze after some time, requiring manual intervention to restart the capture. This script automates the fix by:
1. Detecting frozen frames by comparing consecutive screenshots
2. Automatically triggering the "Restart Capture" function
3. Keeping your stream/recording running smoothly

## Example Output

Here is what the script's output looks like in your terminal when it detects and restarts a frozen source.

```log
$ python obs_display_monitor.py --source "Safari"
OBS Display Capture Monitor
Monitoring source: Safari
OBS WebSocket: localhost:4455
Press Ctrl+C to stop
--------------------------------------------------
2025-06-24 08:19:22,490 - INFO - Starting OBS Display Capture Monitor
2025-06-24 08:19:22,491 - INFO - Connecting to OBS WebSocket at localhost:4455
2025-06-24 08:19:22,498 - INFO - Connected to OBS Studio 31.0.3
2025-06-24 08:19:22,498 - INFO - Checking if source 'Safari' exists...
2025-06-24 08:19:22,499 - INFO - Found 4 inputs in OBS: ['Mic/Aux', 'gopro', 'logi webcam', 'Safari']
2025-06-24 08:19:22,499 - INFO - ✓ Source 'Safari' found in OBS
2025-06-24 08:19:22,499 - INFO - Starting monitor loop for source 'Safari'
2025-06-24 08:19:22,499 - INFO - Monitor interval: 1.0s, Stuck threshold: 1 frames
2025-06-24 08:19:22,499 - INFO - === Check #1 ===
2025-06-24 08:19:23,532 - INFO - === Check #2 ===
2025-06-24 08:19:23,577 - WARNING - ⚠️  Identical frame detected (count: 1/1)
2025-06-24 08:19:23,577 - WARNING - ❗ Display capture appears to be frozen
2025-06-24 08:19:23,577 - WARNING - 
==================================================
2025-06-24 08:19:23,577 - WARNING - 🔄 RESTARTING DISPLAY CAPTURE
2025-06-24 08:19:23,577 - WARNING - Source: Safari
2025-06-24 08:19:23,577 - WARNING - Time: 2025-06-24 08:19:23
2025-06-24 08:19:24,082 - WARNING - ✅ Restart completed
2025-06-24 08:19:24,082 - WARNING - ==================================================

2025-06-24 08:19:25,090 - INFO - === Check #3 ===
2025-06-24 08:19:26,149 - INFO - === Check #4 ===
```

As you can see, the script logs every check. When it finds an identical frame, it logs a warning and immediately attempts a restart. After a successful restart, it goes back to its normal monitoring routine.

## Quick Start

1. **Install Python** (if you haven't already):
   - Download and install Python 3.7 or later from [python.org](https://python.org)
   - Make sure to check "Add Python to PATH" during installation

2. **Download this script**:
   ```bash
   git clone git@github.com:yayuanli/OBS_Restart_Capture_Stuck_Source.git
   cd OBS_Restart_Capture_Stuck_Source
   ```

3. **Install dependencies**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On macOS/Linux
   pip install -r requirements.txt
   ```

4. **Configure OBS WebSocket**:
   - Open OBS
   - Go to Tools → WebSocket Server Settings
   - Enable WebSocket server
   - Set a password (optional but recommended)
   - Note down the port number (default is 4455)

5. **Run the script**:
   
   It's recommended to use [tmux](https://github.com/tmux/tmux/wiki) to leave following command running in the backend robustly. 
   ```bash
   # Basic usage
   python obs_display_monitor.py --source "Your Display Capture Source Name"

   # With more options
   python obs_display_monitor.py --source "My Other Screen" --interval 0.5 --threshold 3
   ```
## Configuration

All configuration is done via command-line arguments. Here are the most common ones:

| Argument | Default | Description |
|---|---|---|
| `--source` | "Safari" | The name of your Display Capture source in OBS. |
| `--host` | "localhost" | The hostname or IP address of the computer running OBS. |
| `--port` | 4455 | The port for the OBS WebSocket server. |
| `--password` | "" | The password for the OBS WebSocket server. |
| `--interval` | 1.0 | How often (in seconds) to check for a frozen frame. |
| `--threshold` | 1 | How many identical frames to see before triggering a restart. |
| `--cooldown` | 30 | Minimum time (in seconds) to wait between restarts. |

To see all available options, run:
```bash
python obs_display_monitor.py --help
```

## Why Is This Script Necessary?

You might wonder why we can't just check the status of the "Restart Capture" button directly. The short answer is: **OBS does not expose that "is-stuck" flag through its public APIs (like obs-websocket) yet.**

The "Restart Capture" button you see in the OBS user interface is controlled by an internal state (`has_stalled`) that is:
*   Kept entirely inside the macOS Screen Capture source plugin.
*   Used only to enable/disable the button and log a warning.
*   Never broadcast or made available to external tools.

This means there is no direct way for a script to know when the button is clickable. Developers have requested this feature, but as of OBS versions 30/31, it has not been implemented. This script provides a reliable workaround by monitoring the *visual output* of the source instead of relying on an internal state we can't access.

For more technical details, see the discussions here:
*   [GitHub Issue #8928: macOS Screen Capture stops working after waking from sleep](https://github.com/obsproject/obs-studio/issues/8928?utm_source=chatgpt.com)
*   [OBS Forum Thread: Keybind and/or cli argument to restart screen capture](https://obsproject.com/forum/threads/macos-keybind-and-or-cli-argument-to-restart-screen-capture.171415/?utm_source=chatgpt.com)


## Troubleshooting

1. **"Cannot connect to OBS" error**:
   - Make sure OBS is running
   - Check if WebSocket server is enabled in OBS
   - Verify the port number matches
   - Try without password first, then add password if needed

2. **"Source not found" error**:
   - Double-check your display capture source name in OBS
   - The name is case-sensitive
   - Make sure the source is in the current scene

3. **Script not detecting freezes**:
   - Increase logging level for more details
   - Try reducing monitor_interval to 0.5
   - Check if the source is actually visible in OBS

## How It Works

The script uses a smart detection method and a safe restart process to keep your stream running.

### 1. Freeze Detection
The script continuously monitors the display capture source by:
1.  Taking a small, low-quality screenshot of the source every second.
2.  Calculating a unique "fingerprint" (an MD5 hash) of the screenshot.
3.  Comparing this fingerprint to the previous one. If they are the same for a few checks in a row, the script concludes the source is frozen.

This method is highly efficient and reliable.

### 2. Restart Process
Once a freeze is detected, the script performs a safe restart that **preserves all your settings**:
1.  It first saves the current settings of your display capture source (which display it's capturing, whether the cursor is shown, etc.).
2.  It quickly toggles the capture type (e.g., from "Display Capture" to "Window Capture").
3.  It immediately toggles it back to the original type, using the saved settings.

This toggle forces OBS to completely reinitialize the display capture, which unfreezes it, all without changing your source's configuration.

## Requirements

- macOS (tested on Ventura and Sonoma)
- OBS Studio 28.0.0 or later
- Python 3.7 or later
- OBS WebSocket enabled
- Required Python packages (installed via requirements.txt):
  ```
  websocket-client>=1.8.0
  ```

## Support

If you encounter any issues:
1. Check the logs in `logs/obs_monitor.log`
2. Enable debug logging for more details
3. Create an issue on GitHub with your log file
4. Include your macOS and OBS versions

## Contributing

Found a bug or want to improve the script? Feel free to:
1. Open an issue
2. Submit a pull request
3. Share your experience in the OBS forums

## License

MIT License - Feel free to use and modify! 
