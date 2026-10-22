"""Spotify Connect receiver -> bounded live PCM stream -> Kodi player."""
import collections
import json
import os
import queue
import struct
import threading
import time
import uuid

import xbmc
import xbmcgui

FRAME_LIMIT = 1024 * 1024
PCM_PER_SECOND = 44100 * 2 * 2
LIVE_WAV_HEADER = struct.pack(
    "<4sI4s4sIHHIIHH4sI", b"RIFF", 0xFFFFFFFF, b"WAVE", b"fmt ", 16,
    1, 2, 44100, PCM_PER_SECOND, 4, 16, b"data", 0xFFFFFFFF,
)


def read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(size - len(result))
        if not chunk:
            raise EOFError("Connect receiver closed its audio pipe")
        result.extend(chunk)
    return bytes(result)


def read_frame(stream):
    header = read_exact(stream, 9)
    size = struct.unpack("<I", header[5:])[0]
    if header[:4] != b"RSC1" or header[4:5] not in (b"A", b"E") or size > FRAME_LIMIT:
        raise ValueError("Invalid Connect audio frame")
    return header[4:5], read_exact(stream, size)


class AudioBuffer:
    """Bound memory/latency even when Kodi stops consuming the live stream."""
    def __init__(self, limit=PCM_PER_SECOND * 10):
        self.limit = limit
        self.condition = threading.Condition()
        self.chunks = collections.deque()
        self.size = 0
        self.generation = 0
        self.closed = False
        self.reader_id = 0
        self.readers = {}
        self.received = self.dropped = self.waits = 0

    def reset(self, closed=False):
        with self.condition:
            self.generation += 1
            self.chunks.clear()
            self.size = 0
            self.readers.clear()
            self.closed = closed
            self.condition.notify_all()
            return self.generation

    def put(self, data):
        if not data:
            return
        if len(data) % 4:
            raise ValueError("Connect PCM is not stereo frame aligned")
        with self.condition:
            if self.closed:
                return
            data = data[-self.limit:]
            self.received += len(data)
            self.chunks.append(data)
            self.size += len(data)
            while self.size > self.limit:
                self.size -= len(self.chunks.popleft())
            for chunks in self.readers.values():
                chunks.append(data)
                size = sum(map(len, chunks))
                while size > self.limit:
                    removed = len(chunks.popleft())
                    size -= removed
                    self.dropped += removed
            self.condition.notify_all()

    def claim_reader(self):
        with self.condition:
            self.reader_id += 1
            self.readers[self.reader_id] = collections.deque(self.chunks)
            self.condition.notify_all()
            return self.reader_id

    def release_reader(self, reader):
        with self.condition:
            self.readers.pop(reader, None)

    def read(self, generation, reader=None):
        with self.condition:
            chunks = self.chunks if reader is None else self.readers.get(reader)
            if generation == self.generation and not self.closed and chunks is not None and not chunks:
                self.waits += 1
                self.condition.wait(1)
            if generation != self.generation or self.closed or chunks is None:
                return b""
            if not chunks:
                return None
            chunk = chunks.popleft()
            if reader is None:
                self.size -= len(chunk)
            return chunk


class ConnectBridge:
    def __init__(self, spotty, auth, player, directory):
        self.spotty, self.auth, self.player = spotty, auth, player
        self.directory = directory
        self.audio = AudioBuffer()
        self.events = queue.Queue(maxsize=256)
        self.process = None
        self.enabled = False
        self.available = spotty.supports_connect_bridge()
        self.session = uuid.uuid4().hex
        self.metadata = {}
        self.uri = ""
        self.remote_state = "stopped"
        self.active = False
        self.switching = False
        self.restart = False
        self.next_start = 0
        self.reader = None
        self.command_lock = threading.Lock()
        self.pending_item = None

    def stream_path(self):
        return "/connect/{}/{}.wav".format(self.session, self.audio.generation)

    def owns_player(self):
        try:
            if not self.player.isPlaying():
                return False
            return self.player.getPlayingFile().startswith("http://127.0.0.1:52309/connect/")
        except RuntimeError:
            return False

    def command(self, name, **values):
        xbmc.log("Resonance Connect command: " + name, xbmc.LOGINFO)
        with self.command_lock:
            if self.process is None or self.process.poll() is not None:
                return
            try:
                self.process.stdin.write((json.dumps(dict(command=name, **values)) + "\n").encode())
                self.process.stdin.flush()
            except (OSError, ValueError):
                pass

    def _read(self, process):
        report = time.monotonic()
        received = 0
        read_seconds = put_seconds = max_put = 0
        try:
            while True:
                before = time.monotonic()
                kind, payload = read_frame(process.stdout)
                read_seconds += time.monotonic() - before
                if kind == b"A":
                    before = time.monotonic()
                    self.audio.put(payload)
                    elapsed = time.monotonic() - before
                    put_seconds += elapsed
                    max_put = max(max_put, elapsed)
                    received += len(payload)
                else:
                    event = json.loads(payload)
                    if not isinstance(event, dict):
                        raise ValueError("Invalid Connect event")
                    xbmc.log("Resonance Connect event: " + str(event.get("event")), xbmc.LOGINFO)
                    if event.get("event") in ("loading", "seeked", "stopped", "unavailable"):
                        self.switching = True
                        self.audio.reset()
                    try:
                        self.events.put_nowait(event)
                    except queue.Full:
                        raise ValueError("Connect event queue overflow")
                if time.monotonic() - report >= 10:
                    self.events.put_nowait({'event': 'pipe_stats', 'bytes': received,
                        'read_ms': round(read_seconds * 1000), 'put_ms': round(put_seconds * 1000),
                        'max_put_ms': round(max_put * 1000)})
                    report = time.monotonic()
                    received = 0
                    read_seconds = put_seconds = max_put = 0
        except (EOFError, OSError, ValueError) as exc:
            xbmc.log("Resonance Connect pipe closed: " + str(exc), xbmc.LOGWARNING)
        finally:
            if self.process is process:
                self.audio.reset(closed=True)
                if process.poll() is None:
                    process.terminate()

    def _start(self):
        if time.monotonic() < self.next_start:
            return
        self.next_start = time.monotonic() + 15
        if not self.auth._acquire_connect_start_lock():
            return
        try:
            if self.reader is not None:
                self.reader.join(timeout=3)
            if self.process is not None:
                for stream in (self.process.stdin, self.process.stdout):
                    if stream is not None:
                        stream.close()
            # Replace the silent pairing receiver, releasing Zeroconf port 10002.
            self.auth._stop_previous_connect_process()
            self.auth._cleanup_orphaned_posix_connect_processes()
            self.audio.reset()
            self.remote_state = "stopped"
            self.active = False
            self.metadata = {}
            self.uri = ""
            while not self.events.empty():
                self.events.get_nowait()
            self.process = self.spotty.run_connect_bridge(os.path.join(self.directory, "connect-bridge.log"))
            self.auth.keep_connect_process(self.process)
            self.reader = threading.Thread(target=self._read, args=(self.process,), daemon=True)
            self.reader.start()
            xbmc.log("Resonance Connect audio receiver started", xbmc.LOGINFO)
        except (OSError, RuntimeError) as exc:
            xbmc.log("Resonance Connect start failed: " + str(exc), xbmc.LOGERROR)
        finally:
            self.auth._release_connect_start_lock()

    def tick(self):
        try:
            self._tick()
        except (RuntimeError, OSError, ValueError) as exc:
            self.active = False
            self.switching = False
            xbmc.log("Resonance Connect player update failed: " + str(exc), xbmc.LOGERROR)

    def _tick(self):
        if not self.enabled or not self.available:
            return
        if self.process is None or self.process.poll() is not None:
            self._start()
        for _ in range(256):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("event")
            if kind == "pipe_stats":
                xbmc.log("Resonance Connect pipe: " + str(event), xbmc.LOGINFO)
            elif kind == "track":
                self.metadata = event
            elif kind in ("playing", "paused", "loading", "stopped", "unavailable"):
                self.remote_state = kind
                if event.get("uri"):
                    self.uri = event["uri"]
                if kind == "loading":
                    self.restart = True
            elif kind == "seeked":
                self.restart = True
        if self.remote_state in ("playing", "paused"):
            if (self.restart or not self.active) and self.metadata.get("uri") == self.uri:
                self._play()
            elif self.active and self.owns_player():
                paused = bool(xbmc.getCondVisibility("Player.Paused"))
                if paused != (self.remote_state == "paused"):
                    self.player.pause()
        elif self.remote_state in ("stopped", "unavailable") and self.active:
            self.active = False
            self.switching = False
            if self.owns_player():
                self.player.stop()

    def _play(self):
        self.restart = False
        self.active = True
        self.switching = True
        url = "http://127.0.0.1:52309" + self.stream_path()
        item = xbmcgui.ListItem(label=self.metadata.get("name") or "Spotify Connect", path=url)
        item.setMimeType("audio/wav")
        item.setContentLookup(False)
        tag = item.getMusicInfoTag()
        tag.setTitle(self.metadata.get("name") or "Spotify Connect")
        tag.setDuration(int(self.metadata.get("duration_ms", 0) / 1000))
        if self.metadata.get("cover"):
            item.setArt({"thumb": self.metadata["cover"]})
        self.pending_item = item
        # PAPlayer treats a WAV URL as a seekable song and probes/reopens it.
        # Use Kodi's streaming demuxer for this non-seekable live source.
        response = json.loads(xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "Player.Open",
            "params": {"item": {"file": url}, "options": {"playername": "VideoPlayer"}},
        })))
        if "error" in response:
            raise RuntimeError("Kodi could not open Connect stream: " + str(response["error"]))
        xbmc.log("Resonance Connect: started Kodi live audio generation=" + str(self.audio.generation), xbmc.LOGINFO)

    def kodi_started(self):
        if self.owns_player():
            self.switching = False
            if self.pending_item is not None:
                try:
                    self.player.updateInfoTag(self.pending_item)
                except RuntimeError:
                    pass
            if self.remote_state == "paused":
                self.player.pause()
        elif self.active:
            self.active = False
            self.remote_state = "stopped"
            self.command("disconnect")
            self.audio.reset()

    def kodi_stopped(self):
        if self.active and not self.switching:
            self.active = False
            self.remote_state = "stopped"
            self.command("disconnect")
            self.audio.reset()

    def kodi_paused(self):
        if self.active and self.remote_state != "paused":
            self.remote_state = "paused"
            self.command("pause")

    def kodi_resumed(self):
        if self.active and self.remote_state != "playing":
            self.remote_state = "playing"
            self.command("play")

    def stop(self):
        self.enabled = False
        self.active = False
        self.audio.reset(closed=True)
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except Exception:
                    process.kill()
                    process.wait(timeout=3)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
        if self.reader is not None:
            self.reader.join(timeout=3)
