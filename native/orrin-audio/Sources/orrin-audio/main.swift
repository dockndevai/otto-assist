// orrin-audio — native macOS audio front-end for Orrin.
//
// Captures the microphone through AVAudioEngine with voice-processing I/O enabled, which turns on
// the system's on-device ACOUSTIC ECHO CANCELLATION (+ noise suppression + AGC) referencing whatever
// is playing on the output device. That means the mic no longer hears Orrin's own TTS, so Orrin can
// be full-duplex on the built-in speakers (barge-in) instead of muting the mic while it speaks.
//
// It segments speech with a simple energy VAD and streams each finalized utterance to stdout as:
//   {"type":"speech_start"}\n                         (JSON control line)
//   {"type":"utterance","bytes":N}\n<N raw bytes>      (header line, then N bytes of PCM)
// PCM is 16 kHz, mono, signed 16-bit little-endian. Control commands arrive on stdin as JSON lines:
//   {"cmd":"mute"} / {"cmd":"unmute"}.
//
// Thresholds below are best-known defaults; tune for your room. This file is self-contained.

import AVFoundation
import Foundation

// ---- tunables ---------------------------------------------------------------
let kSampleRate: Double = 16000
let kStartRMS: Double = 600      // int16 RMS above this = speech
let kEndSilenceMs: Int = 700     // trailing silence that ends an utterance
let kMinUtteranceMs: Int = 300   // drop blips shorter than this
let kMaxUtteranceMs: Int = 15000 // hard cap so a continuous stream still ends

// ---- stdout framing (one writer at a time) ----------------------------------
let outLock = NSLock()
let stdoutHandle = FileHandle.standardOutput

func emit(_ json: String) {
    outLock.lock(); defer { outLock.unlock() }
    if let d = (json + "\n").data(using: .utf8) { stdoutHandle.write(d) }
}

func emitUtterance(_ pcm: Data) {
    outLock.lock(); defer { outLock.unlock() }
    if let h = ("{\"type\":\"utterance\",\"bytes\":\(pcm.count)}\n").data(using: .utf8) {
        stdoutHandle.write(h)
        stdoutHandle.write(pcm)
    }
}

func logErr(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8) ?? Data()) }

// ---- shared mute flag -------------------------------------------------------
let muteLock = NSLock()
var muted = false
func isMuted() -> Bool { muteLock.lock(); defer { muteLock.unlock() }; return muted }
func setMuted(_ v: Bool) { muteLock.lock(); muted = v; muteLock.unlock() }

// ---- VAD state (only touched on the audio thread) ---------------------------
var inSpeech = false
var silenceMs = 0
var utterance = Data()
let frameMs = 20  // we process ~20ms converted frames

func rms(_ ptr: UnsafePointer<Int16>, _ count: Int) -> Double {
    if count == 0 { return 0 }
    var acc: Double = 0
    for i in 0..<count { let v = Double(ptr[i]); acc += v * v }
    return (acc / Double(count)).squareRoot()
}

func handleFrame(_ ptr: UnsafePointer<Int16>, _ count: Int) {
    if isMuted() { return }
    let level = rms(ptr, count)
    let bytes = Data(bytes: ptr, count: count * 2)
    if level >= kStartRMS {
        if !inSpeech { inSpeech = true; silenceMs = 0; utterance.removeAll(keepingCapacity: true); emit("{\"type\":\"speech_start\"}") }
        utterance.append(bytes)
        silenceMs = 0
    } else if inSpeech {
        utterance.append(bytes)                 // keep a little trailing silence
        silenceMs += frameMs
        let lenMs = utterance.count / 2 * 1000 / Int(kSampleRate)
        if silenceMs >= kEndSilenceMs || lenMs >= kMaxUtteranceMs {
            if lenMs - silenceMs >= kMinUtteranceMs { emitUtterance(utterance) }
            inSpeech = false
            utterance.removeAll(keepingCapacity: true)
        }
    }
}

// ---- audio engine -----------------------------------------------------------
let engine = AVAudioEngine()
let input = engine.inputNode
var aecEnabled = false
do {
    try input.setVoiceProcessingEnabled(true)   // <-- enables VoiceProcessingIO AEC
    aecEnabled = true
} catch {
    aecEnabled = false
    logErr("[orrin-audio] voice processing unavailable: \(error) — continuing without AEC")
}

let inFmt = input.outputFormat(forBus: 0)
guard inFmt.sampleRate > 0,
      let outFmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: kSampleRate,
                                 channels: 1, interleaved: true),
      let converter = AVAudioConverter(from: inFmt, to: outFmt) else {
    logErr("[orrin-audio] no usable input format")
    emit("{\"type\":\"error\",\"msg\":\"no input device\"}")
    exit(1)
}

input.installTap(onBus: 0, bufferSize: 1024, format: inFmt) { (buf, _) in
    let outCap = AVAudioFrameCount(Double(buf.frameLength) * kSampleRate / inFmt.sampleRate) + 32
    guard let outBuf = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: outCap) else { return }
    var fed = false
    var err: NSError?
    converter.convert(to: outBuf, error: &err) { _, status in
        if fed { status.pointee = .noDataNow; return nil }
        fed = true; status.pointee = .haveData; return buf
    }
    if let err = err { logErr("[orrin-audio] convert: \(err)"); return }
    guard let ch = outBuf.int16ChannelData, outBuf.frameLength > 0 else { return }
    handleFrame(ch[0], Int(outBuf.frameLength))
}

do {
    engine.prepare()
    try engine.start()
    // The AEC-readiness handshake: Python enables full-duplex ONLY when aec is true.
    emit("{\"type\":\"ready\",\"aec\":\(aecEnabled)}")
    logErr("[orrin-audio] started (in=\(inFmt.sampleRate)Hz -> 16000Hz, AEC \(aecEnabled ? "on" : "OFF"))")
} catch {
    logErr("[orrin-audio] engine failed: \(error)")
    emit("{\"type\":\"error\",\"msg\":\"engine start failed\"}")
    exit(1)
}

// ---- stdin: mute/unmute commands -------------------------------------------
let stdinThread = Thread {
    while let line = readLine(strippingNewline: true) {
        if line.contains("unmute") { setMuted(false) }
        else if line.contains("mute") { setMuted(true) }
        else if line.contains("quit") { exit(0) }
    }
    exit(0)   // stdin closed (parent gone) → shut down
}
stdinThread.start()

RunLoop.main.run()
