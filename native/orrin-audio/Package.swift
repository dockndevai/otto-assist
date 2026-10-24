// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "orrin-audio",
    platforms: [.macOS(.v13)],
    targets: [
        // A tiny CLI that captures the mic through AVAudioEngine's voice-processing I/O
        // (on-device acoustic echo cancellation + noise suppression), segments speech with a
        // simple energy VAD, and streams 16 kHz mono int16 utterance PCM to stdout for Orrin.
        .executableTarget(name: "orrin-audio")
    ]
)
