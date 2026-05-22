#!/usr/bin/env python3
"""
OBS Display Capture Monitor

Monitors Display/Screen Capture sources in OBS Studio and automatically
restarts them when they become frozen (stuck on an identical frame).

By default it auto-detects every capture source and re-checks the list
periodically, so newly added sources are covered without reconfiguring.
It also reconnects to OBS on its own when the WebSocket connection drops
(OBS restart, machine sleep, network blip) instead of looping blind.

Uses the OBS WebSocket API. Designed to run in the background on macOS.
"""

import asyncio
import json
import logging
import logging.handlers
import signal
import sys
import base64
import hashlib
import time
import argparse
from datetime import datetime
from pathlib import Path

try:
    import websocket
except ImportError:
    print("Error: websocket-client not installed. Run: pip install websocket-client")
    sys.exit(1)


# inputKind substrings that identify a screen/window/display capture (macOS uses
# 'screen_capture' for the ScreenCaptureKit source).
DEFAULT_CAPTURE_KINDS = [
    "screen_capture",
    "display_capture",
    "window_capture",
    "monitor_capture",
]

# How often (seconds) to re-scan OBS for new/removed sources in auto mode.
REDISCOVER_INTERVAL = 60

# Cap on the back-off between reconnect attempts (seconds).
MAX_RECONNECT_DELAY = 30

# Socket timeout so a hung OBS doesn't block recv() forever.
WS_TIMEOUT = 10


class OBSDisplayMonitor:
    def __init__(self, source_names=None, all_captures=True, capture_kinds=None,
                 host="localhost", port=4455, password="",
                 interval=1.0, threshold=5, cooldown=30):
        # Source selection: explicit names take precedence over auto mode.
        self.explicit_sources = list(source_names) if source_names else []
        self.all_captures = all_captures and not self.explicit_sources
        self.capture_kinds = capture_kinds or DEFAULT_CAPTURE_KINDS
        self.sources = list(self.explicit_sources)  # current set being watched

        self.host = host
        self.port = port
        self.password = password
        self.ws = None
        self.running = True
        self.monitor_interval = interval
        self.stuck_threshold = threshold
        self.restart_cooldown = cooldown
        self.request_id = 1

        # Per-source state, keyed by source name.
        self.prev_hash = {}
        self.identical_count = {}
        self.last_restart = {}
        # Whether we've ever seen this source's frame change. We only flag a
        # source as "frozen" after it has shown real activity — a capture that
        # has been identical since we started watching is static content (or was
        # already dead), not a freeze we can fix by restarting.
        self.has_changed = {}

        # Connection bookkeeping.
        self._disconnect_logged = False
        self._last_discover = 0.0

        self.setup_logging()

    def setup_logging(self):
        """Setup logging with rotation so logs can never fill the disk."""
        log_dir = Path.cwd() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        log_file = log_dir / "obs_monitor.log"

        # Rotate at ~5 MB, keep 3 backups -> ~20 MB ceiling total.
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5_000_000, backupCount=3
        )

        handlers = [file_handler]
        # Only echo to the console for interactive runs. Under launchd stdout is
        # redirected to a file, so skipping the stream handler there stops that
        # file from growing as an unbounded duplicate of the rotated log.
        if sys.stdout.isatty():
            handlers.append(logging.StreamHandler(sys.stdout))

        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=handlers,
        )
        self.logger = logging.getLogger(__name__)

    def _build_auth_string(self, salt, challenge):
        """Build authentication string for OBS WebSocket"""
        secret = base64.b64encode(
            hashlib.sha256(
                (self.password + salt).encode('utf-8')
            ).digest()
        )
        auth = base64.b64encode(
            hashlib.sha256(
                secret + challenge.encode('utf-8')
            ).digest()
        ).decode('utf-8')
        return auth

    def _authenticate(self):
        """Authenticate with OBS WebSocket"""
        try:
            # Receive hello message
            message = self.ws.recv()
            result = json.loads(message)

            if result.get('op') != 0:  # Hello OpCode
                raise Exception("Expected Hello message")

            auth_data = result['d'].get('authentication')
            # We only do request/response, so subscribe to no events. This avoids
            # event frames showing up where we expect a response and desyncing.
            identify = {"rpcVersion": 1, "eventSubscriptions": 0}
            if auth_data and self.password:
                identify["authentication"] = self._build_auth_string(
                    auth_data['salt'], auth_data['challenge']
                )

            self.ws.send(json.dumps({"op": 1, "d": identify}))  # Identify OpCode

            # Receive Identified message
            message = self.ws.recv()
            result = json.loads(message)

            if result.get('op') != 2:  # Identified OpCode
                raise Exception(f"Authentication failed: {result}")

            return True

        except Exception as e:
            self.logger.error(f"Authentication failed: {e}")
            return False

    def connect_to_obs(self):
        """Establish connection to OBS WebSocket"""
        try:
            self.logger.debug(f"Connecting to OBS WebSocket at {self.host}:{self.port}")
            url = f"ws://{self.host}:{self.port}"
            self.ws = websocket.create_connection(url, timeout=WS_TIMEOUT)

            if not self._authenticate():
                self._drop_connection()
                return False

            version_response = self._send_request("GetVersion")
            if version_response:
                obs_version = version_response.get('responseData', {}).get('obsVersion', 'Unknown')
                self.logger.info(f"Connected to OBS Studio {obs_version}")
                return True

            self._drop_connection()
            return False

        except Exception as e:
            # Quiet by design: OBS may simply not be up yet. The loop retries.
            self.logger.debug(f"Failed to connect to OBS: {e}")
            self._drop_connection()
            return False

    def _drop_connection(self):
        """Tear down the socket so the loop knows to reconnect."""
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None

    def disconnect_from_obs(self):
        """Disconnect from OBS WebSocket"""
        if self.ws:
            try:
                self.ws.close()
                self.logger.info("Disconnected from OBS WebSocket")
            except Exception as e:
                self.logger.error(f"Error disconnecting from OBS: {e}")
            finally:
                self.ws = None

    def _send_request(self, request_type, request_data=None):
        """Send a request to OBS and return the response dict (or None)."""
        if not self.ws:
            return None

        try:
            request_id = f"req_{self.request_id}"
            self.request_id += 1

            payload = {
                "op": 6,  # Request OpCode
                "d": {
                    "requestId": request_id,
                    "requestType": request_type,
                    "requestData": request_data or {}
                }
            }

            self.ws.send(json.dumps(payload))

            message = self.ws.recv()
            response = json.loads(message)

            if response.get('op') == 7 and response.get('d', {}).get('requestId') == request_id:
                return response.get('d')

            return None

        except (websocket.WebSocketConnectionClosedException,
                websocket.WebSocketTimeoutException,
                ConnectionError, BrokenPipeError, OSError) as e:
            # Connection died. Drop it so the loop reconnects. Logged once per
            # disconnect by ensure_connected() -- never spammed every second.
            self.logger.debug(f"Connection lost during {request_type}: {e}")
            self._drop_connection()
            return None
        except Exception as e:
            self.logger.error(f"Error sending request {request_type}: {e}")
            return None

    def ensure_connected(self):
        """Make sure we have a live OBS connection; reconnect if not.

        Returns True if connected. Logs the disconnect/reconnect transition
        exactly once each so an OBS outage doesn't flood the log.
        """
        if self.ws is not None:
            return True

        if not self._disconnect_logged:
            self.logger.warning("⚠️ Conexión con OBS perdida — reintentando…")
            self._disconnect_logged = True

        if self.connect_to_obs():
            self.logger.info("✅ Reconectado a OBS")
            self._disconnect_logged = False
            self.discover_sources(force=True)
            return True

        return False

    def discover_sources(self, force=False):
        """Refresh the set of sources to watch.

        In auto mode, query OBS and keep every input whose inputKind looks like a
        capture. In explicit mode, keep the configured names (warning on any that
        aren't present yet). Throttled to REDISCOVER_INTERVAL unless force=True.
        """
        now = time.time()
        if not force and (now - self._last_discover) < REDISCOVER_INTERVAL:
            return
        self._last_discover = now

        response = self._send_request("GetInputList")
        if not response or 'responseData' not in response:
            return  # connection probably down; keep current set as-is
        inputs = response['responseData'].get('inputs', [])

        if self.all_captures:
            new_set = []
            for inp in inputs:
                name = inp.get('inputName', '')
                kind = inp.get('inputKind', '') or inp.get('unversionedInputKind', '')
                if any(k in kind for k in self.capture_kinds):
                    new_set.append(name)
        else:
            available = {inp.get('inputName', '') for inp in inputs}
            new_set = list(self.explicit_sources)
            missing = [s for s in self.explicit_sources if s not in available]
            if missing:
                self.logger.warning(f"Sources configuradas no encontradas (aún): {missing}")

        if set(new_set) != set(self.sources):
            added = sorted(set(new_set) - set(self.sources))
            removed = sorted(set(self.sources) - set(new_set))
            if added:
                self.logger.info(f"🟢 Vigilando sources: {added}")
            if removed:
                self.logger.info(f"⚪ Sources ya no presentes: {removed}")
            self.sources = new_set
            # Drop state for sources that went away.
            for s in removed:
                self.prev_hash.pop(s, None)
                self.identical_count.pop(s, None)
                self.last_restart.pop(s, None)
                self.has_changed.pop(s, None)

        if not self.sources:
            self.logger.warning("No hay capturas para vigilar todavía.")

    def _get_screenshot_hash(self, source_name):
        """Get MD5 hash of the current screenshot of a source (or None)."""
        response = self._send_request("GetSourceScreenshot", {
            "sourceName": source_name,
            "imageFormat": "png",
            "imageWidth": 320,   # Small size for quick comparison
            "imageHeight": 180,
            "imageCompressionQuality": 50
        })

        if response and 'responseData' in response:
            try:
                # Extract base64 data from the data URL and hash the bytes.
                image_data = response['responseData']['imageData'].split(',', 1)[1]
                return hashlib.md5(base64.b64decode(image_data)).hexdigest()
            except Exception as e:
                self.logger.debug(f"Could not parse screenshot for '{source_name}': {e}")
        return None

    def _restart_capture(self, source_name):
        """Restart a capture source by re-applying its input settings."""
        try:
            now = time.time()
            if now - self.last_restart.get(source_name, 0) < self.restart_cooldown:
                self.logger.info(f"⏳ Restart de '{source_name}' en cooldown — salteando")
                return False

            self.logger.warning(f"{'='*50}")
            self.logger.warning("🔄 RESTARTING DISPLAY CAPTURE")
            self.logger.warning(f"Source: {source_name}")
            self.logger.warning(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            settings_response = self._send_request("GetInputSettings", {
                "inputName": source_name
            })
            if not settings_response or 'responseData' not in settings_response:
                self.logger.error(f"No pude leer settings de '{source_name}'")
                return False

            current_settings = settings_response['responseData'].get('inputSettings', {})
            self.logger.debug(f"Current settings: {json.dumps(current_settings)}")

            # Nudge a toggle-able capture-type field if present, then restore.
            # Re-applying the original settings re-initialises the source, which
            # is what clears a stuck capture.
            if 'type' in current_settings:
                original_type = current_settings.get('type', 0)
                nudged = {**current_settings, "type": 1 if original_type == 0 else 0}
                self._send_request("SetInputSettings", {
                    "inputName": source_name, "inputSettings": nudged
                })
                time.sleep(0.5)

            response = self._send_request("SetInputSettings", {
                "inputName": source_name, "inputSettings": current_settings
            })

            if response and response.get('requestStatus', {}).get('result'):
                self.last_restart[source_name] = now
                self.logger.warning("✅ Restart completed")
                self.logger.warning(f"{'='*50}")
                return True

            self.logger.error(f"❌ Restart de '{source_name}' falló")
            return False

        except Exception as e:
            self.logger.error(f"❌ Failed to restart '{source_name}': {e}")
            return False

    async def monitor_loop(self):
        """Main monitoring loop: watch every discovered source for freezes."""
        mode = "auto (todas las capturas)" if self.all_captures \
            else f"sources: {', '.join(self.explicit_sources)}"
        self.logger.info(f"Starting monitor loop — modo: {mode}")
        self.logger.info(
            f"Interval: {self.monitor_interval}s, threshold: {self.stuck_threshold} frames, "
            f"cooldown: {self.restart_cooldown}s"
        )

        check_count = 0
        reconnect_delay = 1

        while self.running:
            try:
                # Self-healing: (re)connect if needed before doing any work.
                if not await asyncio.to_thread(self.ensure_connected):
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(MAX_RECONNECT_DELAY, reconnect_delay * 2)
                    continue
                reconnect_delay = 1

                # Pick up newly added / removed sources periodically.
                await asyncio.to_thread(self.discover_sources)

                check_count += 1
                self.logger.debug(f"=== Check #{check_count} ({len(self.sources)} sources) ===")

                for source_name in list(self.sources):
                    current_hash = await asyncio.to_thread(
                        self._get_screenshot_hash, source_name
                    )
                    if current_hash is None:
                        # Couldn't read (connection dropped or source gone). Skip;
                        # don't treat as frozen, don't overwrite last good hash.
                        continue

                    prev = self.prev_hash.get(source_name)
                    self.prev_hash[source_name] = current_hash

                    if prev is None:
                        # First sample for this source — just establish a baseline.
                        continue

                    if current_hash != prev:
                        # Frame changed: the source is alive. Remember it so we only
                        # ever flag it as frozen after seeing real activity.
                        self.has_changed[source_name] = True
                        if self.identical_count.get(source_name, 0) > 0:
                            self.logger.info(f"✅ '{source_name}' volvió a cambiar — activa")
                        self.identical_count[source_name] = 0
                        continue

                    # Frame identical to the previous one.
                    if not self.has_changed.get(source_name, False):
                        # Never changed since we started watching → static content
                        # or a source that was already dead. Restarting it would
                        # just loop forever on something this can't fix, so skip.
                        continue

                    count = self.identical_count.get(source_name, 0) + 1
                    self.identical_count[source_name] = count
                    self.logger.warning(
                        f"⚠️ '{source_name}' frame idéntico (count: {count}/{self.stuck_threshold})"
                    )
                    if count >= self.stuck_threshold:
                        self.logger.warning(f"❗ '{source_name}' parece congelada")
                        if await asyncio.to_thread(self._restart_capture, source_name):
                            self.identical_count[source_name] = 0
                            # After a restart the frame should change again; refresh.
                            self.prev_hash[source_name] = await asyncio.to_thread(
                                self._get_screenshot_hash, source_name
                            )

                await asyncio.sleep(self.monitor_interval)

            except Exception as e:
                self.logger.error(f"Error en monitor loop: {e}")
                await asyncio.sleep(2)

    def signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.stop()

    def start(self):
        """Start the monitoring service"""
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

        self.logger.info("Starting OBS Display Capture Monitor")

        # Try once up front, but don't exit if OBS isn't up yet — the loop
        # reconnects on its own.
        if self.connect_to_obs():
            self.discover_sources(force=True)
        else:
            self.logger.warning("OBS no disponible al arrancar — el loop reintentará conectarse.")

        self.running = True

        try:
            asyncio.run(self.monitor_loop())
        except KeyboardInterrupt:
            self.logger.info("Keyboard interrupt received")
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
        finally:
            self.stop()

        return True

    def stop(self):
        """Stop the monitoring service"""
        self.logger.info("Stopping OBS Display Capture Monitor")
        self.running = False
        self.disconnect_from_obs()


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="OBS Display Capture Monitor")
    parser.add_argument(
        '--source',
        action='append',
        default=None,
        help="Nombre exacto de una source a vigilar. Repetible. Si se usa, desactiva la auto-detección."
    )
    parser.add_argument(
        '--all-captures',
        action='store_true',
        help="Auto-detectar y vigilar todas las capturas (default si no se pasa --source)."
    )
    parser.add_argument(
        '--input-kinds',
        type=str,
        default=",".join(DEFAULT_CAPTURE_KINDS),
        help="Substrings de inputKind a tratar como captura (separados por coma)."
    )
    parser.add_argument('--host', type=str, default="localhost", help="OBS WebSocket host")
    parser.add_argument('--port', type=int, default=4455, help="OBS WebSocket port")
    parser.add_argument('--password', type=str, default="", help="OBS WebSocket password")
    parser.add_argument('--interval', type=float, default=1.0, help="Cada cuánto chequear (segundos)")
    parser.add_argument('--threshold', type=int, default=5, help="Cuántos frames idénticos seguidos = congelado")
    parser.add_argument('--cooldown', type=int, default=30, help="Tiempo mínimo entre restarts de una source (s)")

    args = parser.parse_args()

    capture_kinds = [k.strip() for k in args.input_kinds.split(",") if k.strip()]
    all_captures = args.all_captures or not args.source

    print("OBS Display Capture Monitor")
    if args.source:
        print(f"Monitoring sources: {', '.join(args.source)}")
    else:
        print("Monitoring: auto-detect all captures")
    print(f"OBS WebSocket: {args.host}:{args.port}")
    print("Press Ctrl+C to stop")
    print("-" * 50)

    monitor = OBSDisplayMonitor(
        source_names=args.source,
        all_captures=all_captures,
        capture_kinds=capture_kinds,
        host=args.host,
        port=args.port,
        password=args.password,
        interval=args.interval,
        threshold=args.threshold,
        cooldown=args.cooldown
    )

    try:
        monitor.start()
    except Exception as e:
        print(f"Failed to start monitor: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
