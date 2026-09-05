# PyInstaller spec — build a standalone Orrin.app (menu-bar assistant) for macOS.
#
#   pyinstaller packaging/pyinstaller/orrin.spec --noconfirm
#
# Produces dist/Orrin.app. Uses ctranslate2 (not torch) via faster-whisper, and onnxruntime
# for VAD/wake-word — so the bundle stays comparatively small. Whisper model weights are NOT
# bundled; they download to the user cache on first listen.
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files

# SPECPATH is injected by PyInstaller (the dir containing this spec). Paths must be resolved
# relative to it / the repo root, not the cwd.
HERE = SPECPATH
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

datas, binaries, hiddenimports = [], [], []

# Heavy/awkward packages that need their data files + dylibs collected wholesale.
for pkg in ["faster_whisper", "ctranslate2", "onnxruntime", "silero_vad", "openwakeword",
            "RealtimeSTT", "RealtimeTTS", "rumps", "fastmcp", "claude_agent_sdk", "pvporcupine"]:
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception as e:                       # a missing optional dep shouldn't break the build
        print(f"[orrin.spec] skip {pkg}: {e}")

# Our own package data: the dashboard web assets + menu-bar icons.
datas += collect_data_files("otto", includes=["server/web/*", "assets/*"])

# The default config template, placed at the bundle root so a DMG-only install (no install.sh)
# still seeds ~/Library/Application Support/Otto/config.yaml on first run. config.py's
# PACKAGED_DEFAULT resolves to <bundle root>/config.yaml (otto/config.py -> parent.parent), so
# it's found and copied; without this, DMG installs run on defaults with an empty settings drawer.
datas += [(os.path.join(ROOT, "config.yaml"), ".")]

hiddenimports += ["otto.menubar", "otto.server.app", "otto.webview", "uvicorn.logging",
                  "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
                  "uvicorn.lifespan.on"]

# In-app dashboard window (WKWebView) needs the WebKit PyObjC bridge; the first-run preflight
# needs AVFoundation (microphone permission) + ApplicationServices/Quartz (Accessibility).
for pkg in ["WebKit", "Cocoa", "objc", "AVFoundation", "CoreMedia",
            "Quartz", "ApplicationServices", "HIServices"]:
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception as e:
        print(f"[orrin.spec] skip {pkg}: {e}")

# torch CANNOT be excluded: RealtimeSTT imports it at module scope
# (RealtimeSTT/core/initialization.py:13), so dropping it makes `from RealtimeSTT import
# AudioToTextRecorder` raise ImportError and kills voice input in the bundle entirely.
# silero_use_onnx=True picks the VAD backend; it does not remove the import. torchaudio comes
# with it because silero_vad/utils_vad.py imports it. This costs ~1 GB — measure before
# trying to shave it again, and smoke-test the built bundle's imports.
EXCLUDES = ["torchvision", "tensorboard", "triton",
            "matplotlib", "scipy.spatial.cKDTree"]

a = Analysis(
    [os.path.join(HERE, "orrin_main.py")],
    pathex=[ROOT],
    datas=datas, binaries=binaries, hiddenimports=hiddenimports,
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Orrin",
          console=False, disable_windowed_traceback=False)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Orrin")

app = BUNDLE(
    coll,
    name="Orrin.app",
    icon=os.path.join(HERE, "orrin.icns") if os.path.exists(os.path.join(HERE, "orrin.icns")) else None,
    bundle_identifier="com.orrin.app",
    info_plist={
        "CFBundleName": "Orrin",
        "CFBundleDisplayName": "Orrin",
        "CFBundleShortVersionString": "0.2.7",
        "CFBundleVersion": "0.2.7",
        "LSMinimumSystemVersion": "12.0",
        "LSUIElement": True,                     # menu-bar agent: no Dock icon
        "NSMicrophoneUsageDescription":
            "Orrin listens to your voice when you talk to it.",
        "NSHighResolutionCapable": True,
        # The in-app dashboard (WKWebView) loads the local server over http://127.0.0.1 —
        # allow loopback/local networking under App Transport Security.
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
