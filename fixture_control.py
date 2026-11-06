#!/usr/bin/env python3
"""Art-Net fixture controller with a web page and basic cues.

Run:      python3 fixture_control.py
Control:  http://<pi-address>:8080

Needs Python 3 only. Cues and Art-Net settings are saved to show.json, next
to this script. Fixtures go dark when the program starts and when it stops,
unless --autostart is used to resume the effect or run the first cue.
"""

import argparse
import ipaddress
import json
import math
import os
import select
import signal
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

FPS = 40
KEEPALIVE_SECONDS = 1.0
FIXTURE_COUNT = 6
MAX_FADE = 100
MAX_NEXT = 3600
MIN_CUE_STEP = 1.0  # Auto-next cues never change faster than this, to prevent strobing.
MAX_BODY = 64 * 1024
LAYOUTS = ("RGB", "GRB", "BGR", "DRGB", "RGBD", "RGBW", "DRGBW")
DEFAULT_IP = "2.255.255.255"
EFFECT_TYPES = ("wave", "chase")
EFFECT_DIRECTIONS = ("forward", "reverse")
MIN_EFFECT_TIME = 1  # Longer than a strobe, even at the shortest setting.
MAX_EFFECT_TIME = 100
EFFECT_BLEND_SECONDS = 1.0
DEFAULT_EFFECT = {
    "type": "wave",
    "colour1": [0, 0, 255],
    "colour2": [0, 255, 0],
    "time": 10,
    "direction": "forward",
    "running": False,
}


def number(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{label} must be a number from {low:g} to {high:g}.")
    return value


def whole(value, low, high, label):
    if number(value, low, high, label) != int(value):
        raise ValueError(f"{label} must be a whole number from {low} to {high}.")
    return int(value)


def text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 40:
        raise ValueError(f"{label} must be 1 to 40 characters.")
    return value.strip()


def ipv4(value):
    try:
        return str(ipaddress.IPv4Address(value))
    except (ipaddress.AddressValueError, ValueError):
        raise ValueError("Art-Net address must be an IPv4 address, such as 192.168.1.255.")


def rgb(value):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("A colour needs red, green and blue values.")
    return [whole(v, 0, 255, "Colour value") for v in value]


def next_time(value):
    return None if value is None else number(value, 0, MAX_NEXT, "Auto-next time")


def clean_cue(cue, fixture_count):
    levels = cue.get("levels")
    if not isinstance(levels, list) or len(levels) != fixture_count:
        raise ValueError("A cue must have one colour for each fixture.")
    return {
        "id": whole(cue.get("id"), 1, 10**9, "Cue id"),
        "name": text(cue.get("name"), "Cue name"),
        "fade": number(cue.get("fade"), 0, MAX_FADE, "Fade time"),
        "next": next_time(cue.get("next")),
        "levels": [rgb(level) for level in levels],
    }


def clean_effect(effect):
    if not isinstance(effect, dict):
        raise ValueError("The effect settings are not valid.")
    if effect.get("type") not in EFFECT_TYPES:
        raise ValueError("Choose Wave or Chase.")
    if effect.get("direction") not in EFFECT_DIRECTIONS:
        raise ValueError("Choose a direction from the list.")
    return {
        "type": effect["type"],
        "colour1": rgb(effect.get("colour1")),
        "colour2": rgb(effect.get("colour2")),
        "time": number(effect.get("time"), MIN_EFFECT_TIME, MAX_EFFECT_TIME, "Effect time"),
        "direction": effect["direction"],
        "running": bool(effect.get("running", False)),
    }


def effect_position(kind, phase, index, count):
    """Returns 0 for colour 1 through to 1 for colour 2 at this point in the cycle."""
    if kind == "wave":
        return (1 - math.cos(2 * math.pi * (phase - index / count))) / 2
    step = int(phase * 2 * count) % (2 * count)
    eased = (1 - math.cos(math.pi * (phase * 2 * count - step))) / 2
    if step < count:
        return 1.0 if index < step else eased if index == step else 0.0
    step -= count
    return 0.0 if index < step else 1 - eased if index == step else 1.0


def clean_patch(ip, universe, layout, fixtures):
    if layout not in LAYOUTS:
        raise ValueError("Choose a channel layout from the list.")
    if not isinstance(fixtures, list) or len(fixtures) != FIXTURE_COUNT:
        raise ValueError(f"The patch needs {FIXTURE_COUNT} fixtures.")
    highest = 513 - len(layout)
    clean = []
    for position, fixture in enumerate(fixtures, 1):
        clean.append({
            "name": text(fixture.get("name"), f"Fixture {position} name"),
            "address": whole(fixture.get("address"), 1, highest, f"Fixture {position} address"),
        })
    return ipv4(ip), whole(universe, 0, 32767, "Universe"), layout, clean


def default_show():
    blue = [[0, 0, 255] for _ in range(FIXTURE_COUNT)]
    green = [[0, 255, 0] for _ in range(FIXTURE_COUNT)]
    return {
        "ip": DEFAULT_IP,
        "universe": 0,
        "layout": "RGB",
        "loop": True,
        "effect": dict(DEFAULT_EFFECT),
        "fixtures": [
            {"name": f"Fixture {i + 1}", "address": 1 + i * 3} for i in range(FIXTURE_COUNT)
        ],
        "cues": [
            {"id": 1, "name": "Blue", "fade": 10, "next": 0, "levels": blue},
            {"id": 2, "name": "Green", "fade": 10, "next": 0, "levels": green},
        ],
    }


class ArtNet:
    def __init__(self, port):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sequence = 0
        try:
            self.sock.bind(("", port))
            self.listening = True
        except OSError as error:
            self.listening = False
            print(f"Art-Net discovery is off, as port {port} is in use: {error.strerror or error}", flush=True)

    @staticmethod
    def _local_ip(peer):
        """Finds this Pi's address on the network that the peer is on."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((peer, 1))
            return probe.getsockname()[0]
        finally:
            probe.close()

    def poll_reply(self, peer, universe):
        """Builds an ArtPollReply, which makes this controller appear in Art-Net tools."""
        ip = socket.inet_aton(self._local_ip(peer))
        mac = uuid.getnode()
        mac = bytes(6) if mac >> 40 & 1 else mac.to_bytes(6, "big")
        packet = bytearray(239)
        packet[0:8] = b"Art-Net\x00"
        packet[8:10] = (0x2100).to_bytes(2, "little")
        packet[10:14] = ip
        packet[14:16] = (0x1936).to_bytes(2, "little")
        packet[16:18] = bytes([1, 0])  # Version of this program.
        packet[18] = universe >> 8 & 0x7F
        packet[19] = universe >> 4 & 0x0F
        packet[20:22] = (0x00FF).to_bytes(2, "big")  # Art-Net's "unknown" manufacturer code.
        packet[23] = 0x20  # Universe is set in software.
        packet[26:26 + len(b"Fixture Control")] = b"Fixture Control"
        packet[44:44 + len(b"Fixture Control Art-Net controller")] = b"Fixture Control Art-Net controller"
        report = b"#0001 [0001] Fixture Control running"
        packet[108:108 + len(report)] = report
        packet[173] = 1  # One port.
        packet[174] = 0x40  # The port sends DMX onto the Art-Net network.
        packet[178] = 0x80  # The port has data.
        packet[186] = universe & 0x0F
        packet[200] = 0x01  # Identifies this device as a controller.
        packet[201:207] = mac
        packet[207:211] = ip
        packet[211] = 1
        packet[212] = 0x08  # Supports 15-bit universes.
        return bytes(packet)

    def listen(self, get_universe, stop):
        """Answers ArtPoll requests until told to stop."""
        while self.listening and not stop.is_set():
            try:
                ready, _, _ = select.select([self.sock], [], [], 0.5)
                if not ready:
                    continue
                data, sender = self.sock.recvfrom(2048)
                if data[:8] == b"Art-Net\x00" and data[8:10] == (0x2000).to_bytes(2, "little"):
                    self.sock.sendto(self.poll_reply(sender[0], get_universe()), (sender[0], self.port))
            except OSError:
                stop.wait(0.1)

    def send(self, ip, universe, data):
        self.sequence = self.sequence % 255 + 1
        packet = (
            b"Art-Net\x00"
            + (0x5000).to_bytes(2, "little")
            + (14).to_bytes(2, "big")
            + bytes([self.sequence, 0])
            + universe.to_bytes(2, "little")
            + len(data).to_bytes(2, "big")
            + bytes(data)
        )
        self.sock.sendto(packet, (ip, self.port))


class Controller:
    def __init__(self, path, artnet):
        self.path = path
        self.artnet = artnet
        self.lock = threading.RLock()
        self.rev = 1
        self.show = self._load()
        self.levels = [[0.0, 0.0, 0.0] for _ in self.show["fixtures"]]
        self.master = 100
        self.blackout = False
        self.fade = None
        self.next_at = None
        self.active_id = None
        self.state_label = "ready"
        self.error = None
        self.last_data = None
        self.last_sent = 0.0
        self.effect_active = False
        self.effect_phase = 0.0
        self.effect_started = 0.0
        self.effect_updated = 0.0
        self.effect_from = None

    # Saved show

    def _load(self):
        if not os.path.exists(self.path):
            show = default_show()
            self._write(show)
            return show
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
            ip, universe, layout, fixtures = clean_patch(
                raw.get("ip"), raw.get("universe"), raw.get("layout"), raw.get("fixtures")
            )
            cues = [clean_cue(cue, len(fixtures)) for cue in raw.get("cues", [])]
            if len({cue["id"] for cue in cues}) != len(cues):
                raise ValueError("Two cues have the same id.")
            effect = clean_effect(raw.get("effect", DEFAULT_EFFECT))
        except (OSError, ValueError, AttributeError, TypeError) as error:
            sys.exit(f"Could not read {self.path}: {error}\nFix or move the file, then start again.")
        return {
            "ip": ip,
            "universe": universe,
            "layout": layout,
            "loop": bool(raw.get("loop", False)),
            "fixtures": fixtures,
            "cues": cues,
            "effect": effect,
        }

    def _write(self, show):
        temporary = f"{self.path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(show, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def _changed(self):
        self.rev += 1
        self._write(self.show)

    # Cue lookup

    def _index(self, cue_id):
        for index, cue in enumerate(self.show["cues"]):
            if cue["id"] == cue_id:
                return index
        return None

    def _cue(self, cue_id):
        index = self._index(cue_id)
        if index is None:
            raise ValueError("That cue no longer exists.")
        return self.show["cues"][index]

    def _following(self, index):
        cues = self.show["cues"]
        if index + 1 < len(cues):
            return cues[index + 1]
        return cues[0] if self.show["loop"] and cues else None

    # Playback

    def _hold_time(self, cue):
        """Seconds to hold after a fade. None means wait for GO. Loop keeps the stack running."""
        if cue["next"] is None and self.show["loop"]:
            return 0
        return cue["next"]

    def _start(self, cue, now):
        self._stop_effect()
        target = [[float(v) for v in level] for level in cue["levels"]]
        duration = cue["fade"]
        if duration > 0:
            start = [list(level) for level in self.levels]
            self.fade = {"start": start, "end": target, "began": now, "duration": duration}
        else:
            self.fade = None
            self.levels = target
        self.active_id = cue["id"]
        self.state_label = "playing"
        following = self._following(self._index(cue["id"]))
        hold = self._hold_time(cue)
        if hold is None or following is None:
            self.next_at = None
        else:
            self.next_at = now + max(duration + hold, MIN_CUE_STEP)

    def go(self):
        with self.lock:
            cues = self.show["cues"]
            if not cues:
                raise ValueError("Record a cue before pressing GO.")
            index = self._index(self.active_id)
            cue = cues[0] if index is None else self._following(index)
            if cue is None:
                raise ValueError("This is the last cue. Turn on Loop or choose a cue.")
            self._start(cue, time.monotonic())

    def back(self):
        with self.lock:
            index = self._index(self.active_id)
            if index is None or not self.show["cues"]:
                raise ValueError("No cue is running.")
            self._start(self.show["cues"][max(index - 1, 0)], time.monotonic())

    def go_to(self, cue_id):
        with self.lock:
            self._start(self._cue(cue_id), time.monotonic())

    def hold(self):
        with self.lock:
            self._stop_effect()
            self.fade = None
            self.next_at = None
            self.state_label = "held"

    def live(self, fixtures, colour):
        colour = rgb(colour)
        if not isinstance(fixtures, list) or not fixtures:
            raise ValueError("Select at least one fixture.")
        indexes = [whole(i, 0, len(self.levels) - 1, "Fixture") for i in fixtures]
        with self.lock:
            self._stop_effect()
            self.fade = None
            self.next_at = None
            self.state_label = "manual"
            for index in indexes:
                self.levels[index] = [float(v) for v in colour]

    def set_master(self, value):
        value = whole(value, 0, 100, "Master")
        with self.lock:
            self.master = value

    def set_blackout(self, on):
        with self.lock:
            self.blackout = bool(on)

    def set_loop(self, on):
        with self.lock:
            self.show["loop"] = bool(on)
            self._reschedule()
            self._changed()

    def _reschedule(self):
        """Keeps a running cue's auto-next correct after the cue list changes."""
        index = self._index(self.active_id)
        if self.state_label != "playing" or index is None:
            return
        cue = self.show["cues"][index]
        hold = self._hold_time(cue)
        if hold is None or self._following(index) is None:
            self.next_at = None
        elif self.next_at is None:
            now = time.monotonic()
            fade_end = self.fade["began"] + self.fade["duration"] if self.fade else now
            self.next_at = max(fade_end + hold, now + MIN_CUE_STEP)

    # Effects

    def _stop_effect(self):
        if self.effect_active:
            self.effect_active = False
            self.show["effect"]["running"] = False
            self.state_label = "held"
            self._changed()

    def effect_update(self, body):
        with self.lock:
            settings = {**self.show["effect"], **body, "running": self.show["effect"]["running"]}
            self.show["effect"] = clean_effect(settings)
            self._changed()

    def effect_start(self, body=None):
        with self.lock:
            settings = {**self.show["effect"], **(body or {}), "running": True}
            self.show["effect"] = clean_effect(settings)
            now = time.monotonic()
            self.fade = None
            self.next_at = None
            self.active_id = None
            self.state_label = "effect"
            self.effect_active = True
            self.effect_phase = 0.0
            self.effect_started = now
            self.effect_updated = now
            self.effect_from = [list(level) for level in self.levels]
            self._changed()

    def effect_stop(self):
        with self.lock:
            self._stop_effect()

    def _effect_tick(self, now):
        effect = self.show["effect"]
        self.effect_phase = (self.effect_phase + (now - self.effect_updated) / (2 * effect["time"])) % 1
        self.effect_updated = now
        blend = min((now - self.effect_started) / EFFECT_BLEND_SECONDS, 1)
        count = len(self.levels)
        levels = []
        for index in range(count):
            place = index if effect["direction"] == "forward" else count - 1 - index
            position = effect_position(effect["type"], self.effect_phase, place, count)
            colour = [a + (b - a) * position for a, b in zip(effect["colour1"], effect["colour2"])]
            levels.append([a + (b - a) * blend for a, b in zip(self.effect_from[index], colour)])
        self.levels = levels

    # Cue editing

    def record(self, name, fade, following):
        fade = number(fade, 0, MAX_FADE, "Fade time")
        following = next_time(following)
        with self.lock:
            cues = self.show["cues"]
            cue_id = max((cue["id"] for cue in cues), default=0) + 1
            name = text(name, "Cue name") if name else f"Cue {len(cues) + 1}"
            cues.append({
                "id": cue_id,
                "name": name,
                "fade": fade,
                "next": following,
                "levels": [[round(v) for v in level] for level in self.levels],
            })
            self._changed()

    def edit(self, cue_id, body):
        with self.lock:
            cue = self._cue(cue_id)
            updates = {}
            if "name" in body:
                updates["name"] = text(body["name"], "Cue name")
            if "fade" in body:
                updates["fade"] = number(body["fade"], 0, MAX_FADE, "Fade time")
            if "next" in body:
                updates["next"] = next_time(body["next"])
            cue.update(updates)
            self._reschedule()
            self._changed()

    def update_look(self, cue_id):
        with self.lock:
            self._cue(cue_id)["levels"] = [[round(v) for v in level] for level in self.levels]
            self._changed()

    def delete(self, cue_id):
        with self.lock:
            index = self._index(cue_id)
            if index is None:
                raise ValueError("That cue no longer exists.")
            del self.show["cues"][index]
            if self.active_id == cue_id:
                self.active_id = None
                self.next_at = None
                self.state_label = "manual"
            self._reschedule()
            self._changed()

    def move(self, cue_id, direction):
        if direction not in (-1, 1):
            raise ValueError("A cue moves up or down one place.")
        with self.lock:
            cues = self.show["cues"]
            index = self._index(cue_id)
            if index is None:
                raise ValueError("That cue no longer exists.")
            other = index + direction
            if 0 <= other < len(cues):
                cues[index], cues[other] = cues[other], cues[index]
                self._reschedule()
                self._changed()

    def fade_all(self, fade):
        fade = number(fade, 0, MAX_FADE, "Fade time")
        with self.lock:
            for cue in self.show["cues"]:
                cue["fade"] = fade
            self._changed()

    def patch(self, body):
        ip, universe, layout, fixtures = clean_patch(
            body.get("ip"), body.get("universe"), body.get("layout"), body.get("fixtures")
        )
        with self.lock:
            self.show.update(ip=ip, universe=universe, layout=layout, fixtures=fixtures)
            self.last_data = None
            self._changed()

    def set_address(self, index, address):
        with self.lock:
            fixtures = self.show["fixtures"]
            index = whole(index, 0, len(fixtures) - 1, "Fixture")
            highest = 513 - len(self.show["layout"])
            fixtures[index]["address"] = whole(address, 1, highest, f"{fixtures[index]['name']} address")
            self._changed()

    # Output

    def _frame(self):
        layout = self.show["layout"]
        scale = 0.0 if self.blackout else self.master / 100
        data = bytearray(512)
        output = []
        for fixture, level in zip(self.show["fixtures"], self.levels):
            values = [round(v * scale) for v in level]
            output.append(values)
            channels = {"R": values[0], "G": values[1], "B": values[2], "D": 255, "W": 0}
            for offset, channel in enumerate(layout):
                data[fixture["address"] - 1 + offset] = channels[channel]
        return data, output

    def tick(self):
        now = time.monotonic()
        with self.lock:
            if self.effect_active:
                self._effect_tick(now)
            if self.fade:
                position = (now - self.fade["began"]) / self.fade["duration"]
                if position >= 1:
                    self.levels = self.fade["end"]
                    self.fade = None
                else:
                    eased = (1 - math.cos(math.pi * position)) / 2
                    self.levels = [
                        [a + (b - a) * eased for a, b in zip(start, end)]
                        for start, end in zip(self.fade["start"], self.fade["end"])
                    ]
            if self.next_at is not None and now >= self.next_at:
                following = self._following(self._index(self.active_id))
                if following:
                    self._start(following, now)
                else:
                    self.next_at = None
            data, _ = self._frame()
            ip = self.show["ip"]
            universe = self.show["universe"]
            if data != self.last_data or now - self.last_sent >= KEEPALIVE_SECONDS:
                try:
                    self.artnet.send(ip, universe, data)
                    self.error = None
                except OSError as error:
                    self.error = f"Art-Net could not be sent to {ip}: {error.strerror or error}"
                self.last_data = data
                self.last_sent = now

    def run(self, stop):
        while not stop.is_set():
            self.tick()
            stop.wait(1 / FPS)
        with self.lock:
            try:
                for _ in range(3):
                    self.artnet.send(self.show["ip"], self.show["universe"], bytearray(512))
            except OSError:
                pass

    def state(self):
        now = time.monotonic()
        with self.lock:
            _, output = self._frame()
            warnings = []
            size = len(self.show["layout"])
            used = {}
            for position, fixture in enumerate(self.show["fixtures"]):
                for channel in range(fixture["address"], fixture["address"] + size):
                    if channel in used:
                        warnings.append(f"{fixture['name']} overlaps {used[channel]} on channel {channel}.")
                        break
                    used[channel] = fixture["name"]
            progress = None
            if self.fade:
                progress = min((now - self.fade["began"]) / self.fade["duration"], 1)
            return {
                "rev": self.rev,
                "ip": self.show["ip"],
                "universe": self.show["universe"],
                "layout": self.show["layout"],
                "layouts": LAYOUTS,
                "loop": self.show["loop"],
                "fixtures": self.show["fixtures"],
                "cues": self.show["cues"],
                "levels": [[round(v) for v in level] for level in self.levels],
                "output": output,
                "master": self.master,
                "blackout": self.blackout,
                "activeId": self.active_id,
                "playState": self.state_label,
                "progress": progress,
                "effect": self.show["effect"],
                "effectActive": self.effect_active,
                "nextIn": None if self.next_at is None else max(self.next_at - now, 0),
                "error": self.error,
                "warnings": warnings,
            }


def build_handler(controller):
    actions = {
        "live": lambda b: controller.live(b.get("fixtures"), b.get("rgb")),
        "master": lambda b: controller.set_master(b.get("value")),
        "blackout": lambda b: controller.set_blackout(b.get("on")),
        "loop": lambda b: controller.set_loop(b.get("on")),
        "go": lambda b: controller.go(),
        "back": lambda b: controller.back(),
        "hold": lambda b: controller.hold(),
        "cue/go": lambda b: controller.go_to(b.get("id")),
        "cue/record": lambda b: controller.record(b.get("name"), b.get("fade"), b.get("next")),
        "cue/edit": lambda b: controller.edit(b.get("id"), b),
        "cue/update": lambda b: controller.update_look(b.get("id")),
        "cue/delete": lambda b: controller.delete(b.get("id")),
        "cue/move": lambda b: controller.move(b.get("id"), b.get("direction")),
        "cue/fade_all": lambda b: controller.fade_all(b.get("fade")),
        "patch": controller.patch,
        "fixture/address": lambda b: controller.set_address(b.get("index"), b.get("address")),
        "effect/update": controller.effect_update,
        "effect/start": controller.effect_start,
        "effect/stop": lambda b: controller.effect_stop(),
    }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30

        def log_message(self, *args):
            pass

        def _send(self, status, content_type, payload):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status, value):
            self._send(status, "application/json", json.dumps(value).encode())

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif path == "/api/state":
                self._json(200, controller.state())
            else:
                self._json(404, {"error": "Not found."})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.close_connection = True
                return self._json(411, {"error": "The request has no length."})
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                return self._json(413, {"error": "The request is too large."})
            raw = self.rfile.read(length)
            # Stops other websites in the operator's browser from sending commands.
            origin = self.headers.get("Origin")
            content_type = self.headers.get("Content-Type", "").split(";")[0].strip()
            if content_type != "application/json" or (
                origin and urlparse(origin).netloc != self.headers.get("Host")
            ):
                return self._json(403, {"error": "This request is not allowed."})
            path = urlparse(self.path).path
            action = actions.get(path[5:]) if path.startswith("/api/") else None
            if action is None:
                return self._json(404, {"error": "Not found."})
            try:
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("The request must be a JSON object.")
                action(body)
            except json.JSONDecodeError:
                return self._json(400, {"error": "The request is not valid JSON."})
            except ValueError as error:
                return self._json(400, {"error": str(error)})
            except OSError as error:
                return self._json(500, {"error": f"The show could not be saved: {error.strerror or error}"})
            self._json(200, controller.state())

    return Handler


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fixture Control</title>
<style>
:root {
  color-scheme: dark;
  --ink: #141821; --panel: #1d2330; --raised: #293142; --line: #414b60;
  --text: #edf0f6; --muted: #aab3c4; --amber: #f5b841; --red: #ef5a5f;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--ink); color: var(--text);
  font: 16px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  padding-bottom: 104px;
}
h1, h2 { margin: 0; line-height: 1.2; font-weight: 650; }
h1 { font-size: 1.05rem; }
h2 { font-size: 1.15rem; }
button, input, select { font: inherit; color: inherit; }
button, select, input:not([type=range]):not([type=color]) {
  min-height: 42px; border: 1px solid var(--line); border-radius: 6px; background: var(--raised);
  padding: 7px 11px;
}
input:not([type=range]):not([type=color]) { background: var(--ink); min-width: 0; width: 100%; }
button { cursor: pointer; font-weight: 550; }
button:hover:not(:disabled) { border-color: var(--muted); }
button:disabled { opacity: .45; cursor: default; }
:focus-visible { outline: 3px solid var(--amber); outline-offset: 2px; }
label { display: block; color: var(--muted); font-size: .85rem; }
label > input, label > select { display: block; width: 100%; margin-top: 4px; color: var(--text); }
input[type=range] { width: 100%; height: 30px; margin: 0; accent-color: var(--amber); }
header { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; padding: 14px 20px 0; flex-wrap: wrap; }
#destination { color: var(--muted); font-size: .85rem; overflow-wrap: anywhere; }

/* The rig: the page's only decoration, and it shows the real output. */
#rig { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 8px; padding: 14px 20px 18px; }
.lamp {
  --c: rgb(0,0,0); display: block; width: 100%; min-height: 112px; padding: 0 6px 8px; overflow: hidden;
  background: var(--panel); border: 2px solid transparent; border-radius: 8px; position: relative;
}
.lamp[aria-pressed=true] { border-color: var(--amber); }
.lamp::before {
  content: ""; position: absolute; inset: 0;
  background: radial-gradient(ellipse 85% 90% at 50% 0, var(--c), transparent 100%);
  transition: background .2s linear;
}
.lamp span { position: relative; display: block; }
.lamp .head { width: 34px; height: 34px; margin: 12px auto 14px; border-radius: 50%; background: var(--c); border: 2px solid rgba(255,255,255,.55); }
.lamp .name {
  font-size: .85rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  background: rgba(20,24,33,.82); border-radius: 4px; padding: 1px 4px;
}
.fixture { min-width: 0; }
.fixture label { margin-top: 6px; font-size: .8rem; }
.fixture label > input { margin-top: 2px; padding: 6px 8px; font-variant-numeric: tabular-nums; }
#rigWarnings { padding: 0 20px; }
#rigWarnings p { margin: 0 0 12px; }
#layout { display: grid; grid-template-columns: minmax(280px, 340px) minmax(0, 1fr); gap: 14px; padding: 0 20px; align-items: start; }
.panel { background: var(--panel); border-radius: 8px; padding: 16px; }
.heading { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 12px; min-height: 42px; flex-wrap: wrap; }
.group { display: flex; gap: 6px; }
.channel { display: grid; grid-template-columns: 1.2rem 1fr 2.2rem; gap: 8px; align-items: center; margin-bottom: 6px; }
.channel output { text-align: right; font-variant-numeric: tabular-nums; }
#swatches { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin-top: 12px; }
#swatches button { display: flex; gap: 6px; align-items: center; padding: 6px; font-size: .8rem; font-weight: 500; min-width: 0; }
#swatches i { flex: none; width: 15px; height: 15px; border-radius: 50%; border: 1px solid rgba(255,255,255,.5); }
#pickerRow { display: flex; align-items: center; gap: 10px; margin-top: 10px; color: var(--muted); font-size: .85rem; }
input[type=color] { width: 56px; height: 36px; padding: 2px; background: var(--ink); border: 1px solid var(--line); border-radius: 6px; }
#selectHint { color: var(--amber); font-size: .9rem; margin: 8px 0 0; }
.column { display: grid; gap: 14px; min-width: 0; }
#effectForm { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
#effectForm input[type=color] { width: 100%; height: 42px; }
#effectForm .full { grid-column: 1 / -1; }
#effectButton { background: var(--text); color: var(--ink); border-color: var(--text); width: 100%; }
#effectButton.running { background: var(--amber); border-color: var(--amber); color: #1a1408; }

#record { display: grid; grid-template-columns: minmax(120px, 1fr) 96px 136px auto; gap: 8px; align-items: end; padding-bottom: 14px; border-bottom: 1px solid var(--line); }
#record button { background: var(--text); color: var(--ink); border-color: var(--text); }
#cues { list-style: none; margin: 0; padding: 0; }
.cue {
  display: grid; grid-template-columns: 26px minmax(100px,1fr) 92px 84px 96px auto; gap: 8px; align-items: end;
  padding: 11px 8px; border-bottom: 1px solid var(--line); position: relative;
}
.cue.active { background: var(--raised); box-shadow: inset 3px 0 var(--amber); }
.cue .number { align-self: center; color: var(--muted); font-variant-numeric: tabular-nums; }
.cue.active .number { color: var(--amber); font-weight: 700; }
.colours { display: flex; height: 42px; border-radius: 6px; overflow: hidden; border: 1px solid var(--line); }
.colours i { flex: 1; }
.tools { display: flex; gap: 4px; }
.tools button { padding: 7px 9px; }
.tools .run { background: var(--amber); border-color: var(--amber); color: #1a1408; }
.bar { position: absolute; left: 0; bottom: -1px; height: 3px; background: var(--amber); }
#empty { padding: 20px 8px; color: var(--muted); margin: 0; }
#allFades { display: flex; gap: 8px; align-items: end; margin-top: 14px; flex-wrap: wrap; }
#allFades label { width: 110px; }
details { margin: 14px 20px 0; }
summary { cursor: pointer; font-weight: 650; font-size: 1.15rem; padding: 2px; }
#patchForm { margin-top: 14px; }
#patchTop { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
#patchFixtures { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px 14px; margin: 14px 0; }
.patchRow { display: grid; grid-template-columns: 1fr 88px; gap: 8px; }
#patchActions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.warning { color: var(--amber); margin: 10px 0 0; }

#transport {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 3; background: #0d1017; border-top: 1px solid var(--line);
  display: grid; grid-template-columns: auto minmax(100px, 1fr) minmax(140px, 240px) auto; gap: 12px; align-items: center;
  padding: 10px 20px calc(10px + env(safe-area-inset-bottom));
}
#transport .group button { min-width: 70px; min-height: 64px; }
#go { min-width: 148px !important; background: var(--amber); border-color: var(--amber); color: #1a1408; font-size: 1.5rem; font-weight: 800; }
#playState { min-width: 0; overflow: hidden; }
#playState strong, #playState span { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#playState span { color: var(--muted); font-size: .85rem; font-variant-numeric: tabular-nums; }
#blackout { min-height: 64px; }
#blackout[aria-pressed=true] { background: var(--red); border-color: var(--red); color: #16080a; font-weight: 750; }
#loop[aria-pressed=true], #several[aria-pressed=true] { border-color: var(--amber); color: var(--amber); }
#notice {
  position: fixed; left: 50%; bottom: 104px; transform: translateX(-50%); z-index: 4; width: min(520px, calc(100% - 32px));
  background: var(--text); color: var(--ink); border-radius: 6px; padding: 10px 14px; font-weight: 550;
}
#notice.bad { background: var(--red); color: #16080a; }
#notice:empty { display: none; }
#problem { background: var(--red); color: #16080a; padding: 9px 20px; font-weight: 600; }
#problem:empty { display: none; }

@media (max-width: 1180px) {
  #layout { grid-template-columns: 1fr; }
}
@media (max-width: 820px) {
  body { padding-bottom: 186px; }
  header { padding: 12px 16px 0; }
  #rig { padding: 12px 16px 14px; grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .lamp { min-height: 88px; }
  .lamp .head { width: 26px; height: 26px; margin: 8px auto 8px; }
  #layout { padding: 0 16px; }
  #rigWarnings { padding: 0 16px; }
  details { margin: 14px 16px 0; }
  #record { grid-template-columns: 1fr 1fr; }
  #record .wide { grid-column: 1 / -1; }
  .cue { grid-template-columns: 26px 1fr 1fr; padding: 13px 4px; }
  .cue .nameCell { grid-column: 2 / -1; }
  .cue .colours { grid-column: 1 / -1; order: 3; }
  .cue .tools { grid-column: 1 / -1; order: 4; }
  .cue .fadeCell { grid-column: 1 / 3; order: 2; padding-left: 0; }
  .cue .nextCell { order: 2; }
  .tools button { flex: 1 1 auto; min-width: 0; padding: 7px 4px; }
  #transport { grid-template-columns: 1fr 1fr; padding: 8px 16px calc(8px + env(safe-area-inset-bottom)); gap: 8px; }
  #playState { grid-column: 1 / -1; grid-row: 1; }
  #masterCell { grid-column: 1 / 2; grid-row: 2; }
  #blackoutCell { grid-column: 2 / 3; grid-row: 2; }
  #blackout { width: 100%; min-height: 44px; }
  #transportButtons { grid-column: 1 / -1; grid-row: 3; }
  #transportButtons button { flex: 1; min-width: 0 !important; min-height: 56px !important; }
  #go { flex: 2 !important; }
  #notice { bottom: 190px; }
}
@media (max-width: 360px) { #swatches { grid-template-columns: repeat(2, 1fr); } }
@media (prefers-reduced-motion: reduce) { .lamp::before { transition: none; } }
</style>
</head>
<body>
<div id="problem" role="alert"></div>
<header>
  <h1>Fixture Control</h1>
  <div id="destination"></div>
</header>
<section id="rig" aria-label="Fixtures. Select the ones to change, or edit their start addresses."></section>
<div id="rigWarnings" role="status"></div>

<main id="layout">
  <div class="column">
  <section class="panel" aria-labelledby="colourTitle">
    <div class="heading">
      <h2 id="colourTitle">Colour</h2>
      <div class="group">
        <button id="several" type="button" aria-pressed="false" title="Click fixtures to add or remove them. You can also hold Ctrl while clicking.">Several</button>
        <button id="selectAll" type="button">All</button>
        <button id="selectNone" type="button">None</button>
      </div>
    </div>
    <div class="channel"><span aria-hidden="true">R</span><input id="r" type="range" min="0" max="255" aria-label="Red"><output id="rv"></output></div>
    <div class="channel"><span aria-hidden="true">G</span><input id="g" type="range" min="0" max="255" aria-label="Green"><output id="gv"></output></div>
    <div class="channel"><span aria-hidden="true">B</span><input id="b" type="range" min="0" max="255" aria-label="Blue"><output id="bv"></output></div>
    <div id="pickerRow"><input id="picker" type="color" aria-label="Colour picker"><span>Pick any colour</span></div>
    <div id="swatches"></div>
    <p id="selectHint" hidden>Select a fixture to change its colour.</p>
  </section>

  <section class="panel" aria-labelledby="effectTitle">
    <div class="heading"><h2 id="effectTitle">Effects</h2></div>
    <form id="effectForm">
      <label class="full">Effect
        <select id="effectType">
          <option value="wave">Wave: a smooth ripple across the fixtures</option>
          <option value="chase">Chase: fixtures change one after another</option>
        </select>
      </label>
      <label>Colour 1<input id="effectColour1" type="color"></label>
      <label>Colour 2<input id="effectColour2" type="color"></label>
      <label>Time (seconds)<input id="effectTime" type="number" min="1" max="100" step="any" required></label>
      <label>Direction
        <select id="effectDirection">
          <option value="forward">First to last</option>
          <option value="reverse">Last to first</option>
        </select>
      </label>
      <p class="full" style="margin:0;color:var(--muted);font-size:.85rem">Time is how long the fixtures take to go from colour 1 to colour 2.</p>
      <button id="effectButton" class="full" type="button">Start effect</button>
    </form>
  </section>
  </div>

  <section class="panel" aria-labelledby="cuesTitle">
    <div class="heading">
      <h2 id="cuesTitle">Cues</h2>
      <button id="loop" type="button" aria-pressed="false">Loop</button>
    </div>
    <form id="record">
      <label class="wide">New cue name<input id="newName" maxlength="40" placeholder="Optional"></label>
      <label>Fade (seconds)<input id="newFade" type="number" min="0" max="100" step="any" value="5" required></label>
      <label>Auto-next (seconds)<input id="newNext" type="number" min="0" max="3600" step="any" placeholder="Wait for GO"></label>
      <button class="wide" type="submit">Record current look</button>
    </form>
    <ol id="cues"></ol>
    <p id="empty" hidden>No cues yet. Set a look, then select Record current look.</p>
    <form id="allFades">
      <label>Every fade (seconds)<input id="allFade" type="number" min="0" max="100" step="any" required></label>
      <button type="submit">Set all cue fades</button>
    </form>
  </section>
</main>

<details id="patch" class="panel">
  <summary>Art-Net setup</summary>
  <form id="patchForm">
    <div id="patchTop">
      <label>Send to IP address<input id="patchIp" required></label>
      <label>Universe (first is 0)<input id="patchUniverse" type="number" min="0" max="32767" required></label>
      <label>Fixture channel layout<select id="patchLayout"></select></label>
    </div>
    <div id="patchFixtures"></div>
    <div id="patchActions">
      <button type="submit">Save Art-Net setup</button>
      <button id="autoAddress" type="button">Number addresses in order from fixture 1</button>
    </div>
    <div id="warnings"></div>
  </form>
</details>

<div id="notice" role="status"></div>
<section id="transport" aria-label="Playback">
  <div id="transportButtons" class="group">
    <button id="back" type="button">Back</button>
    <button id="hold" type="button">Hold</button>
    <button id="go" type="button">GO</button>
  </div>
  <div id="playState" aria-live="off"><strong></strong><span></span></div>
  <label id="masterCell">Master <span id="masterValue"></span><input id="master" type="range" min="0" max="100"></label>
  <div id="blackoutCell"><button id="blackout" type="button" aria-pressed="false">Blackout</button></div>
</section>

<script>
const $ = id => document.getElementById(id);
const COLOURS = [["Red",[255,0,0]],["Amber",[255,140,0]],["Green",[0,255,0]],["Cyan",[0,255,255]],
                 ["Blue",[0,0,255]],["Pink",[255,0,180]],["White",[255,255,255]],["Off",[0,0,0]]];
let S = null, renderedRev = 0, patchRev = 0, editedAt = -Infinity, masterAt = -Infinity, noticeTimer = 0;
let selected = new Set([0,1,2,3,4,5]), selectSeveral = false;
let pendingLive = null, liveBusy = false, renderWaiting = false, connected = true;

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "data") Object.assign(node.dataset, value);
    else if (key === "css") node.style.cssText = value;
    else node[key] = value;
  }
  node.append(...children);
  return node;
}

async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "The controller rejected that action.");
  return data;
}
async function api(path, body = {}) {
  const data = await request("/api/" + path, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
  });
  apply(data);
  return data;
}
function notice(message, bad = false) {
  clearTimeout(noticeTimer);
  $("notice").textContent = message;
  $("notice").className = bad ? "bad" : "";
  noticeTimer = setTimeout(() => { $("notice").textContent = ""; }, bad ? 6000 : 2500);
}
const act = (path, body, done) => api(path, body).then(() => done && notice(done)).catch(e => notice(e.message, true));

function sendLive(colour) {
  if (!selected.size) return;
  editedAt = performance.now();
  pendingLive = {fixtures: [...selected], rgb: colour};
  pump();
}
async function pump() {
  if (liveBusy || !pendingLive) return;
  liveBusy = true;
  const body = pendingLive;
  pendingLive = null;
  try { await api("live", body); } catch (e) { notice(e.message, true); }
  liveBusy = false;
  pump();
}
const hex = c => "#" + c.map(v => v.toString(16).padStart(2, "0")).join("");
const css = c => `rgb(${c[0]},${c[1]},${c[2]})`;

function showColour(c) {
  ["r","g","b"].forEach((id, i) => { $(id).value = c[i]; $(id + "v").textContent = c[i]; });
  $("picker").value = hex(c);
}
function sliderColour() { return ["r","g","b"].map(id => Number($(id).value)); }

function renderRig() {
  $("rig").replaceChildren(...S.fixtures.map((fixture, i) => {
    const lamp = el("button", {type: "button", className: "lamp", title: fixture.name, onclick(e) {
      if (selectSeveral || e.ctrlKey || e.metaKey || e.shiftKey) {
        selected.has(i) ? selected.delete(i) : selected.add(i);
      } else {
        selected = new Set([i]);
      }
      syncSelection();
    }}, el("span", {className: "head"}), el("span", {className: "name", textContent: fixture.name}));
    const address = el("input", {type: "number", min: 1, max: 513 - S.layout.length, step: 1, value: fixture.address,
      ariaLabel: `${fixture.name} start address`, data: {key: "address" + i}, onchange(e) {
        const value = Number(e.target.value);
        api("fixture/address", {index: i, address: value})
          .then(() => notice(`${fixture.name} now starts at address ${value}`))
          .catch(err => { notice(err.message, true); e.target.value = S.fixtures[i].address; });
      }});
    return el("div", {className: "fixture"}, lamp, el("label", {}, "Start address", address));
  }));
}
function syncSelection() {
  document.querySelectorAll(".lamp").forEach((lamp, i) => lamp.setAttribute("aria-pressed", selected.has(i)));
  const none = !selected.size;
  $("selectHint").hidden = !none;
  ["r","g","b","picker"].forEach(id => { $(id).disabled = none; });
  document.querySelectorAll("#swatches button").forEach(b => { b.disabled = none; });
  if (S && !none) showColour(S.levels[Math.min(...selected)]);
}

function numberField(label, value, attrs, onchange) {
  return el("label", {}, label, el("input", {
    type: "number", value: value === null ? "" : value, step: "any", ...attrs, onchange
  }));
}
function renderCues() {
  const focusKey = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : null;
  const rows = S.cues.map((cue, i) => {
    const edit = (key, value, input) => api("cue/edit", {id: cue.id, [key]: value}).catch(e => {
      notice(e.message, true); input.value = cue[key] === null ? "" : cue[key];
    });
    const tool = (text, key, title, onclick, cls = "") =>
      el("button", {type: "button", textContent: text, title, ariaLabel: `${title}: ${cue.name}`, className: cls, data: {key: key + cue.id}, onclick});
    const up = tool("↑", "up", "Move up", () => act("cue/move", {id: cue.id, direction: -1}));
    const down = tool("↓", "down", "Move down", () => act("cue/move", {id: cue.id, direction: 1}));
    up.disabled = i === 0;
    down.disabled = i === S.cues.length - 1;
    return el("li", {className: "cue", data: {id: cue.id}},
      el("span", {className: "number", textContent: i + 1}),
      el("label", {className: "nameCell"}, "Name", el("input", {value: cue.name, maxLength: 40, data: {key: "name" + cue.id},
        onchange(e) { edit("name", e.target.value, e.target); }})),
      el("div", {className: "fadeCell"}, numberField("Fade (s)", cue.fade, {min: 0, max: 100, required: true},
        e => { if (e.target.value !== "") edit("fade", Number(e.target.value), e.target); else e.target.value = cue.fade; })),
      el("div", {className: "nextCell"}, numberField("Auto-next", cue.next, {min: 0, max: 3600, placeholder: S.loop ? "0" : "GO"},
        e => edit("next", e.target.value === "" ? null : Number(e.target.value), e.target))),
      el("div", {className: "colours", title: "Colours in this cue"}, ...cue.levels.map(c => el("i", {css: "background:" + css(c)}))),
      el("div", {className: "tools"},
        tool("Run", "run", "Run cue", () => act("cue/go", {id: cue.id}), "run"),
        tool("Update", "update", "Save the current look into this cue", () => act("cue/update", {id: cue.id}, `Updated ${cue.name}`)),
        up, down,
        tool("✕", "delete", "Delete cue", () => { if (confirm(`Delete ${cue.name}?`)) act("cue/delete", {id: cue.id}, `Deleted ${cue.name}`); })),
      el("div", {className: "bar", hidden: true}));
  });
  $("cues").replaceChildren(...rows);
  $("empty").hidden = S.cues.length > 0;
  if (focusKey) {
    const again = document.querySelector(`[data-key="${focusKey}"]`);
    if (again) again.focus();
  }
}

function renderPatch() {
  if ($("patchForm").contains(document.activeElement)) return;
  $("patchIp").value = S.ip;
  $("patchUniverse").value = S.universe;
  $("patchLayout").replaceChildren(...S.layouts.map(name => el("option", {
    value: name, textContent: name, selected: name === S.layout
  })));
  $("patchFixtures").replaceChildren(...S.fixtures.map((fixture, i) => el("div", {className: "patchRow"},
    el("label", {}, `Fixture ${i + 1} name`, el("input", {value: fixture.name, maxLength: 40, required: true, data: {field: "name"}})),
    el("label", {}, "DMX address", el("input", {type: "number", min: 1, max: 512, value: fixture.address, required: true, data: {field: "address"}})))));
  patchRev = S.rev;
}

function apply(data) {
  S = data;
  if (S.rev !== renderedRev) {
    const typing = document.activeElement && document.activeElement.matches("#cues input, #rig input");
    if (typing) renderWaiting = true;
    else {
      renderedRev = S.rev;
      renderRig(); renderCues(); syncSelection();
    }
    if (patchRev !== S.rev) renderPatch();
    document.querySelectorAll("#rig input").forEach((input, i) => {
      if (input !== document.activeElement) input.value = S.fixtures[i].address;
    });
  }
  document.querySelectorAll(".lamp").forEach((lamp, i) => lamp.style.setProperty("--c", css(S.output[i])));
  const slidersBusy = performance.now() - editedAt < 800 || liveBusy || pendingLive;
  if (selected.size && !slidersBusy) showColour(S.levels[Math.min(...selected)]);
  if (performance.now() - masterAt > 800) $("master").value = S.master;
  $("masterValue").textContent = (performance.now() - masterAt > 800 ? S.master : $("master").value) + "%";
  $("blackout").setAttribute("aria-pressed", S.blackout);
  $("loop").setAttribute("aria-pressed", S.loop);
  $("loop").textContent = S.loop ? "Loop on" : "Loop off";
  $("newNext").placeholder = S.loop ? "0" : "Wait for GO";
  $("destination").textContent = `Art-Net to ${S.ip}, universe ${S.universe}, ${S.layout} fixtures`;
  document.querySelectorAll(".cue").forEach(row => {
    const active = Number(row.dataset.id) === S.activeId;
    row.classList.toggle("active", active);
    const bar = row.querySelector(".bar");
    bar.hidden = !(active && S.progress !== null);
    if (!bar.hidden) bar.style.width = (S.progress * 100) + "%";
  });
  renderPlayState();
  renderEffect();
  $("warnings").replaceChildren(...S.warnings.map(w => el("p", {className: "warning", textContent: w})));
  $("rigWarnings").replaceChildren(...S.warnings.map(w => el("p", {className: "warning", textContent: w})));
  setProblem(S.error || "");
}
function setProblem(message) { if ($("problem").textContent !== message) $("problem").textContent = message; }

const toRgb = v => [1,3,5].map(i => parseInt(v.slice(i, i + 2), 16));
function effectSettings() {
  return {
    type: $("effectType").value,
    colour1: toRgb($("effectColour1").value),
    colour2: toRgb($("effectColour2").value),
    time: Number($("effectTime").value),
    direction: $("effectDirection").value
  };
}
function renderEffect() {
  if (!$("effectForm").contains(document.activeElement) || document.activeElement === $("effectButton")) {
    $("effectType").value = S.effect.type;
    $("effectColour1").value = hex(S.effect.colour1);
    $("effectColour2").value = hex(S.effect.colour2);
    $("effectTime").value = S.effect.time;
    $("effectDirection").value = S.effect.direction;
  }
  $("effectButton").textContent = S.effectActive ? "Stop effect" : "Start effect";
  $("effectButton").classList.toggle("running", S.effectActive);
}

function renderPlayState() {
  const cue = S.cues.find(c => c.id === S.activeId);
  const name = cue ? cue.name : "";
  let title = "Ready", detail = S.cues.length ? "Select GO to run the first cue" : "Record a cue to begin";
  if (S.effectActive) { title = S.effect.type === "wave" ? "Wave effect" : "Chase effect"; detail = `${S.effect.time} seconds, colour 1 to colour 2`; }
  else if (S.progress !== null) { title = name; detail = `Fading, ${Math.round(S.progress * 100)}%`; }
  else if (S.nextIn !== null) { title = name; detail = `Next cue in ${S.nextIn.toFixed(1)} s`; }
  else if (S.playState === "playing") { title = name; detail = "Waiting for GO"; }
  else if (S.playState === "held") { title = "Held"; detail = name ? `Stopped during ${name}` : "Output is holding"; }
  else if (S.playState === "manual") { title = "Manual"; detail = "Colour set by hand"; }
  if (S.blackout) detail = "Blackout is on. " + detail;
  $("playState").querySelector("strong").textContent = title;
  $("playState").querySelector("span").textContent = detail;
}

async function poll() {
  if (!document.hidden) {
    try {
      const data = await request("/api/state");
      apply(data);
      connected = true;
    } catch (e) {
      connected = false;
      setProblem("Can't reach the controller. Check the network connection. Retrying.");
    }
  }
  setTimeout(poll, 200);
}

["r","g","b"].forEach(id => $(id).addEventListener("input", () => {
  const colour = sliderColour();
  showColour(colour);
  sendLive(colour);
}));
$("picker").addEventListener("input", e => {
  const v = e.target.value;
  const colour = [1,3,5].map(i => parseInt(v.slice(i, i + 2), 16));
  showColour(colour);
  sendLive(colour);
});
$("swatches").append(...COLOURS.map(([name, colour]) => el("button", {type: "button", onclick() {
  showColour(colour); sendLive(colour);
}}, el("i", {css: "background:" + css(colour)}), name)));
$("several").onclick = () => {
  selectSeveral = !selectSeveral;
  $("several").setAttribute("aria-pressed", selectSeveral);
};
$("selectAll").onclick = () => { selected = new Set(S.fixtures.map((_, i) => i)); syncSelection(); };
$("selectNone").onclick = () => { selected = new Set(); syncSelection(); };
$("effectButton").onclick = () => {
  if (S.effectActive) return act("effect/stop");
  if (!$("effectForm").reportValidity()) return;
  act("effect/start", effectSettings());
};
$("effectForm").addEventListener("submit", e => e.preventDefault());
["effectType", "effectColour1", "effectColour2", "effectDirection", "effectTime"].forEach(id => {
  $(id).addEventListener("change", () => {
    if ($("effectForm").checkValidity()) act("effect/update", effectSettings());
    else {
      notice("Effect time must be a number from 1 to 100.", true);
      $("effectTime").value = S.effect.time;
    }
  });
});
$("go").onclick = () => act("go");
$("back").onclick = () => act("back");
$("hold").onclick = () => act("hold");
$("loop").onclick = () => act("loop", {on: !S.loop});
$("blackout").onclick = () => act("blackout", {on: !S.blackout});
$("master").addEventListener("input", e => {
  masterAt = performance.now();
  $("masterValue").textContent = e.target.value + "%";
  api("master", {value: Number(e.target.value)}).catch(err => notice(err.message, true));
});
$("record").addEventListener("submit", e => {
  e.preventDefault();
  const next = $("newNext").value;
  api("cue/record", {name: $("newName").value.trim() || null, fade: Number($("newFade").value), next: next === "" ? null : Number(next)})
    .then(() => { $("newName").value = ""; notice("Recorded cue"); })
    .catch(err => notice(err.message, true));
});
$("allFades").addEventListener("submit", e => {
  e.preventDefault();
  const fade = Number($("allFade").value);
  act("cue/fade_all", {fade}, `Every cue now fades in ${fade} seconds`);
});
$("autoAddress").onclick = () => {
  const size = $("patchLayout").value.length;
  document.querySelectorAll('[data-field="address"]').forEach((input, i) => { input.value = 1 + i * size; });
};
$("patchForm").addEventListener("submit", e => {
  e.preventDefault();
  const rows = [...document.querySelectorAll(".patchRow")];
  document.activeElement.blur();
  act("patch", {
    ip: $("patchIp").value.trim(),
    universe: Number($("patchUniverse").value),
    layout: $("patchLayout").value,
    fixtures: rows.map(row => ({
      name: row.querySelector('[data-field="name"]').value,
      address: Number(row.querySelector('[data-field="address"]').value)
    }))
  }, "Saved Art-Net setup");
});
document.addEventListener("focusout", () => setTimeout(() => {
  if (renderWaiting && !document.activeElement.matches("#cues input, #rig input")) {
    renderWaiting = false;
    renderedRev = S.rev; renderRig(); renderCues(); syncSelection();
  }
}));
document.addEventListener("keydown", e => {
  if (e.code === "Space" && !e.repeat && !e.target.closest("input, select, button, summary, textarea")) {
    e.preventDefault();
    act("go");
  }
});
poll();
</script>
</body>
</html>
"""


def parse_args():
    parser = argparse.ArgumentParser(description="Art-Net fixture controller with a web page and cues.")
    parser.add_argument("--port", type=int, default=8080, help="web page port")
    parser.add_argument("--bind", default="0.0.0.0", help="network address for the web page")
    parser.add_argument("--artnet-port", type=int, default=6454, help="Art-Net UDP port")
    parser.add_argument(
        "--show",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "show.json"),
        help="file used to save cues and setup",
    )
    parser.add_argument("--autostart", action="store_true", help="at startup, resume a running effect or run the first cue")
    return parser.parse_args()


def main():
    args = parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    controller = Controller(args.show, ArtNet(args.artnet_port))
    if args.autostart:
        if controller.show["effect"]["running"]:
            controller.effect_start()
        elif controller.show["cues"]:
            controller.go()

    try:
        server = ThreadingHTTPServer((args.bind, args.port), build_handler(controller))
    except OSError as error:
        sys.exit(f"Could not open port {args.port}: {error.strerror or error}")
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(
        target=controller.artnet.listen,
        args=(lambda: controller.show["universe"], stop),
        daemon=True,
    ).start()

    print(f"Control page: http://<pi-address>:{args.port}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    controller.run(stop)
    server.shutdown()
    print("Stopped and fixtures turned off.", flush=True)


if __name__ == "__main__":
    main()
