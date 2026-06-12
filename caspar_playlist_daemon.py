#!/usr/bin/env python3
"""
Local playlist daemon for CasparCG.

The daemon keeps a playlist after the GUI client disconnects, exposes a small
JSON Lines TCP API for playlist edits, and controls CasparCG over AMCP.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import queue
import re
import socket
import socketserver
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

try:
    from PIL import Image
except ImportError:  # Logo preprocessing is optional.
    Image = None


DEFAULT_STATE_FILE = "playlist_state.json"
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8765
DEFAULT_CASPAR_HOST = "127.0.0.1"
DEFAULT_CASPAR_PORT = 5250
DEFAULT_VIDEO_LAYER = "1-10"
DEFAULT_LOGO_LAYER = "1-20"
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_TRANSITION = "MIX 50"
DEFAULT_LOGO_TRANSITION = "CUT 0"
DEFAULT_FRAME_WIDTH = 1920
DEFAULT_FRAME_HEIGHT = 1080
DEFAULT_LOGO_MARGIN_X = 40
DEFAULT_LOGO_MARGIN_Y = 40


def log(message: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), message, flush=True)


def make_item(clip: str, logo: str = "", item_id: str | None = None) -> dict[str, str]:
    return {
        "id": item_id or str(uuid.uuid4()),
        "clip": clip.strip(),
        "logo": logo.strip(),
    }


def normalize_item(raw: dict[str, Any]) -> dict[str, str]:
    clip = str(raw.get("clip", "")).strip()
    if not clip:
        raise ValueError("Playlist item requires non-empty 'clip'")

    return make_item(
        clip=clip,
        logo=str(raw.get("logo", "")).strip(),
        item_id=str(raw.get("id") or uuid.uuid4()),
    )


def media_name_from_path(path: str, media_dir: str) -> str:
    rel = os.path.relpath(path, media_dir)
    rel_no_ext = os.path.splitext(rel)[0]
    return rel_no_ext.replace("\\", "/")


def find_png_path(media_dir: str, logo_name: str) -> str:
    path = os.path.join(media_dir, logo_name)
    if not path.lower().endswith(".png"):
        path += ".png"

    if not os.path.exists(path):
        raise FileNotFoundError(f"Logo PNG not found: {path}")

    return path


@dataclass
class DaemonConfig:
    api_host: str
    api_port: int
    caspar_host: str
    caspar_port: int
    video_layer: str
    logo_layer: str
    state_file: str
    media_dir: str
    poll_seconds: float
    transition: str
    logo_transition: str
    frame_width: int
    frame_height: int
    logo_margin_x: int
    logo_margin_y: int


class AmcpClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                finally:
                    self._sock = None

    def _connect(self) -> socket.socket:
        if self._sock is None:
            self._sock = socket.create_connection((self.host, self.port), timeout=5)
        return self._sock

    def command(self, command: str, timeout: float = 0.5) -> str:
        with self._lock:
            for attempt in range(2):
                try:
                    sock = self._connect()
                    log(f"AMCP SEND: {command}")
                    sock.sendall((command + "\r\n").encode("utf-8"))
                    sock.settimeout(timeout)
                    chunks: list[bytes] = []

                    try:
                        while True:
                            chunk = sock.recv(8192)
                            if not chunk:
                                break
                            chunks.append(chunk)
                    except socket.timeout:
                        pass

                    response = b"".join(chunks).decode("utf-8", errors="ignore")
                    if response.strip():
                        log(f"AMCP RECV: {response.strip()}")
                    return response
                except OSError:
                    self.close()
                    if attempt == 1:
                        raise
                    time.sleep(0.2)

        return ""


class PlaylistController:
    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self.amcp = AmcpClient(config.caspar_host, config.caspar_port)
        self.lock = threading.RLock()
        self.playlist: list[dict[str, str]] = []
        self.current_index = -1
        self.next_to_load = 0
        self.running = False
        self.stop_event = threading.Event()
        self.worker_events: queue.Queue[str] = queue.Queue()
        self.last_playing = ""
        self.last_logo = ""
        self.loaded_background_index: int | None = None
        self.load_state()

    def load_state(self) -> None:
        if not os.path.exists(self.config.state_file):
            return

        try:
            with open(self.config.state_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.playlist = [normalize_item(item) for item in data.get("playlist", [])]
            self.current_index = int(data.get("current_index", -1))
            self.next_to_load = max(0, int(data.get("next_to_load", 0)))
            self.running = bool(data.get("running", False))
            log(f"Loaded state from {self.config.state_file}")
        except Exception as exc:
            log(f"Could not load state file: {exc}")

    def save_state(self) -> None:
        tmp_path = self.config.state_file + ".tmp"
        data = {
            "playlist": self.playlist,
            "current_index": self.current_index,
            "next_to_load": self.next_to_load,
            "running": self.running,
            "last_playing": self.last_playing,
        }
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self.config.state_file)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            current_item = None
            if 0 <= self.current_index < len(self.playlist):
                current_item = copy.deepcopy(self.playlist[self.current_index])

            return {
                "playlist": copy.deepcopy(self.playlist),
                "current_index": self.current_index,
                "next_to_load": self.next_to_load,
                "running": self.running,
                "last_playing": self.last_playing,
                "last_logo": self.last_logo,
                "loaded_background_index": self.loaded_background_index,
                "current_item": current_item,
            }

    def replace_playlist(self, items: list[dict[str, Any]], start_index: int = 0) -> dict[str, Any]:
        normalized = [normalize_item(item) for item in items]
        with self.lock:
            self.playlist = normalized
            self.current_index = -1
            self.next_to_load = max(0, min(start_index, len(self.playlist)))
            self.last_playing = ""
            self.last_logo = ""
            self.loaded_background_index = None
            self.save_state()
        return self.snapshot()

    def insert_item(self, index: int, item: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_item(item)
        with self.lock:
            index = max(0, min(index, len(self.playlist)))
            self.playlist.insert(index, normalized)
            if self.current_index >= index:
                self.current_index += 1
            if self.next_to_load >= index:
                self.next_to_load += 1
            self.loaded_background_index = None
            self.save_state()
        return self.snapshot()

    def delete_item(self, index: int) -> dict[str, Any]:
        with self.lock:
            self._require_index(index)
            del self.playlist[index]

            if self.current_index == index:
                # Current media cannot be pulled out of Caspar instantly without stopping.
                # Continue playback and let the worker advance from this position.
                self.current_index = min(index, len(self.playlist) - 1)
                self.next_to_load = self.current_index + 1
            elif self.current_index > index:
                self.current_index -= 1

            if self.next_to_load > index:
                self.next_to_load -= 1

            self.loaded_background_index = None
            self.save_state()
        return self.snapshot()

    def update_item(self, index: int, item: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_item(item)
        with self.lock:
            self._require_index(index)
            existing_id = self.playlist[index].get("id")
            if not item.get("id") and existing_id:
                normalized["id"] = existing_id
            self.playlist[index] = normalized
            if index >= self.current_index:
                self.loaded_background_index = None
            self.save_state()
        return self.snapshot()

    def move_item(self, old_index: int, new_index: int) -> dict[str, Any]:
        with self.lock:
            self._require_index(old_index)
            new_index = max(0, min(new_index, len(self.playlist) - 1))
            item = self.playlist.pop(old_index)
            self.playlist.insert(new_index, item)
            self._rebuild_playback_position_after_reorder()
            self.loaded_background_index = None
            self.save_state()
        return self.snapshot()

    def play(self, start_index: int = 0) -> dict[str, Any]:
        with self.lock:
            if not self.playlist:
                raise ValueError("Playlist is empty")
            if start_index < 0 or start_index >= len(self.playlist):
                raise IndexError("start_index out of range")
            self.current_index = start_index
            self.next_to_load = start_index + 1
            self.running = True
            self.last_playing = ""
            self.loaded_background_index = None
            self.save_state()
        self.worker_events.put("start")
        return self.snapshot()

    def stop(self, clear: bool = False) -> dict[str, Any]:
        with self.lock:
            self.running = False
            self.save_state()
        self.worker_events.put("stop")
        if clear:
            self.amcp.command(f"CLEAR {self.config.video_layer}")
            self.amcp.command(f"CLEAR {self.config.logo_layer}")
        return self.snapshot()

    def clear_playlist(self) -> dict[str, Any]:
        with self.lock:
            self.playlist = []
            self.current_index = -1
            self.next_to_load = 0
            self.running = False
            self.loaded_background_index = None
            self.save_state()
        return self.snapshot()

    def _require_index(self, index: int) -> None:
        if index < 0 or index >= len(self.playlist):
            raise IndexError(f"index {index} out of range")

    def _rebuild_playback_position_after_reorder(self) -> None:
        if not self.last_playing:
            self.current_index = min(self.current_index, len(self.playlist) - 1)
            self.next_to_load = max(0, self.current_index + 1)
            return

        for index, item in enumerate(self.playlist):
            if item["clip"].upper() == self.last_playing.upper():
                self.current_index = index
                self.next_to_load = index + 1
                return

        self.current_index = min(self.current_index, len(self.playlist) - 1)
        self.next_to_load = max(0, self.current_index + 1)

    def worker_loop(self) -> None:
        log("Playback worker started")
        while not self.stop_event.is_set():
            try:
                event = self.worker_events.get(timeout=self.config.poll_seconds)
            except queue.Empty:
                event = "poll"

            if event == "stop":
                continue
            if event == "start":
                try:
                    self._start_current_from_state()
                except Exception as exc:
                    log(f"Worker start error: {exc}")
                continue

            try:
                self._tick()
            except Exception as exc:
                log(f"Worker error: {exc}")
                time.sleep(1)

    def _tick(self) -> None:
        with self.lock:
            running = self.running
            current_index = self.current_index
            playlist_len = len(self.playlist)

        if not running or playlist_len == 0:
            return

        if current_index < 0:
            with self.lock:
                self.current_index = 0
                self.next_to_load = 1
                self.save_state()
            self._start_current_from_state()
            return

        with self.lock:
            current_item = copy.deepcopy(self.playlist[current_index])

        info = self.amcp.command(f"INFO {self.config.video_layer}")
        playing = self.detect_playing_clip(info)

        if not playing and not self.last_playing:
            self._start_current_item(current_index, current_item)
            return

        if playing:
            self._handle_playing(playing)

        self._load_next_if_needed()

    def _start_current_from_state(self) -> None:
        with self.lock:
            if not self.running or not self.playlist:
                return
            index = max(0, min(self.current_index, len(self.playlist) - 1))
            item = copy.deepcopy(self.playlist[index])

        self._start_current_item(index, item)

    def _start_current_item(self, index: int, item: dict[str, str]) -> None:
        self.amcp.command(f'PLAY {self.config.video_layer} "{item["clip"]}" {self.config.transition}')
        self._play_logo(item.get("logo", ""))
        with self.lock:
            self.current_index = index
            self.next_to_load = index + 1
            self.last_playing = item["clip"]
            self.save_state()
        self._load_next_if_needed()

    def _handle_playing(self, playing: str) -> None:
        logo_to_play = ""
        should_play_logo = False

        with self.lock:
            if playing != self.last_playing:
                log(f"NOW PLAYING: {playing}")
                self.last_playing = playing

            if (
                0 <= self.current_index < len(self.playlist)
                and self.playlist[self.current_index]["clip"].upper() == playing.upper()
            ):
                self.save_state()
                return

            search_order = list(range(max(0, self.current_index + 1), len(self.playlist)))
            search_order.extend(range(0, max(0, self.current_index + 1)))

            for index in search_order:
                item = self.playlist[index]
                if item["clip"].upper() == playing.upper():
                    if index != self.current_index:
                        self.current_index = index
                        self.next_to_load = index + 1
                        self.loaded_background_index = None
                        logo_to_play = item.get("logo", "")
                        should_play_logo = True
                    self.save_state()
                    break

        if should_play_logo:
            self._play_logo(logo_to_play)

    def _load_next_if_needed(self) -> None:
        with self.lock:
            if not self.running:
                return
            index = self.next_to_load
            if index >= len(self.playlist):
                return
            if self.loaded_background_index == index:
                return
            item = copy.deepcopy(self.playlist[index])

        self.amcp.command(
            f'LOADBG {self.config.video_layer} "{item["clip"]}" {self.config.transition} AUTO'
        )

        with self.lock:
            self.loaded_background_index = index
            self.save_state()
        log(f"BACKGROUND LOADED index={index} clip={item['clip']}")

    def detect_playing_clip(self, info_response: str) -> str:
        foreground = self._get_foreground_part(info_response).upper()
        with self.lock:
            items = copy.deepcopy(self.playlist)

        with self.lock:
            current_index = self.current_index

        search_order = list(range(max(0, current_index), len(items)))
        search_order.extend(range(0, max(0, current_index)))

        for index in search_order:
            item = items[index]
            if item["clip"].upper() in foreground:
                return item["clip"]
        return ""

    @staticmethod
    def _get_foreground_part(info_response: str) -> str:
        match = re.search(
            r"<foreground>(.*?)</foreground>",
            info_response,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            return match.group(1)
        return info_response

    def _play_logo(self, logo_name: str) -> None:
        if not logo_name:
            self.amcp.command(f"CLEAR {self.config.logo_layer}")
            self.last_logo = ""
            return

        prepared_logo = self._prepare_logo_file(logo_name)
        self.amcp.command(f"CLEAR {self.config.logo_layer}")
        self.amcp.command(f"MIXER {self.config.logo_layer} CLEAR")
        self.amcp.command(f"MIXER {self.config.logo_layer} FILL 0 0 1 1")
        self.amcp.command(f"MIXER {self.config.logo_layer} OPACITY 1")
        self.amcp.command(
            f'PLAY {self.config.logo_layer} "{prepared_logo}" {self.config.logo_transition}'
        )
        self.last_logo = logo_name

    def _prepare_logo_file(self, logo_name: str) -> str:
        if Image is None:
            log("Pillow is not installed; playing logo without preprocessing")
            return logo_name

        source_path = find_png_path(self.config.media_dir, logo_name)
        cache_dir = os.path.join(self.config.media_dir, "_logo_cache")
        os.makedirs(cache_dir, exist_ok=True)

        img = Image.open(source_path).convert("RGBA")
        width, height = img.size
        base_name = os.path.splitext(os.path.basename(source_path))[0]
        cache_path = os.path.join(
            cache_dir,
            f"{base_name}_caspar_{self.config.frame_width}x{self.config.frame_height}.png",
        )

        source_mtime = os.path.getmtime(source_path)
        if os.path.exists(cache_path) and os.path.getmtime(cache_path) >= source_mtime:
            return media_name_from_path(cache_path, self.config.media_dir)

        canvas = Image.new(
            "RGBA",
            (self.config.frame_width, self.config.frame_height),
            (0, 0, 0, 0),
        )

        if width == self.config.frame_width and height == self.config.frame_height:
            canvas.alpha_composite(img, (0, 0))
        else:
            x = max(0, self.config.frame_width - width - self.config.logo_margin_x)
            y = max(0, self.config.logo_margin_y)
            canvas.alpha_composite(img, (x, y))

        canvas.save(cache_path, "PNG")
        return media_name_from_path(cache_path, self.config.media_dir)


class ApiHandler(socketserver.StreamRequestHandler):
    controller: PlaylistController

    def handle(self) -> None:
        peer = self.client_address[0]
        log(f"API client connected: {peer}")
        for raw_line in self.rfile:
            try:
                request = json.loads(raw_line.decode("utf-8"))
                response = self.server.dispatch(request)  # type: ignore[attr-defined]
            except Exception as exc:
                response = {
                    "ok": False,
                    "error": str(exc),
                }

            self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()


class PlaylistApiServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], controller: PlaylistController) -> None:
        super().__init__(server_address, ApiHandler)
        self.controller = controller

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        payload = request.get("payload") or {}
        request_id = request.get("id")

        if action == "ping":
            result = {"message": "pong"}
        elif action == "get_state":
            result = self.controller.snapshot()
        elif action == "replace_playlist":
            result = self.controller.replace_playlist(
                payload.get("items") or [],
                int(payload.get("start_index", 0)),
            )
        elif action == "insert_item":
            result = self.controller.insert_item(int(payload["index"]), payload["item"])
        elif action == "delete_item":
            result = self.controller.delete_item(int(payload["index"]))
        elif action == "update_item":
            result = self.controller.update_item(int(payload["index"]), payload["item"])
        elif action == "move_item":
            result = self.controller.move_item(int(payload["old_index"]), int(payload["new_index"]))
        elif action == "clear_playlist":
            result = self.controller.clear_playlist()
        elif action == "play":
            result = self.controller.play(int(payload.get("start_index", 0)))
        elif action == "stop":
            result = self.controller.stop(bool(payload.get("clear", False)))
        else:
            raise ValueError(f"Unknown action: {action}")

        return {
            "ok": True,
            "id": request_id,
            "result": result,
        }


def parse_args() -> DaemonConfig:
    parser = argparse.ArgumentParser(description="CasparCG playlist daemon")
    parser.add_argument("--api-host", default=DEFAULT_API_HOST)
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--caspar-host", default=DEFAULT_CASPAR_HOST)
    parser.add_argument("--caspar-port", type=int, default=DEFAULT_CASPAR_PORT)
    parser.add_argument("--video-layer", default=DEFAULT_VIDEO_LAYER)
    parser.add_argument("--logo-layer", default=DEFAULT_LOGO_LAYER)
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--media-dir", default=r"c:\Projekty\mxf")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--transition", default=DEFAULT_TRANSITION)
    parser.add_argument("--logo-transition", default=DEFAULT_LOGO_TRANSITION)
    parser.add_argument("--frame-width", type=int, default=DEFAULT_FRAME_WIDTH)
    parser.add_argument("--frame-height", type=int, default=DEFAULT_FRAME_HEIGHT)
    parser.add_argument("--logo-margin-x", type=int, default=DEFAULT_LOGO_MARGIN_X)
    parser.add_argument("--logo-margin-y", type=int, default=DEFAULT_LOGO_MARGIN_Y)
    args = parser.parse_args()

    return DaemonConfig(
        api_host=args.api_host,
        api_port=args.api_port,
        caspar_host=args.caspar_host,
        caspar_port=args.caspar_port,
        video_layer=args.video_layer,
        logo_layer=args.logo_layer,
        state_file=args.state_file,
        media_dir=args.media_dir,
        poll_seconds=args.poll_seconds,
        transition=args.transition,
        logo_transition=args.logo_transition,
        frame_width=args.frame_width,
        frame_height=args.frame_height,
        logo_margin_x=args.logo_margin_x,
        logo_margin_y=args.logo_margin_y,
    )


def main() -> None:
    config = parse_args()
    controller = PlaylistController(config)

    worker = threading.Thread(target=controller.worker_loop, daemon=True)
    worker.start()

    server = PlaylistApiServer((config.api_host, config.api_port), controller)
    log(f"API listening on {config.api_host}:{config.api_port}")
    log(f"CasparCG AMCP target {config.caspar_host}:{config.caspar_port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Stopping daemon")
    finally:
        controller.stop_event.set()
        controller.amcp.close()
        server.server_close()


if __name__ == "__main__":
    main()
