# audio_recorder.py
# Continuous-stream audio capture with a pre-roll ring buffer. Key design choices:
#
#   • A single sd.InputStream runs continuously from app startup. We do NOT
#     open/close streams per recording — that was the source of "first word
#     clipped" bugs and added 100-200ms of warmup latency on every press.
#
#   • The buffer is a deque trimmed to ~500ms when idle. When the user presses
#     record, the buffer already contains the recent past, so anything they
#     started saying before the OS finished routing the hotkey is captured.
#
#   • WASAPI on Windows runs at native rate (typically 48 kHz); we resample to
#     Whisper's 16 kHz only at stop time. This avoids resampling per chunk during
#     a recording, which would burn CPU.
#
#   • The chosen device is remembered by name, not PortAudio index: indices
#     shift whenever a device is plugged in or pulled out.
#
#   • Hot-plug recovery: PortAudio only enumerates devices at startup, and on
#     macOS an unplugged device doesn't raise, its callbacks just stop. So the
#     stream is rebuilt (with a fresh PortAudio device list) when the OS reports
#     a device change, when callbacks stall, or when the stream errors. The
#     preferred device is used again as soon as it is back; until then the
#     system default input stands in. Silent-mic (peak amplitude near zero)
#     surfaces a console warning on stop.

import collections
import logging
import threading
import time
from typing import Optional, Callable

import numpy as np
import sounddevice as sd
import soxr

from .platform import audio_devices
from .voice_activity_detection import VadEvent, VAD_CHUNK_SIZE


# PortAudio builds its device list once, in Pa_Initialize. A re-plugged
# interface stays invisible (or keeps a dead CoreAudio ID) until PortAudio is
# re-initialized. Pa_Terminate closes every open stream, so only call this
# while no stream is open.
def refresh_portaudio_device_list():
    sd._terminate()
    sd._initialize()


class AudioRecorder:
    WHISPER_SAMPLE_RATE = 16000
    THREAD_JOIN_TIMEOUT = 2.0
    LOOP_SLEEP_MS = 100
    # No audio callback for this long means the device is gone (CoreAudio
    # stops calling back on unplug instead of raising).
    STALL_TIMEOUT_SECONDS = 1.5
    # Allowance for the first callback after opening; Bluetooth headsets can
    # take a couple of seconds to switch into microphone mode.
    STREAM_START_TIMEOUT_SECONDS = 5.0
    # Pause before reopening, so a just-plugged device can finish registering.
    # Doubles after each failed attempt, up to the max.
    REOPEN_DELAY_SECONDS = 0.5
    MAX_REOPEN_DELAY_SECONDS = 5.0
    STREAM_DTYPE = np.float32
    PREROLL_SECONDS = 0.5
    TRAILING_SILENCE_TRIM_SECONDS = 0.4
    SILENCE_RMS_THRESHOLD = 0.005
    LONG_PAUSE_SECONDS = 2.0
    LONG_PAUSE_REPLACEMENT_SECONDS = 0.4

    def __init__(self,
                 on_vad_event: Callable[[VadEvent], None],
                 channels: int = 1,
                 dtype: str = "float32",
                 max_duration: int = 30,
                 on_max_duration_reached: callable = None,
                 vad_manager=None,
                 streaming_manager=None,
                 on_streaming_result: Callable[[str, bool], None] = None,
                 device=None,
                 host: Optional[str] = None,
                 on_device_switched: Optional[Callable[[str], None]] = None,
                 noise_suppression_config: Optional[dict] = None):

        self.sample_rate = self.WHISPER_SAMPLE_RATE
        self.channels = channels
        self.dtype = dtype
        self.max_duration = max_duration
        self.on_max_duration_reached = on_max_duration_reached
        self.is_recording = False
        self.recording_start_time = None
        self.logger = logging.getLogger(__name__)
        self._noise_suppression_config = noise_suppression_config or {}

        self.vad_manager = vad_manager
        self.on_vad_event = on_vad_event
        self.continuous_vad = self._setup_continuous_vad_monitoring()

        self.streaming_manager = streaming_manager
        self.on_streaming_result = on_streaming_result
        self.on_device_switched = on_device_switched

        # The device the user picked, by name (+ host API, since Windows lists
        # the same mic once per host API). None means "follow system default".
        self._preferred_device_name, self._preferred_hostapi = self._preferred_device_from_setting(device, host)
        self._select_device()
        self._test_audio_source()

        self.continuous_streaming = self._setup_continuous_streaming()
        self._configure_stream_format()
        self._buffer = collections.deque()
        # Guards the ring buffer. The PortAudio callback appends/trims on its own
        # thread while stop/cancel/max-duration snapshot+clear from other threads;
        # list(deque) raises RuntimeError if the deque mutates mid-iteration, so
        # every buffer access is serialized through this lock. Held only for fast
        # ops (append, snapshot, clear), so it won't stall the audio callback.
        self._buffer_lock = threading.Lock()

        self._capture_running = False
        self._capture_thread = None
        self._stream_error = None
        self._current_level = 0.0
        self._last_callback_time = None
        # Set by the OS device-change listener or a manual device switch; the
        # capture loop rebuilds the stream when it sees it (after any recording).
        self._reopen_requested = threading.Event()
        self._unwatch_devices = lambda: None
        self._start_capture()

    def _setup_continuous_vad_monitoring(self):
        if self.vad_manager.is_available():
            return self.vad_manager.create_continuous_detector(event_callback=self._handle_vad_event)
        return None

    def _setup_continuous_streaming(self):
        if self.streaming_manager and self.streaming_manager.is_available():
            recognizer = self.streaming_manager.create_continuous_recognizer(
                result_callback=self._handle_streaming_result
            )
            recognizer.set_recording_rate(self._get_recording_sample_rate())
            return recognizer
        return None

    def _handle_streaming_result(self, text: str, is_final: bool):
        if self.on_streaming_result:
            self.on_streaming_result(text, is_final)

    # --- Device selection ------------------------------------------------------

    # Turn the input_device setting ("default", a device name, or a PortAudio
    # index from the setup wizard) into a (name, host API) pair.
    def _preferred_device_from_setting(self, device, host):
        if device == "default" or device is None:
            return None, None
        if isinstance(device, str):
            return device, host
        if isinstance(device, int):
            try:
                device_info = sd.query_devices(device)
                if device_info.get('max_input_channels', 0) > 0:
                    return device_info['name'], sd.query_hostapis(device_info['hostapi'])['name']
                self.logger.warning(f"Selected device {device} has no input channels; using default input instead")
            except Exception as e:
                self.logger.warning(f"Failed to load device {device}: {e}. Falling back to default input")
            return None, None
        self.logger.warning(f"Invalid device parameter: {device}, using default")
        return None, None

    # Point self.device at the preferred device if PortAudio lists it right now,
    # otherwise at the system default input (self.device = None).
    def _select_device(self):
        self.device = self._find_input_device(self._preferred_device_name, self._preferred_hostapi)
        if self._preferred_device_name and self.device is None:
            self.logger.warning(f"'{self._preferred_device_name}' is not connected; using the default input until it is back")
        try:
            self._resolve_hostapi(sd.query_devices(self.device, kind='input'))
        except Exception as e:
            self.logger.debug(f"Could not query input device: {e}")
            self._resolve_hostapi(None)

    @staticmethod
    def _find_input_device(name: Optional[str], hostapi: Optional[str]) -> Optional[int]:
        if not name:
            return None
        hostapis = sd.query_hostapis()
        for index, device_info in enumerate(sd.query_devices()):
            if device_info['name'] != name or device_info.get('max_input_channels', 0) <= 0:
                continue
            if hostapi and hostapis[device_info['hostapi']]['name'].lower() != hostapi.lower():
                continue
            return index
        return None

    # Switch to another input device (PortAudio index from the current list,
    # or None for the system default). The capture thread reopens the stream.
    def switch_device(self, device_index: Optional[int]):
        if device_index is None:
            self._preferred_device_name, self._preferred_hostapi = None, None
        else:
            device_info = sd.query_devices(device_index)
            self._preferred_device_name = device_info['name']
            self._preferred_hostapi = sd.query_hostapis(device_info['hostapi'])['name']
        self._reopen_requested.set()

    def get_device_name(self) -> str:
        try:
            return sd.query_devices(self.device, kind='input')['name']
        except Exception:
            return self._preferred_device_name or "default input"

    # What to store in the input_device setting for the current choice.
    def get_device_setting(self) -> str:
        return self._preferred_device_name or "default"

    def _resolve_hostapi(self, device_info):
        try:
            if device_info is None:
                device_info = sd.query_devices(kind='input')
            hostapi_index = device_info['hostapi']
            self.device_hostapi = sd.query_hostapis(hostapi_index)['name']
            self.device_native_rate = int(device_info['default_samplerate'])
        except Exception as e:
            self.logger.debug(f"Could not determine host API: {e}")
            self.device_hostapi = None
            self.device_native_rate = self.WHISPER_SAMPLE_RATE

    def _needs_resampling(self) -> bool:
        return self.device_hostapi and 'wasapi' in self.device_hostapi.lower()

    def _get_recording_sample_rate(self) -> int:
        if self._needs_resampling():
            return self.device_native_rate
        return self.WHISPER_SAMPLE_RATE

    # Derive rate, block size and pre-roll length from the selected device.
    # Re-run on every device change: WASAPI devices differ in native rate.
    def _configure_stream_format(self):
        self._recording_rate = self._get_recording_sample_rate()
        self._needs_resampling_cached = self._needs_resampling()
        if self._needs_resampling_cached:
            self._vad_blocksize = int(VAD_CHUNK_SIZE * self._recording_rate / self.WHISPER_SAMPLE_RATE)
        else:
            self._vad_blocksize = VAD_CHUNK_SIZE

        chunk_seconds = self._vad_blocksize / self._recording_rate
        self._preroll_max_chunks = max(1, int(self.PREROLL_SECONDS / chunk_seconds))
        if self.continuous_streaming:
            self.continuous_streaming.set_recording_rate(self._recording_rate)

    def _resample_audio(self, audio: np.ndarray, orig_rate: int, target_rate: int) -> np.ndarray:
        if orig_rate == target_rate or len(audio) == 0:
            return audio
        return soxr.resample(audio.flatten(), orig_rate, target_rate).astype(np.float32)

    def _handle_vad_event(self, event: VadEvent):
        self.on_vad_event(event)

    def _test_audio_source(self):
        try:
            if self.device is not None:
                device_info = sd.query_devices(self.device)
                self.logger.info(f"Using device: {device_info['name']}")
            else:
                default_input = sd.query_devices(kind='input')
                self.logger.info(f"Default source: {default_input['name']}")
        except Exception as e:
            self.logger.error(f"Audio source test failed: {e}")
            raise

    # --- Capture stream lifecycle -----------------------------------------------

    def _start_capture(self):
        self._capture_running = True
        self._unwatch_devices = audio_devices.watch_device_changes(self._reopen_requested.set)
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True, name="audio-capture")
        self._capture_thread.start()

    # Keep a stream open for the app's lifetime. Whenever it ends for any reason
    # other than shutdown, refresh PortAudio's device list and open a new one;
    # never give up, since the device may come back at any time.
    def _capture_loop(self):
        reopen_delay = self.REOPEN_DELAY_SECONDS
        while self._capture_running:
            try:
                self._run_stream()
                reopen_delay = self.REOPEN_DELAY_SECONDS
            except Exception as e:
                self._stream_error = e
                self.logger.warning(f"Audio stream on '{self.get_device_name()}' failed: {e}")
                reopen_delay = min(reopen_delay * 2, self.MAX_REOPEN_DELAY_SECONDS)

            if not self._capture_running:
                return
            if self.is_recording:
                print("⚠ Audio device lost during recording — recording discarded")
                with self._buffer_lock:
                    self.is_recording = False
                    self._buffer.clear()

            time.sleep(reopen_delay)
            self._reopen_requested.clear()
            try:
                self._reselect_device()
            except Exception as e:
                self.logger.error(f"Could not refresh audio devices: {e}")

    # Run one stream until shutdown, a requested reopen, or the device dying.
    # Returns normally for shutdown/reopen; raises when the device is lost.
    def _run_stream(self):
        self._last_callback_time = None
        opened_at = time.monotonic()
        with sd.InputStream(samplerate=self._recording_rate,
                            channels=self.channels,
                            callback=self._audio_callback,
                            dtype=self.STREAM_DTYPE,
                            blocksize=self._vad_blocksize if self.continuous_vad else None,
                            device=self.device,
                            latency='low') as stream:
            self.logger.info(f"Audio stream open on '{self.get_device_name()}'")
            while self._capture_running:
                if self.is_recording:
                    self._check_max_duration_exceeded()
                # A device change waits until the current recording is done,
                # unless the device itself is gone (caught below).
                elif self._reopen_requested.is_set():
                    self.logger.info("Audio devices changed; reopening input stream")
                    return
                if self._last_callback_time is None:
                    stalled_for, limit = time.monotonic() - opened_at, self.STREAM_START_TIMEOUT_SECONDS
                else:
                    stalled_for, limit = time.monotonic() - self._last_callback_time, self.STALL_TIMEOUT_SECONDS
                if not stream.active or stalled_for > limit:
                    raise RuntimeError(f"no audio for {stalled_for:.1f}s, device likely disconnected")
                sd.sleep(self.LOOP_SLEEP_MS)

    # Rebuild PortAudio's device list and pick the preferred device again (or
    # the default input while it is missing). Announces actual switches.
    def _reselect_device(self):
        previous_name = self.get_device_name()
        refresh_portaudio_device_list()
        self._select_device()
        self._configure_stream_format()
        # Pre-roll chunks from the old device may be at a different sample rate.
        with self._buffer_lock:
            self._buffer.clear()

        current_name = self.get_device_name()
        if current_name != previous_name:
            self.logger.info(f"Audio input switched: '{previous_name}' -> '{current_name}'")
            print(f"🎤 Audio input: {current_name}")
            if self.on_device_switched:
                self.on_device_switched(current_name)

    def _audio_callback(self, audio_data, frames, _time, status):
        self._last_callback_time = time.monotonic()
        chunk = audio_data.copy()
        recording = self.is_recording

        if recording:
            self._current_level = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
        else:
            self._current_level = 0.0

        with self._buffer_lock:
            self._buffer.append(chunk)
            # Trim to pre-roll size while idle. (VAD/streaming below operate on the
            # local `chunk` copy, not the buffer, so they stay outside the lock.)
            if not recording:
                while len(self._buffer) > self._preroll_max_chunks:
                    self._buffer.popleft()

        if recording:
            if self.continuous_vad and frames == self._vad_blocksize:
                if self._needs_resampling_cached:
                    chunk_16k = self._resample_audio(chunk, self._recording_rate, self.WHISPER_SAMPLE_RATE)
                    self.continuous_vad.process_chunk(chunk_16k.reshape(-1, 1))
                else:
                    self.continuous_vad.process_chunk(chunk)

            if self.continuous_streaming:
                self.continuous_streaming.process_chunk(chunk)

        if status:
            self.logger.debug(f"Audio callback status: {status}")

    def start_recording(self):
        if self.is_recording:
            return False

        self.logger.info("Starting audio recording...")
        self.recording_start_time = time.time()

        if self.continuous_vad:
            self.continuous_vad.reset()
        if self.continuous_streaming:
            self.continuous_streaming.reset()

        preroll_chunks = len(self._buffer)
        self.is_recording = True
        self.logger.debug(f"Recording started with {preroll_chunks} preroll chunks (~{preroll_chunks * self._vad_blocksize / self._recording_rate:.2f}s)")
        return True

    def stop_recording(self) -> Optional[np.ndarray]:
        if not self.is_recording:
            return None
        with self._buffer_lock:
            snapshot = list(self._buffer)
            self.is_recording = False
            self._buffer.clear()
        return self._build_audio_array(snapshot)

    def _build_audio_array(self, chunks) -> Optional[np.ndarray]:
        if not chunks:
            print("   ✗ No audio data recorded!")
            return None

        audio_array = np.concatenate(chunks, axis=0)
        peak = float(np.max(np.abs(audio_array))) if len(audio_array) else 0.0
        if peak < 1e-5:
            self.logger.warning(f"Recorded audio is silent (peak={peak:.2e}) — mic may be muted or permission denied")
            print("   ⚠ Recording captured pure silence — mic muted, unplugged, or OS permission denied?")

        if self._needs_resampling_cached:
            self.logger.info(f"Resampling from {self._recording_rate} Hz to {self.WHISPER_SAMPLE_RATE} Hz")
            audio_array = self._resample_audio(audio_array, self._recording_rate, self.WHISPER_SAMPLE_RATE)

        if self._noise_suppression_config.get('enabled'):
            from .noise_suppression import apply_noise_reduction
            strength = float(self._noise_suppression_config.get('strength', 0.75))
            audio_array = apply_noise_reduction(audio_array, self.WHISPER_SAMPLE_RATE, strength)

        audio_array = self._trim_long_pauses(audio_array)
        audio_array = self._trim_trailing_silence(audio_array)

        duration = self.get_audio_duration(audio_array)
        self.logger.info(f"Recorded {duration:.2f}s (incl. preroll, mid-pauses + trailing silence trimmed)")
        return audio_array

    def _trim_long_pauses(self, audio: np.ndarray) -> np.ndarray:
        if audio.ndim > 1:
            mono = audio.mean(axis=1)
        else:
            mono = audio
        win = int(0.05 * self.WHISPER_SAMPLE_RATE)
        if win < 1:
            return audio
        long_silence = int(self.LONG_PAUSE_SECONDS / 0.05)
        keep_windows = max(1, int(self.LONG_PAUSE_REPLACEMENT_SECONDS / 0.05))
        n = len(mono) // win
        if n < long_silence * 2:
            return audio

        voiced = np.empty(n, dtype=bool)
        for i in range(n):
            block = mono[i * win:(i + 1) * win]
            voiced[i] = float(np.sqrt(np.mean(block ** 2))) > self.SILENCE_RMS_THRESHOLD

        pieces = []
        i = 0
        modified = False
        while i < n:
            if voiced[i]:
                j = i
                while j < n and voiced[j]:
                    j += 1
                pieces.append(audio[i * win:j * win])
                i = j
            else:
                j = i
                while j < n and not voiced[j]:
                    j += 1
                run_windows = j - i
                if run_windows > long_silence:
                    pieces.append(audio[i * win:(i + keep_windows) * win])
                    modified = True
                else:
                    pieces.append(audio[i * win:j * win])
                i = j

        if not modified:
            return audio
        if (len(mono) % win) > 0:
            pieces.append(audio[n * win:])
        return np.concatenate(pieces, axis=0) if pieces else audio

    def _trim_trailing_silence(self, audio: np.ndarray) -> np.ndarray:
        if audio.ndim > 1:
            mono = audio.mean(axis=1)
        else:
            mono = audio
        window = int(0.02 * self.WHISPER_SAMPLE_RATE)
        if window < 1 or len(mono) < window * 4:
            return audio
        trim_samples = int(self.TRAILING_SILENCE_TRIM_SECONDS * self.WHISPER_SAMPLE_RATE)
        last_voice = len(mono)
        i = len(mono) - window
        while i > 0:
            block = mono[i:i + window]
            rms = float(np.sqrt(np.mean(block ** 2)))
            if rms > self.SILENCE_RMS_THRESHOLD:
                last_voice = i + window
                break
            i -= window
        keep_until = min(len(mono), last_voice + trim_samples)
        if keep_until >= len(mono) - window:
            return audio
        return audio[:keep_until]

    def get_current_level(self) -> float:
        return self._current_level

    def cancel_recording(self):
        if not self.is_recording:
            return
        with self._buffer_lock:
            self.is_recording = False
            self._buffer.clear()
        self.recording_start_time = None

    def shutdown(self):
        self._capture_running = False
        self._unwatch_devices()
        if self._capture_thread:
            self._capture_thread.join(timeout=self.THREAD_JOIN_TIMEOUT)

    def _check_max_duration_exceeded(self) -> bool:
        if self.max_duration <= 0 or not self.recording_start_time:
            return False
        if time.time() - self.recording_start_time < self.max_duration:
            return False

        self.logger.info(f"Maximum recording duration of {self.max_duration}s reached")
        print(f"⏰ Maximum recording duration of {self.max_duration}s reached - stopping recording")

        with self._buffer_lock:
            snapshot = list(self._buffer)
            self.is_recording = False
            self._buffer.clear()
        audio_data = self._build_audio_array(snapshot)
        if self.on_max_duration_reached:
            self.on_max_duration_reached(audio_data)
        return True

    def get_recording_status(self) -> bool:
        return self.is_recording

    def get_audio_duration(self, audio_data: np.ndarray) -> float:
        if audio_data is None or len(audio_data) == 0:
            return 0.0
        return len(audio_data) / self.sample_rate

    def get_device_id(self) -> Optional[int]:
        if self.device is not None:
            return self.device
        return sd.query_devices(kind='input')['index']

    @staticmethod
    def get_available_audio_devices(host_filter: Optional[str] = None):
        try:
            all_devices = sd.query_devices()
            hostapis = sd.query_hostapis()
        except Exception as e:
            logging.getLogger(__name__).error(f"Failed to enumerate audio devices: {e}")
            return []

        devices = []
        host_filter_lower = host_filter.lower() if host_filter else None

        for idx, device in enumerate(all_devices):
            if device.get('max_input_channels', 0) <= 0:
                continue

            hostapi_index = device['hostapi']
            hostapi_info = hostapis[hostapi_index]
            hostapi_name = hostapi_info['name']

            if host_filter_lower and hostapi_name.lower() != host_filter_lower:
                continue

            devices.append({
                'id': idx,
                'name': device['name'],
                'input_channels': device['max_input_channels'],
                'sample_rate': device['default_samplerate'],
                'hostapi': hostapi_name
            })

        return devices
