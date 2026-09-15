"""First-run permissions + a startup/restart health check ("is Otto ready?").

Two jobs:

1. **Request the OS permissions Otto needs** — at first run and any time they're missing.
   Microphone is the important one: without it the ears can't open the input device and
   Otto silently can't hear you. We ask macOS to show its permission prompt *up front*
   (``AVCaptureDevice.requestAccessForMediaType``) instead of letting the recorder subprocess
   trip a silent denial. Accessibility is only needed for the global push-to-talk hotkey.

2. **Validate everything end to end** — mic access, the ``claude`` CLI, a real *test turn*
   through the Claude Agent SDK (proves the binary runs, is logged in, and can complete a
   turn), and voice output. The orchestrator runs these on ``start()`` (so they re-run on
   every restart) and publishes the results to the dashboard's "Otto ready" panel.

All macOS calls are guarded: on a non-mac host, or if a PyObjC framework is missing, the
check degrades to ``unknown``/``warn`` rather than raising.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from typing import Any

from .config import OTTO_HOME, Settings

_IS_MAC = sys.platform == "darwin"

# Marker written the first time Otto reaches full readiness. Its absence == "first run",
# which the dashboard uses to auto-open the setup panel.
_SETUP_MARKER = OTTO_HOME / ".setup_complete"


# -- a single check result --------------------------------------------------
@dataclass
class Check:
    key: str
    label: str
    status: str = "checking"     # checking | ok | warn | fail | pending
    detail: str = ""
    required: bool = True         # required checks gate overall readiness
    fix: str = ""                 # short human "how to fix"
    pane: str = ""                # a System Settings privacy pane to offer opening

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# macOS permission helpers (Microphone via AVFoundation, Accessibility via AX).
# ---------------------------------------------------------------------------
_MIC_STATUS = {0: "notDetermined", 1: "restricted", 2: "denied", 3: "authorized"}


def mic_authorization_status() -> str:
    """TCC microphone status: notDetermined | restricted | denied | authorized | unknown."""
    if not _IS_MAC:
        return "unknown"
    try:
        import AVFoundation  # type: ignore
        st = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
            AVFoundation.AVMediaTypeAudio)
        return _MIC_STATUS.get(int(st), "unknown")
    except Exception:
        return "unknown"


def request_mic_access(timeout: float = 90.0) -> str:
    """If mic access is undecided, show the system prompt and wait for the answer.

    Returns the resulting status string. Safe to call repeatedly: once the user has decided
    (authorized/denied), macOS won't prompt again and this just returns the current status.
    """
    st = mic_authorization_status()
    if st != "notDetermined":
        return st
    try:
        import AVFoundation  # type: ignore
        done = threading.Event()

        def _handler(granted: bool) -> None:      # block: void(^)(BOOL)
            done.set()

        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVFoundation.AVMediaTypeAudio, _handler)
        done.wait(timeout)
    except Exception as e:
        print(f"[otto] mic request failed: {e}", file=sys.stderr, flush=True)
    return mic_authorization_status()


def request_mic_access_async(callback) -> None:
    """Fire the microphone prompt WITHOUT blocking; call ``callback(granted: bool)`` when the
    user responds (or immediately if already decided).

    Must be called on the app's **main thread with an active run loop** (the menu-bar/AppKit
    thread) — that's the only context where macOS will actually present the prompt for a
    menu-bar agent, and where the resulting grant is real. Calling AVFoundation's request from a
    background worker thread instead resolves to a *false* "authorized" with no UI and leaves
    capture denied. The main app must hold the grant so the recorder child (a re-exec of the
    same signed binary, which TCC won't prompt for on its own) inherits it.
    """
    if not _IS_MAC:
        callback(False)
        return
    try:
        import AVFoundation  # type: ignore
        st = mic_authorization_status()
        if st == "authorized":
            callback(True)
            return
        if st in ("denied", "restricted"):
            callback(False)
            return

        def _handler(granted: bool) -> None:      # block: void(^)(BOOL)
            try:
                callback(bool(granted))
            except Exception:
                pass

        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVFoundation.AVMediaTypeAudio, _handler)
    except Exception as e:
        print(f"[otto] async mic request failed: {e}", file=sys.stderr, flush=True)
        callback(False)


def accessibility_trusted() -> bool:
    """Is this process trusted for Accessibility (needed for the global PTT hotkey)?"""
    if not _IS_MAC:
        return False
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore
        return bool(AXIsProcessTrusted())
    except Exception:
        return False


# IOKit HID access = macOS "Input Monitoring", what a global key monitor actually needs. We
# query/request it directly (AXIsProcessTrusted only reflects Accessibility, a different grant).
_IOHID_LISTEN = 1                       # kIOHIDRequestTypeListenEvent
_IOHID_GRANTED, _IOHID_DENIED = 0, 1    # kIOHIDAccessType*


def _iokit():
    import ctypes
    lib = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
    lib.IOHIDCheckAccess.restype = ctypes.c_int
    lib.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
    lib.IOHIDRequestAccess.restype = ctypes.c_bool
    lib.IOHIDRequestAccess.argtypes = [ctypes.c_uint32]
    return lib


def input_monitoring_status() -> str:
    """"granted" / "denied" / "unknown" for Input Monitoring (the global-hotkey permission)."""
    if not _IS_MAC:
        return "unknown"
    try:
        v = _iokit().IOHIDCheckAccess(_IOHID_LISTEN)
        return {0: "granted", 1: "denied"}.get(v, "unknown")
    except Exception:
        return "unknown"


def request_input_monitoring() -> bool:
    """Ask macOS for Input Monitoring — registers this app in the list and prompts on first ask.
    MUST run on the main thread (like the mic prompt). Returns True if already/now granted."""
    if not _IS_MAC:
        return False
    try:
        ok = bool(_iokit().IOHIDRequestAccess(_IOHID_LISTEN))
        if not ok:
            open_privacy_pane("input_monitoring")   # already-denied: send them to the toggle
        return ok
    except Exception:
        open_privacy_pane("input_monitoring")
        return False


def prompt_accessibility() -> None:
    """Ask macOS to show the 'add this app to Accessibility' prompt (best effort)."""
    if not _IS_MAC:
        return
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions  # type: ignore
        try:
            from ApplicationServices import kAXTrustedCheckOptionPrompt  # type: ignore
        except Exception:
            from HIServices import kAXTrustedCheckOptionPrompt  # type: ignore
        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    except Exception:
        open_privacy_pane("accessibility")


def screen_recording_status() -> str:
    """"authorized" / "denied" / "unknown" for Screen Recording (computer-use screenshots)."""
    if not _IS_MAC:
        return "unknown"
    try:
        from Quartz import CGPreflightScreenCaptureAccess  # type: ignore
        return "authorized" if CGPreflightScreenCaptureAccess() else "denied"
    except Exception:
        return "unknown"


def request_screen_recording() -> bool:
    """Ask macOS for Screen Recording — registers this app in the list and prompts on first ask.
    Returns True if already/now granted (the prompt itself is async; a fresh denial opens the
    pane so the user can toggle it)."""
    if not _IS_MAC:
        return False
    try:
        from Quartz import (CGPreflightScreenCaptureAccess,  # type: ignore
                            CGRequestScreenCaptureAccess)
        if CGPreflightScreenCaptureAccess():
            return True
        ok = bool(CGRequestScreenCaptureAccess())
        if not ok:
            open_privacy_pane("screen_recording")
        return ok
    except Exception:
        open_privacy_pane("screen_recording")
        return False


_PANES = {
    "microphone": "Privacy_Microphone",
    "accessibility": "Privacy_Accessibility",
    "automation": "Privacy_Automation",
    "input_monitoring": "Privacy_ListenEvent",   # global key monitor (push-to-talk hotkey)
    "screen_recording": "Privacy_ScreenCapture", # computer-use screenshots / screen reading
}


def open_privacy_pane(which: str) -> bool:
    """Open a System Settings → Privacy pane so the user can toggle a permission."""
    key = _PANES.get(which)
    if not key or not _IS_MAC:
        return False
    url = f"x-apple.systempreferences:com.apple.preference.security?{key}"
    try:
        subprocess.Popen(["open", url])
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The individual checks.
# ---------------------------------------------------------------------------
def check_microphone(settings: Settings) -> Check:
    c = Check("microphone", "Microphone access", required=settings.voice.input_enabled,
              pane="microphone")
    if not settings.voice.input_enabled:
        c.status, c.detail = "warn", "Voice input is turned off in settings."
        return c
    st = mic_authorization_status()
    if st == "authorized":
        c.status, c.detail = "ok", "Granted."
    elif st == "notDetermined":
        # Not blocking: the ears start anyway, and opening the mic shows the OS prompt.
        c.status = "warn"
        c.detail = "Approve the microphone prompt when it appears (Otto is asking now)."
        c.fix = "Click Allow on the microphone prompt, then Recheck."
    elif st == "unknown":
        c.status, c.detail = "warn", "Couldn't read microphone permission."
    else:  # denied / restricted
        c.status = "fail"
        c.detail = "macOS is blocking the microphone."
        c.fix = "Enable Orrin under System Settings → Privacy & Security → Microphone."
    return c


def check_screen_recording(settings: Settings) -> Check:
    # Needed for computer-use (screenshots / reading the screen to click & type). Not required
    # for voice itself, so informational — but surfaced so the user can grant it up front.
    c = Check("screen_recording", "Screen Recording (computer control)", required=False,
              pane="screen_recording")
    st = screen_recording_status()
    if st == "authorized":
        c.status, c.detail = "ok", "Granted."
    elif st == "unknown":
        c.status, c.detail = "warn", "Couldn't read Screen Recording permission."
    else:  # denied / notDetermined
        c.status = "warn"
        c.detail = "Needed so Orrin can see the screen to click and type."
        c.fix = ("Enable Orrin under System Settings → Privacy & Security → Screen Recording, "
                 "then Recheck.")
    return c


def check_claude_cli(settings: Settings) -> Check:
    c = Check("claude_cli", "Claude CLI found")
    path = shutil.which("claude")
    if path:
        c.status, c.detail = "ok", path
    else:
        c.status = "fail"
        c.detail = "The `claude` command isn't on PATH."
        c.fix = "Install Claude Code and make sure `claude` runs in a terminal."
    return c


def check_accessibility(settings: Settings) -> Check:
    # Only *required* if the global push-to-talk hotkey is enabled; otherwise informational.
    needed = settings.voice.hotkey.enabled
    c = Check("accessibility", "Input Monitoring (global hotkey)", required=needed,
              pane="input_monitoring")
    # Query the actual Input Monitoring grant (what the key monitor needs); fall back to the
    # Accessibility trust bit only if the IOKit query is unavailable.
    status = input_monitoring_status()
    granted = status == "granted" or (status == "unknown" and accessibility_trusted())
    if granted:
        c.status, c.detail = "ok", "Granted."
    elif needed:
        c.status = "fail"
        c.detail = "The push-to-talk hotkey needs Input Monitoring."
        c.fix = "Enable Orrin under System Settings → Privacy & Security → Input Monitoring."
    else:
        c.status = "warn"
        c.detail = "Not granted (only needed if you enable the global push-to-talk hotkey)."
    return c


async def check_claude_test(settings: Settings, timeout: float = 60.0) -> Check:
    """A real end-to-end test turn: connect a throwaway Claude client and get one reply.

    Proves the `claude` binary runs, is authenticated, and can complete a turn (permissions).
    Runs in a temp cwd with no user setting-sources so it doesn't touch a real project session.
    """
    c = Check("claude_test", "Claude connection (test run)")
    if not shutil.which("claude"):
        c.status, c.detail = "fail", "Skipped — Claude CLI not found."
        c.fix = "Install Claude Code first."
        return c
    import tempfile

    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
    )

    tmp = tempfile.mkdtemp(prefix="otto-preflight-")
    opts = ClaudeAgentOptions(
        cwd=tmp,
        setting_sources=[],          # don't load user MCP/agents — keep the probe fast & clean
        permission_mode="default",
        include_partial_messages=False,
        **({"model": settings.brain.model} if settings.brain.model else {}),
    )
    client = ClaudeSDKClient(options=opts)

    async def _run() -> tuple[str | None, bool, str]:
        await client.connect()
        await client.query("Reply with exactly: OK")
        sid: str | None = None
        err = False
        async for m in client.receive_response():
            if isinstance(m, ResultMessage):
                sid = getattr(m, "session_id", None)
                err = bool(getattr(m, "is_error", False))
        return sid, err, ""

    try:
        sid, err, _ = await asyncio.wait_for(_run(), timeout)
        if err:
            c.status, c.detail = "fail", "Claude returned an error on the test turn."
            c.fix = "Run `claude` in a terminal to check it's set up and logged in."
        elif sid:
            c.status, c.detail = "ok", "Test turn completed."
        else:
            c.status, c.detail = "warn", "Test turn finished but returned no session id."
    except asyncio.TimeoutError:
        c.status = "fail"
        c.detail = f"No reply within {int(timeout)}s."
        c.fix = "Check your network and that `claude` runs in a terminal (login/quota)."
    except Exception as e:
        c.status = "fail"
        c.detail = f"{type(e).__name__}: {e}"
        c.fix = "Run `claude` in a terminal to check it's installed and logged in."
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return c


def check_voice_output(settings: Settings) -> Check:
    c = Check("voice_output", "Voice output", required=False)
    if not settings.voice.output_enabled:
        c.status, c.detail = "warn", "Speaking replies is turned off in settings."
        return c
    # We can't fully prove audio out without playing a sound; confirm the engine imports.
    try:
        from .voice.tts import Mouth  # noqa: F401
        c.status, c.detail = "ok", f"{settings.voice.tts.engine} engine ready."
    except Exception as e:
        c.status, c.detail = "warn", f"Voice engine unavailable: {e}"
    return c


# ---------------------------------------------------------------------------
# First-run marker.
# ---------------------------------------------------------------------------
def is_first_run() -> bool:
    return not _SETUP_MARKER.exists()


def mark_setup_complete() -> None:
    try:
        OTTO_HOME.mkdir(parents=True, exist_ok=True)
        _SETUP_MARKER.write_text("ok\n")
    except Exception:
        pass


def summarize(checks: list[Check]) -> bool:
    """Ready == every *required* check passed (a warn on a required check does not block)."""
    return all(c.status in ("ok", "warn") for c in checks if c.required)
