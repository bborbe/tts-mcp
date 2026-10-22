"""macOS CoreAudio HAL device queries and default-output-device change watcher."""

import ctypes
import ctypes.util
import os
import sys
import threading
from collections.abc import Callable


class _AudioObjectPropertyAddress(ctypes.Structure):
    """CoreAudio AudioObjectPropertyAddress: (selector, scope, element)."""

    _fields_ = (
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    )


def default_output_device_id() -> int | None:
    """Return the macOS system default output device ID, or None if unavailable.

    Reads kAudioHardwarePropertyDefaultOutputDevice directly from the CoreAudio HAL
    via ctypes. Unlike sounddevice/PortAudio (which snapshots its device list at
    initialization), this reflects a live default-device switch immediately and
    without tearing PortAudio down. Used to detect when the warm output stream must
    be reopened on a newly-selected device, so PortAudio is re-initialized only on an
    actual change rather than on every utterance. Returns None off macOS or on any
    query failure, in which case the caller keeps the current warm stream.
    """
    if sys.platform != "darwin":
        return None
    try:
        lib_path = ctypes.util.find_library("CoreAudio")
        if lib_path is None:
            return None
        core_audio = ctypes.CDLL(lib_path)
    except OSError as exc:
        print(f"\n  CoreAudio load failed, skipping device-change detection: {exc}", file=sys.stderr)
        return None

    # FourCC codes: 'dOut' = default output device selector, 'glob' = global scope; element 0 = main.
    address = _AudioObjectPropertyAddress(0x644F7574, 0x676C6F62, 0)
    system_object = ctypes.c_uint32(1)  # kAudioObjectSystemObject
    device_id = ctypes.c_uint32(0)
    data_size = ctypes.c_uint32(ctypes.sizeof(device_id))

    get_property = core_audio.AudioObjectGetPropertyData
    get_property.restype = ctypes.c_int32
    status = get_property(
        system_object,
        ctypes.byref(address),
        ctypes.c_uint32(0),
        None,
        ctypes.byref(data_size),
        ctypes.byref(device_id),
    )
    if status != 0:
        print(f"\n  CoreAudio default-output query returned status {status}", file=sys.stderr)
        return None
    return int(device_id.value)


def restart_process_on_device_change(_new_device: int) -> None:
    """Default watcher handler: exit so launchd (KeepAlive) respawns a fresh process.

    Restarting yields a clean CoreAudio HAL bound to the new default output device,
    avoiding the in-process PortAudio re-init that degrades the HAL (see ``AudioPlayer``).
    """
    os._exit(0)


def start_output_device_change_watcher(
    poll_interval_s: float,
    get_device: Callable[[], int | None],
    on_change: Callable[[int], None],
    stop_event: threading.Event,
    sustained_polls: int = 3,
) -> threading.Thread:
    """Restart the process when the macOS default output device changes.

    An in-process PortAudio re-init (``sd._terminate``/``_initialize``) after a
    live device switch degrades the CoreAudio HAL and distorts playback (see
    ``AudioPlayer``). Instead, this background daemon thread polls the HAL for the
    default output device and, on a change from the boot device, calls ``on_change``
    (in production ``restart_process_on_device_change``, which exits so launchd
    ``KeepAlive`` respawns a fresh process with a clean HAL bound to the new device).
    Trade-off: the fresh process reloads the TTS model (~15-20s of no voice),
    acceptable for infrequent plug/unplug switches.

    Only a *sustained* switch triggers a restart: the same non-boot device id must
    be reported on ``sustained_polls`` consecutive polls. A transient/aggregate
    device id the HAL reports mid-playback resets the streak as soon as the poll
    returns to the boot device or to a different id, so it never fires. This is
    what the docstring promised before it was implemented: without it, the watcher
    restarted on the first differing poll, and transient blips restarted the
    process ~808 times.

    Args:
        poll_interval_s: Seconds between HAL polls.
        get_device: Returns the current default output device id (injectable for tests).
        on_change: Called with the new device id on a sustained change.
        stop_event: When set, the watch loop returns (used by tests).
        sustained_polls: Consecutive polls reporting the same non-boot device id
            required before ``on_change`` fires. Defaults to 3, i.e. ~6s at the
            2.0s poll interval in ``src/server.py``.

    Returns:
        The started daemon thread.

    Raises:
        ValueError: If ``sustained_polls`` is less than 1.
    """
    if sustained_polls < 1:
        msg = f"sustained_polls must be >= 1, got {sustained_polls}"
        raise ValueError(msg)

    def _run() -> None:
        boot_device = get_device()
        candidate: int | None = None
        streak = 0
        while not stop_event.wait(poll_interval_s):
            current = get_device()
            if current is None or boot_device is None or current == boot_device:
                candidate, streak = None, 0
                continue
            if current == candidate:
                streak += 1
            else:
                candidate, streak = current, 1
            if streak < sustained_polls:
                continue
            print(
                f"audio: default output device changed ({boot_device} -> {current}) "
                f"for {streak} consecutive polls; restarting process for a clean CoreAudio HAL",
                file=sys.stderr,
            )
            on_change(current)
            return

    thread = threading.Thread(target=_run, name="device-change-watcher", daemon=True)
    thread.start()
    return thread
