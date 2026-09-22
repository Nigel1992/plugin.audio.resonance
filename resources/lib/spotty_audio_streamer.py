import json
import os
import queue
import struct
import subprocess
import threading
import time
from collections import deque
from io import BytesIO
from typing import Callable, Tuple

from xbmc import LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR

from spotty import Spotty
from utils import ADDON_DATA_PATH, bytes_to_megabytes, kill_process_by_pid, log_msg, log_exception

SPOTIFY_TRACK_PREFIX = "spotify:track:"
# Keep local HTTP writes small enough that Kodi can consume each block before
# a temporary decoder/player pause creates socket backpressure.
SPOTTY_AUDIO_CHUNK_SIZE = 64 * 1024

SPOTIFY_BITRATE_DEFAULT = "320"
SPOTTY_INITIAL_VOLUME_DEFAULT = "100"
SPOTTY_GAIN_TYPE_DEFAULT = "track"
SPOTTY_PCM_BYTES_PER_SECOND = 44100 * 2 * 2
PREFETCH_METRICS_FILE = os.path.join(ADDON_DATA_PATH, "prefetch-metrics.json")
PREFETCH_METRICS_SAVE_INTERVAL_SECONDS = 15.0
SPOTTY_FIRST_AUDIO_TIMEOUT_SECONDS = 8.0
SPOTTY_FIRST_AUDIO_RETRIES = 1


class SpottyAudioStreamer:
    def __init__(self, spotty: Spotty):
        self.__spotty = spotty

        self.__track_id: str = ""
        self.__track_duration: float = 0.0
        self.__wav_header: bytes = bytes()
        self.__track_length: int = 0
        self.__last_log_bytes_sent: int = 0

        self.__notify_track_finished: Callable[[str], None] = lambda x: None
        self.__last_spotty_pid = -1
        self.__active_process = None
        self.__active_process_token = 0
        self.__active_process_lock = threading.Lock()
        self.__terminated = False
        self.__prefetch_lock = threading.Lock()
        self.__prefetch_generation = 0
        self.__prefetch_track_id = ""
        self.__prefetch_process = None
        self.__prefetch_audio = bytes()
        self.__prefetch_started_at = 0.0
        self.__prefetch_signature = None
        self.__metrics_lock = threading.Lock()
        self.__latency_ema = 1.0
        self.__latency_samples = 0
        self.__prefetch_hits = 0
        self.__prefetch_failures = 0
        self.__prefetch_cancellations = 0
        self.__normal_fallbacks = 0
        self.__adaptive_tier = 5
        self.__tier_candidate = 5
        self.__tier_candidate_count = 0
        self.__metrics_dirty = False
        self.__last_metrics_save_at = 0.0
        self.__load_metrics()

        self.use_normalization = True
        self.audio_quality = SPOTIFY_BITRATE_DEFAULT
        self.initial_volume = SPOTTY_INITIAL_VOLUME_DEFAULT
        self.normalization_type = SPOTTY_GAIN_TYPE_DEFAULT

    def configure_playback(
        self,
        audio_quality: str,
        initial_volume: int,
        normalization_type: str,
    ) -> None:
        """Apply validated playback settings to the next Spotty process."""
        quality = str(audio_quality)
        self.audio_quality = quality if quality in ("96", "160", "320") else "320"

        try:
            volume = int(initial_volume)
        except (TypeError, ValueError):
            volume = 100
        self.initial_volume = str(min(100, max(0, volume)))

        gain_type = str(normalization_type)
        self.normalization_type = (
            gain_type if gain_type in ("track", "album", "auto") else "track"
        )

    def get_track_length(self) -> int:
        return self.__track_length

    def get_track_duration(self) -> float:
        return self.__track_duration

    def set_track(self, track_id: str, track_duration: float) -> None:
        self.__track_id = track_id
        self.__track_duration = max(0.001, float(track_duration))
        self.__last_log_bytes_sent = 0
        self.__wav_header, self.__track_length = self.__create_wav_header()
        self.__start_offset_bytes = 0

    def set_resume_offset_ms(self, position_ms: int) -> None:
        try:
            self.__start_offset_bytes = max(0, int(position_ms)) * SPOTTY_PCM_BYTES_PER_SECOND // 1000
        except Exception:
            self.__start_offset_bytes = 0

    def send_resume_handshake_stream(
        self,
        range_len: int,
        cancel_event: threading.Event,
        max_seconds: float = 8.0,
    ):
        """Open Kodi's WAV decoder with silence before its resume Range.

        Kodi first requests bytes=0 even when Player.Open contains a resume
        time. Passing decoded Spotify audio through that provisional request
        can make a fraction of the track start audible before Kodi replaces it
        with the authoritative byte range. A short real-time silent stream is
        enough for AVStarted; the service seek then opens the real range and
        cancels this handshake without exposing any track audio at 0:00.
        """
        self.__terminated = False
        remaining = max(0, int(range_len))
        if remaining <= 0:
            return
        header = self.__wav_header[:remaining]
        if header:
            remaining -= len(header)
            yield header
        chunk_size = max(4, (SPOTTY_PCM_BYTES_PER_SECOND // 50) // 4 * 4)
        deadline = time.monotonic() + max(0.5, float(max_seconds))
        sent = len(header)
        log_msg(
            f"RESUME_HANDOFF_DIAG silent_handshake_started track={self.__track_id}",
            LOGINFO,
        )
        while (
            remaining > 0
            and not cancel_event.is_set()
            and not self.__terminated
            and time.monotonic() < deadline
        ):
            size = min(chunk_size, remaining)
            yield bytes(size)
            sent += size
            remaining -= size
            time.sleep(size / SPOTTY_PCM_BYTES_PER_SECOND)
        log_msg(
            "RESUME_HANDOFF_DIAG silent_handshake_finished "
            f"track={self.__track_id} bytes={sent} "
            f"cancelled={str(cancel_event.is_set()).lower()}",
            LOGINFO,
        )

    def set_notify_track_finished(self, func: Callable[[str], None]) -> None:
        self.__notify_track_finished = func

    def terminate_stream(
        self,
        preserve_prefetch_track_id: str = "",
        preserve_any_prefetch: bool = False,
    ) -> bool:
        self.__terminated = True
        if not preserve_any_prefetch:
            self.cancel_prefetch(preserve_prefetch_track_id)
        return self.__terminate_active_process()

    def __playback_signature(self):
        return (
            self.audio_quality,
            self.initial_volume,
            bool(self.use_normalization),
            self.normalization_type,
        )

    def __spotty_args(self, track_id: str, start_position_seconds: float = 0.0):
        args = [
            "--bitrate", self.audio_quality,
            "--initial-volume", self.initial_volume,
        ]
        if self.use_normalization:
            args += [
                "--enable-volume-normalisation",
                "--normalisation-gain-type",
                self.normalization_type,
            ]
        # Music tracks historically arrive as bare IDs. Podcast episodes are
        # passed as full spotify:episode: URIs by the dedicated podcast path.
        spotify_uri = track_id if str(track_id).startswith("spotify:") else SPOTIFY_TRACK_PREFIX + track_id
        args += ["--single-track", spotify_uri]
        if start_position_seconds > 0:
            args += ["--start-position", f"{start_position_seconds:.3f}"]
        return args

    @staticmethod
    def __terminate_process(process) -> None:
        if not process:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                kill_process_by_pid(process.pid)
        except Exception:
            try:
                kill_process_by_pid(process.pid)
            except Exception:
                pass

    @staticmethod
    def __start_stderr_drain(process) -> None:
        """Continuously drain Spotty stderr so Windows pipes cannot deadlock."""
        if not process or process.stderr is None:
            return
        tail = deque(maxlen=12)
        try:
            process._resonance_stderr_tail = tail
        except Exception:
            pass

        def drain() -> None:
            try:
                while True:
                    line = process.stderr.readline()
                    if not line:
                        break
                    if isinstance(line, bytes):
                        value = line.decode("utf-8", errors="replace").strip()
                    else:
                        value = str(line).strip()
                    if value:
                        tail.append(value)
            except Exception:
                return

        threading.Thread(
            target=drain,
            daemon=True,
            name=f"ResonanceSpottyStderr-{getattr(process, 'pid', 0)}",
        ).start()

    @staticmethod
    def __stderr_summary(process) -> str:
        try:
            tail = list(getattr(process, "_resonance_stderr_tail", ()) or ())
        except Exception:
            tail = []
        if not tail:
            return "-"
        return str(tail[-1]).replace("\r", " ").replace("\n", " ")[:300]

    def __claim_active_process(self, process) -> int:
        """Make one Spotty process authoritative for the current HTTP stream."""
        previous = None
        with self.__active_process_lock:
            previous = self.__active_process
            self.__active_process_token += 1
            token = self.__active_process_token
            self.__active_process = process
            self.__last_spotty_pid = int(getattr(process, "pid", -1) or -1)
        if previous is not None and previous is not process:
            self.__terminate_process(previous)
        return token

    def __release_active_process(self, process, token: int) -> None:
        """Release only the generation owned by this response generator."""
        with self.__active_process_lock:
            if (
                self.__active_process is process
                and self.__active_process_token == token
            ):
                self.__active_process = None
                self.__last_spotty_pid = -1
        self.__terminate_process(process)

    def __owns_active_process(self, process, token: int) -> bool:
        with self.__active_process_lock:
            return bool(
                self.__active_process is process
                and self.__active_process_token == token
            )

    def __terminate_active_process(self) -> bool:
        with self.__active_process_lock:
            process = self.__active_process
            self.__active_process = None
            self.__active_process_token += 1
            self.__last_spotty_pid = -1
        if process is None:
            return False
        self.__terminate_process(process)
        return True

    @staticmethod
    def __read_first_frame(process, timeout_seconds: float):
        """Return the first PCM block without blocking a Kodi HTTP worker forever."""
        result = queue.Queue(maxsize=1)

        def read() -> None:
            try:
                result.put((process.stdout.read(SPOTTY_AUDIO_CHUNK_SIZE), None))
            except Exception as exc:
                result.put((b"", exc))

        threading.Thread(
            target=read,
            daemon=True,
            name=f"ResonanceFirstAudio-{getattr(process, 'pid', 0)}",
        ).start()
        try:
            return result.get(timeout=max(0.5, float(timeout_seconds)))
        except queue.Empty:
            return None, None

    def cancel_prefetch(self, preserve_track_id: str = "") -> None:
        process = None
        with self.__prefetch_lock:
            if preserve_track_id and self.__prefetch_track_id == preserve_track_id:
                return
            self.__prefetch_generation += 1
            process = self.__prefetch_process
            cancelled_track = self.__prefetch_track_id
            self.__prefetch_track_id = ""
            self.__prefetch_process = None
            self.__prefetch_audio = bytes()
            self.__prefetch_started_at = 0.0
            self.__prefetch_signature = None
        if process:
            with self.__metrics_lock:
                self.__prefetch_cancellations += 1
                self.__save_metrics_locked()
            self.__terminate_process(process)
            log_msg(
                f"PREFETCH_DIAG cancelled track={cancelled_track}",
                LOGINFO,
            )

    def prefetch_track(
        self,
        track_id: str,
        track_duration: float,
        buffer_seconds: int = 10,
    ) -> None:
        """Start and buffer one future track without touching current output."""
        self.cancel_prefetch(track_id)
        with self.__prefetch_lock:
            if self.__prefetch_track_id == track_id and self.__prefetch_process:
                return
            self.__prefetch_generation += 1
            generation = self.__prefetch_generation
            self.__prefetch_track_id = track_id
            self.__prefetch_signature = self.__playback_signature()
        process = None
        started = time.perf_counter()
        try:
            process = self.__spotty.run_spotty(
                self.__spotty_args(track_id),
                use_audio_backend=False,
            )
            self.__start_stderr_drain(process)
            with self.__prefetch_lock:
                if generation != self.__prefetch_generation:
                    self.__terminate_process(process)
                    return
                self.__prefetch_process = process
                self.__prefetch_started_at = started
            chunks = []
            buffered = 0
            safe_buffer_seconds = min(15, max(5, int(buffer_seconds)))
            target_bytes = SPOTTY_PCM_BYTES_PER_SECOND * safe_buffer_seconds
            first_frame, first_error = self.__read_first_frame(
                process, SPOTTY_FIRST_AUDIO_TIMEOUT_SECONDS
            )
            if not first_frame:
                reason = (
                    "timeout" if first_frame is None
                    else type(first_error).__name__ if first_error
                    else "premature_eof"
                )
                raise RuntimeError(f"prefetch_first_audio_{reason}")
            first_frame = first_frame[:target_bytes]
            chunks.append(first_frame)
            buffered += len(first_frame)
            while buffered < target_bytes:
                frame = process.stdout.read(
                    min(SPOTTY_AUDIO_CHUNK_SIZE, target_bytes - buffered)
                )
                if not frame:
                    break
                chunks.append(frame)
                buffered += len(frame)
            with self.__prefetch_lock:
                if generation != self.__prefetch_generation:
                    self.__terminate_process(process)
                    return
                self.__prefetch_audio = b"".join(chunks)
            log_msg(
                f"PREFETCH_DIAG ready track={track_id} duration={track_duration:.3f}s "
                f"bytes={buffered} audio_seconds="
                f"{buffered / SPOTTY_PCM_BYTES_PER_SECOND:.3f}s "
                f"elapsed={time.perf_counter() - started:.3f}s pid={process.pid}",
                LOGINFO,
            )
            self.__record_latency(time.perf_counter() - started)
        except Exception as exc:
            with self.__metrics_lock:
                self.__prefetch_failures += 1
                self.__save_metrics_locked()
            self.cancel_prefetch()
            if process:
                self.__terminate_process(process)
            log_msg(
                f"PREFETCH_DIAG failed track={track_id} error={type(exc).__name__}",
                LOGWARNING,
            )

    def __take_prefetch(self, track_id: str):
        with self.__prefetch_lock:
            if (
                self.__prefetch_track_id != track_id
                or not self.__prefetch_process
                or not self.__prefetch_audio
                or self.__prefetch_signature != self.__playback_signature()
            ):
                return None
            result = (
                self.__prefetch_process,
                self.__prefetch_audio,
                self.__prefetch_started_at,
            )
            self.__prefetch_generation += 1
            self.__prefetch_track_id = ""
            self.__prefetch_process = None
            self.__prefetch_audio = bytes()
            self.__prefetch_started_at = 0.0
            self.__prefetch_signature = None
            return result

    def __record_latency(self, latency: float) -> None:
        with self.__metrics_lock:
            self.__latency_ema = (
                latency if self.__latency_samples == 0
                else (0.75 * self.__latency_ema) + (0.25 * latency)
            )
            self.__latency_samples += 1
            requested = 5 if self.__latency_ema < 1.75 else (10 if self.__latency_ema < 3.5 else 15)
            if requested == self.__adaptive_tier:
                self.__tier_candidate = requested
                self.__tier_candidate_count = 0
            elif requested == self.__tier_candidate:
                self.__tier_candidate_count += 1
                if self.__tier_candidate_count >= 3:
                    old_tier = self.__adaptive_tier
                    self.__adaptive_tier = requested
                    self.__tier_candidate_count = 0
                    log_msg(
                        f"PREFETCH_DIAG adaptive_tier_changed old={old_tier}s "
                        f"new={requested}s latency_ema={self.__latency_ema:.3f}s",
                        LOGINFO,
                    )
            else:
                self.__tier_candidate = requested
                self.__tier_candidate_count = 1
            self.__save_metrics_locked()

    def get_adaptive_lead_seconds(self) -> int:
        """Return a conservative bounded tier from smoothed readiness latency."""
        with self.__metrics_lock:
            latency = self.__latency_ema
            samples = self.__latency_samples
        return 5 if samples < 3 else self.__adaptive_tier

    def __load_metrics(self) -> None:
        try:
            with open(PREFETCH_METRICS_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.__latency_ema = min(30.0, max(0.0, float(data.get("latency_ema", 1.0))))
            self.__latency_samples = min(10000, max(0, int(data.get("samples", 0))))
            self.__prefetch_hits = max(0, int(data.get("hits", 0)))
            self.__prefetch_failures = max(0, int(data.get("failures", 0)))
            self.__prefetch_cancellations = max(0, int(data.get("cancellations", 0)))
            self.__normal_fallbacks = max(0, int(data.get("fallbacks", 0)))
            tier = int(data.get("adaptive_tier", 5))
            self.__adaptive_tier = tier if tier in (5, 10, 15) else 5
            self.__tier_candidate = self.__adaptive_tier
            log_msg(
                f"PREFETCH_DIAG metrics_restored samples={self.__latency_samples} "
                f"lead={self.__adaptive_tier}s",
                LOGINFO,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def __save_metrics_locked(self, force: bool = False) -> None:
        self.__metrics_dirty = True
        now = time.monotonic()
        if (
            not force
            and self.__last_metrics_save_at
            and now - self.__last_metrics_save_at
            < PREFETCH_METRICS_SAVE_INTERVAL_SECONDS
        ):
            return
        temporary = (
            f"{PREFETCH_METRICS_FILE}.{os.getpid()}."
            f"{threading.get_ident()}.tmp"
        )
        try:
            os.makedirs(ADDON_DATA_PATH, exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({
                    "latency_ema": self.__latency_ema,
                    "samples": self.__latency_samples,
                    "hits": self.__prefetch_hits,
                    "failures": self.__prefetch_failures,
                    "cancellations": self.__prefetch_cancellations,
                    "fallbacks": self.__normal_fallbacks,
                    "adaptive_tier": self.__adaptive_tier,
                }, handle, separators=(",", ":"))
            os.replace(temporary, PREFETCH_METRICS_FILE)
            self.__metrics_dirty = False
            self.__last_metrics_save_at = now
        except OSError as exc:
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass
            log_msg(f"PREFETCH_DIAG metrics_save_failed error={type(exc).__name__}", LOGWARNING)

    def flush_metrics(self) -> None:
        """Persist debounced counters during a clean service shutdown."""
        with self.__metrics_lock:
            if self.__metrics_dirty:
                self.__save_metrics_locked(force=True)

    def get_prefetch_diagnostics(self) -> dict:
        with self.__metrics_lock:
            hits = self.__prefetch_hits
            failures = self.__prefetch_failures
            cancellations = self.__prefetch_cancellations
            fallbacks = self.__normal_fallbacks
            latency = self.__latency_ema
            samples = self.__latency_samples
        opportunities = hits + fallbacks
        return {
            "hits": hits,
            "failures": failures,
            "cancellations": cancellations,
            "fallbacks": fallbacks,
            "hit_rate": (100.0 * hits / opportunities) if opportunities else 0.0,
            "latency_ema": latency,
            "samples": samples,
        }

    def send_part_audio_stream(
        self,
        range_len: int,
        range_begin: int,
        preserve_prefetch: bool = False,
    ) -> str:
        """Chunked transfer of audio data from spotty binary"""

        self.__terminated = False
        stream_track_id = self.__track_id
        stream_wav_header = self.__wav_header
        spotty_process = None
        process_token = 0
        bytes_sent = 0
        transfer_started = time.perf_counter()
        process_started = None
        first_audio_logged = False
        range_seek_bytes = max(0, int(range_begin) - 44)
        configured_seek_bytes = max(
            0,
            int(getattr(self, "_SpottyAudioStreamer__start_offset_bytes", 0)),
        )
        # A Kodi byte range is authoritative. The configured offset is used
        # only for the initial bytes=0 request; adding both would seek twice.
        native_seek_bytes = range_seek_bytes or configured_seek_bytes
        native_start_seconds = native_seek_bytes / SPOTTY_PCM_BYTES_PER_SECOND
        try:
            self.__log_start_transfer(range_begin)
            track_id_uri = (
                stream_track_id if str(stream_track_id).startswith("spotify:")
                else SPOTIFY_TRACK_PREFIX + stream_track_id
            )
            self.__log_start_reading_audio(track_id_uri)
            # Execute the spotty process, then collect stdout.
            audio_quality = self.audio_quality
            initial_volume = self.initial_volume
            normalization_type = self.normalization_type
            log_msg(
                f"Playback started: track={stream_track_id}, "
                f"bitrate={audio_quality} kbps, initial_volume={initial_volume}%, "
                f"normalization={self.use_normalization}, "
                f"normalization_type={normalization_type}.",
                LOGINFO,
            )

            prefetched = (
                self.__take_prefetch(stream_track_id)
                if range_begin == 0 and native_seek_bytes == 0
                else None
            )
            if prefetched:
                with self.__metrics_lock:
                    self.__prefetch_hits += 1
                    self.__save_metrics_locked()
                spotty_process, prefetched_audio, process_started = prefetched
                process_token = self.__claim_active_process(spotty_process)
                log_msg(
                    f"PREFETCH_DIAG hit track={stream_track_id} "
                    f"bytes={len(prefetched_audio)} pid={spotty_process.pid}",
                    LOGINFO,
                )
                if range_begin == 0:
                    bytes_sent = len(stream_wav_header)
                    self.__log_send_wav_header()
                    yield stream_wav_header
                first_audio_logged = True
                for offset in range(0, len(prefetched_audio), SPOTTY_AUDIO_CHUNK_SIZE):
                    remaining = range_len - bytes_sent
                    if remaining <= 0:
                        break
                    chunk = prefetched_audio[
                        offset:offset + min(SPOTTY_AUDIO_CHUNK_SIZE, remaining)
                    ]
                    bytes_sent += len(chunk)
                    yield chunk
            else:
                with self.__metrics_lock:
                    self.__normal_fallbacks += 1
                    self.__save_metrics_locked()
                # A same-track Range request replaces only the active decoder.
                # Keep the already prepared successor available for Next.
                if not preserve_prefetch:
                    self.cancel_prefetch()
                first_frame = b""
                for start_attempt in range(SPOTTY_FIRST_AUDIO_RETRIES + 1):
                    process_launch_started = time.perf_counter()
                    spotty_process = self.__spotty.run_spotty(
                        self.__spotty_args(stream_track_id, native_start_seconds),
                        use_audio_backend=False,
                    )
                    self.__start_stderr_drain(spotty_process)
                    process_started = time.perf_counter()
                    self.__log_spotty_return_code(spotty_process)
                    process_token = self.__claim_active_process(spotty_process)
                    log_msg(
                        f"PLAYBACK_DIAG spotty_launch track={stream_track_id} "
                        f"native_start={native_start_seconds:.3f}s "
                        f"attempt={start_attempt + 1} "
                        f"elapsed={process_started - process_launch_started:.3f}s "
                        f"pid={spotty_process.pid}",
                        LOGINFO,
                    )
                    first_frame, first_error = self.__read_first_frame(
                        spotty_process, SPOTTY_FIRST_AUDIO_TIMEOUT_SECONDS
                    )
                    if first_frame:
                        break
                    reason = (
                        "timeout" if first_frame is None
                        else type(first_error).__name__ if first_error
                        else "premature_eof"
                    )
                    log_msg(
                        "PLAYBACK_DIAG first_audio_retry "
                        f"track={stream_track_id} attempt={start_attempt + 1} "
                        f"reason={reason} pid={spotty_process.pid} "
                        f"stderr={self.__stderr_summary(spotty_process)}",
                        LOGWARNING,
                    )
                    self.__release_active_process(spotty_process, process_token)
                    spotty_process = None
                    process_token = 0
                    if self.__terminated:
                        return
                if not first_frame or spotty_process is None:
                    log_msg(
                        "PLAYBACK_DIAG first_audio_failed "
                        f"track={stream_track_id} "
                        f"attempts={SPOTTY_FIRST_AUDIO_RETRIES + 1}",
                        LOGERROR,
                    )
                    return

                first_audio_logged = True
                first_audio_at = time.perf_counter()
                log_msg(
                    f"PLAYBACK_DIAG first_audio track={stream_track_id} "
                    f"after_process={first_audio_at - process_started:.3f}s "
                    f"after_transfer={first_audio_at - transfer_started:.3f}s "
                    f"bytes={len(first_frame)}",
                    LOGINFO,
                )
                self.__record_latency(first_audio_at - process_started)
                # Do not expose a header-only response to Kodi.  The header
                # and first PCM block become visible only after Spotty is
                # demonstrably ready, so a bounded retry stays in one request.
                if range_begin == 0:
                    bytes_sent = len(stream_wav_header)
                    self.__log_send_wav_header()
                    yield stream_wav_header
                remaining = range_len - bytes_sent
                if remaining > 0:
                    first_frame = first_frame[:remaining]
                    bytes_sent += len(first_frame)
                    yield first_frame
            if native_seek_bytes:
                log_msg(
                    "SEEK_DIAG native_spotty_seek "
                    f"track={stream_track_id} bytes={native_seek_bytes} "
                    f"position={native_start_seconds:.3f}s",
                    LOGINFO,
                )

            # Loop as long as there's something to output.  Some episode
            # streams can return a tiny first PCM fragment and exit cleanly on
            # the first Spotty process.  Kodi interprets that as end-of-file and
            # advances to the next native directory item.  Retry only podcast
            # episodes and only for an obviously premature EOF; music tracks
            # retain their existing, already verified behavior.
            episode_retry_count = 0
            max_episode_retries = 2
            premature_episode_bytes = 44 + (SPOTTY_PCM_BYTES_PER_SECOND * 2)
            while bytes_sent < range_len:
                if self.__terminated:
                    return

                frame = spotty_process.stdout.read(SPOTTY_AUDIO_CHUNK_SIZE)
                if self.__terminated:
                    return
                if not frame:
                    if not self.__owns_active_process(spotty_process, process_token):
                        log_msg(
                            "PLAYBACK_DIAG replaced_stream_closed "
                            f"track={stream_track_id} pid={spotty_process.pid}",
                            LOGINFO,
                        )
                        break
                    returncode = spotty_process.poll()
                    if returncode is None:
                        try:
                            returncode = spotty_process.wait(timeout=0.25)
                        except subprocess.TimeoutExpired:
                            pass
                    is_episode = str(stream_track_id).startswith("spotify:episode:")
                    if (is_episode and episode_retry_count < max_episode_retries
                            and bytes_sent < premature_episode_bytes):
                        episode_retry_count += 1
                        log_msg(
                            "PODCAST_DIAG premature_eof_retry "
                            f"track={stream_track_id} attempt={episode_retry_count} "
                            f"bytes={bytes_sent} returncode={returncode}",
                            LOGWARNING,
                        )
                        self.__release_active_process(spotty_process, process_token)
                        process_launch_started = time.perf_counter()
                        spotty_process = self.__spotty.run_spotty(
                            self.__spotty_args(stream_track_id, native_start_seconds),
                            use_audio_backend=False,
                        )
                        self.__start_stderr_drain(spotty_process)
                        process_started = time.perf_counter()
                        self.__log_spotty_return_code(spotty_process)
                        process_token = self.__claim_active_process(spotty_process)
                        log_msg(
                            "PODCAST_DIAG retry_launch "
                            f"track={stream_track_id} attempt={episode_retry_count} "
                            f"elapsed={process_started - process_launch_started:.3f}s "
                            f"pid={spotty_process.pid}",
                            LOGINFO,
                        )
                        continue

                    remaining = range_len - bytes_sent
                    if (
                        returncode in (0, None)
                        and 0 < remaining <= SPOTTY_PCM_BYTES_PER_SECOND
                    ):
                        # Spotify's duration metadata can be a few PCM frames
                        # longer than Spotty's decoded output. Fulfil only a
                        # small clean-EOF deficit so Kodi does not retry the
                        # same near-EOF byte range as a failed transfer.
                        log_msg(
                            "PLAYBACK_DIAG clean_eof_padding "
                            f"track={stream_track_id} bytes={remaining} "
                            f"range_begin={range_begin}",
                            LOGINFO,
                        )
                        while remaining:
                            padding_size = min(SPOTTY_AUDIO_CHUNK_SIZE, remaining)
                            padding = bytes(padding_size)
                            bytes_sent += padding_size
                            remaining -= padding_size
                            yield padding

                    if returncode not in (0, None):
                        stderr_output = self.__stderr_summary(spotty_process)
                        if stderr_output != "-":
                            log_msg(f"Spotty stderr: {stderr_output}", LOGERROR)
                        if returncode == -9:
                            log_msg(
                                "Spotty stdout closed after stream replacement/stop. returncode=-9",
                                LOGINFO,
                            )
                        else:
                            log_msg(
                                f"Spotty stdout closed unexpectedly. returncode={returncode}",
                                LOGERROR,
                            )
                    else:
                        log_msg("Spotty stdout closed normally.", LOGDEBUG)
                    break

                if not first_audio_logged:
                    first_audio_logged = True
                    first_audio_at = time.perf_counter()
                    log_msg(
                        f"PLAYBACK_DIAG first_audio track={stream_track_id} "
                        f"after_process={first_audio_at - process_started:.3f}s "
                        f"after_transfer={first_audio_at - transfer_started:.3f}s "
                        f"bytes={len(frame)}",
                        LOGINFO,
                    )
                    self.__record_latency(first_audio_at - process_started)

                bytes_sent += len(frame)
                self.__log_continue_sending(bytes_sent)
                yield frame

            # A complete byte range is a natural EOF. Resume seeks replace the
            # old Spotty process and close stdout with -9; that technical close
            # must never advance an audiobook to the following chapter.
            natural_eof = bytes_sent >= range_len and not self.__terminated
            if natural_eof:
                self.__notify_track_finished(stream_track_id)
            else:
                log_msg(
                    "PLAYBACK_DIAG transfer_interrupted_no_eof "
                    f"track={stream_track_id} range_begin={range_begin} "
                    f"bytes={bytes_sent} expected={range_len} "
                    f"terminated={str(self.__terminated).lower()}",
                    LOGINFO,
                )
            self.__log_finished_sending(range_begin, bytes_sent)
            log_msg(f"Playback finished: track={stream_track_id}.", LOGINFO)
            log_msg(
                f"PLAYBACK_DIAG transfer_finished track={stream_track_id} "
                f"elapsed={time.perf_counter() - transfer_started:.3f}s "
                f"bytes={bytes_sent}",
                LOGINFO,
            )

        except Exception as ex:
            self.__log_exception_sending(ex, range_begin, bytes_sent)
        finally:
            if spotty_process:
                self.__release_active_process(spotty_process, process_token)


    def __kill_last_spotty(self) -> None:
        self.__terminate_active_process()


    def __log_start_transfer(self, range_begin: int) -> None:
        log_msg(
            f"Start transfer for track '{self.__track_id}' - range begin: {range_begin}",
            LOGDEBUG,
        )
        log_msg(f"Use Spotify normalization: {self.use_normalization}.", LOGDEBUG)

    def __log_send_wav_header(self) -> None:
        log_msg(
            f"Sending wav header for track '{self.__track_id}'.",
            LOGDEBUG,
        )

    def __log_start_reading_audio(self, track_id_uri: str) -> None:
        log_msg(
            f"Start reading audio data for track: '{track_id_uri}',"
            f" length = {self.__track_length} ({self.__get_mb_str(self.__track_length)}).",
            LOGDEBUG,
        )

    def __log_continue_sending(self, bytes_sent: int) -> None:
        if bytes_sent - self.__last_log_bytes_sent >= (10 * 1024 * 1024):
            self.__last_log_bytes_sent = bytes_sent

            log_msg(
                f"Sending track '{self.__track_id}'"
                f" - {self.__get_data_sent_str(bytes_sent, self.__track_length)}.",
                LOGDEBUG,
            )

    def __log_finished_sending(self, range_begin: int, bytes_sent: int) -> None:
        log_msg(
            f"Finished sending track '{self.__track_id}'"
            f" - range begin {range_begin}"
            f" - range end {bytes_sent} - {self.__get_mb_str(bytes_sent)}.",
            LOGDEBUG,
        )

    def __log_exception_sending(
        self,
        ex: Exception,
        range_begin: int,
        bytes_sent: int
    ) -> None:

        log_msg(
            f"EXCEPTION sending track '{self.__track_id}'"
            f" - range begin {range_begin}"
            f" - range end {bytes_sent} - {self.__get_mb_str(bytes_sent)}.",
            LOGERROR,
        )

        try:
            error_text = str(ex)
        except Exception:
            error_text = repr(ex)

        log_msg(
            f"Exception: {error_text}",
            LOGERROR,
        )

    @staticmethod
    def __log_spotty_return_code(spotty_process: subprocess.Popen) -> None:
        if spotty_process.returncode:
            log_msg(
                f"Spotty process return code: {spotty_process.returncode}",
                LOGWARNING,
            )

    @staticmethod
    def __get_mb_str(data_bytes: int) -> str:
        data_mb = bytes_to_megabytes(data_bytes)
        return f"{data_mb:.1f}MB"

    @staticmethod
    def __get_data_sent_str(data_bytes: int, track_length: int) -> str:
        data_mb = bytes_to_megabytes(data_bytes)
        percent = int(100.0 * float(data_bytes) / float(track_length))
        return f"sent so far: {data_mb:>5.1f}MB ({percent:>3}%)"

    def __create_wav_header(self) -> Tuple[bytes, int]:
        """generate a wav header for the stream"""
        try:
            log_msg(f"Start getting wav header. Duration = {self.__track_duration}", LOGDEBUG)
            file = BytesIO()
            num_samples = max(1, int(round(44100 * self.__track_duration)))
            channels = 2
            sample_rate = 44100
            bits_per_sample = 16

            # Generate format chunk.
            format_chunk_spec = "<4sLHHLLHH"
            format_chunk = struct.pack(
                format_chunk_spec,
                "fmt ".encode(encoding="UTF-8"),  # Chunk id
                16,  # Size of this chunk (excluding chunk id and this field)
                1,  # Audio format, 1 for PCM
                channels,  # Number of channels
                sample_rate,  # Samplerate, 44100, 48000, etc.
                sample_rate * channels * (bits_per_sample // 8),  # Byterate
                channels * (bits_per_sample // 8),  # Blockalign
                bits_per_sample,  # 16 bits for two byte samples, etc.
            )

            # Generate data chunk.
            data_chunk_spec = "<4sL"
            data_size = int(num_samples * channels * (bits_per_sample // 8))
            data_chunk = struct.pack(
                data_chunk_spec,
                "data".encode(encoding="UTF-8"),  # Chunk id
                int(data_size),  # Chunk size (excluding chunk id and this field)
            )
            sum_items = [
                # "WAVE" string following size field
                4,
                # "fmt " + chunk size field + chunk size
                struct.calcsize(format_chunk_spec),
                # Size of data chunk spec + data size
                struct.calcsize(data_chunk_spec) + data_size,
            ]

            # Generate main header.
            all_chunks_size = int(sum(sum_items))
            main_header_spec = "<4sL4s"
            main_header = struct.pack(
                main_header_spec,
                "RIFF".encode(encoding="UTF-8"),
                all_chunks_size,
                "WAVE".encode(encoding="UTF-8"),
            )

            # Write all the contents in.
            file.write(main_header)
            file.write(format_chunk)
            file.write(data_chunk)

            return file.getvalue(), all_chunks_size + 8

        except Exception as exc:
            log_exception(exc, "Failed to create wave header.")
