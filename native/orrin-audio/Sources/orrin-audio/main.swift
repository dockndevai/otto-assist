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
let kEndSilenceDefaultMs: Int = 500  // trailing silence that ends an utterance (now dynamic, see below)
let kEndSilenceMinMs: Int = 150  // clamp for Python-driven end-silence
let kEndSilenceMaxMs: Int = 2000
let kMinUtteranceMs: Int = 300   // drop blips shorter than this
let kMaxUtteranceMs: Int = 15000 // hard cap so a continuous stream still ends
let kInterimMs: Int = 400        // emit an interim PCM snapshot roughly this often while speaking

// ---- dynamic end-silence (semantic endpointing, #109) -----------------------
// Python feeds a content-aware target via {"cmd":"endsilence","ms":N}: a complete-looking partial
// shortens it (snappy commit), an unfinished one lengthens it (never cut the speaker off). Clamped.
let endSilenceLock = NSLock()
var endSilenceMs = kEndSilenceDefaultMs
func getEndSilence() -> Int { endSilenceLock.lock(); defer { endSilenceLock.unlock() }; return endSilenceMs }
func setEndSilence(_ v: Int) {
    endSilenceLock.lock(); endSilenceMs = max(kEndSilenceMinMs, min(kEndSilenceMaxMs, v)); endSilenceLock.unlock()
}

// ---- instant barge-in -------------------------------------------------------
// ChatGPT-style interrupt: while Orrin is SPEAKING (the parent sets this via {"cmd":"speaking"}),
// sustained real speech — echo-cancelled by VoiceProcessingIO, so it's the USER, not Orrin's own
// voice — fires a one-shot {"type":"barge"} so the parent can cut playback the instant you start,
// without waiting to transcribe a "stop" word. Thresholds are pushed from config ({"cmd":"bargecfg"}).
let bargeLock = NSLock()
var speaking = false
var bargeMinMs = 180
var bargeRms = 900
func isSpeaking() -> Bool { bargeLock.lock(); defer { bargeLock.unlock() }; return speaking }
func setSpeaking(_ v: Bool) { bargeLock.lock(); speaking = v; bargeLock.unlock() }
func getBargeMinMs() -> Int { bargeLock.lock(); defer { bargeLock.unlock() }; return bargeMinMs }
func getBargeRms() -> Double { bargeLock.lock(); defer { bargeLock.unlock() }; return Double(bargeRms) }
func setBargeCfg(ms: Int?, rms: Int?) {
    bargeLock.lock()
    if let m = ms { bargeMinMs = max(60, min(2000, m)) }
    if let r = rms { bargeRms = max(200, min(20000, r)) }
    bargeLock.unlock()
}

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

// An in-progress snapshot of the current utterance, for streaming partials (#109). Same framing as
// `utterance`, type "interim" — the parent transcribes the rolling buffer and never treats it as final.
func emitInterim(_ pcm: Data) {
    outLock.lock(); defer { outLock.unlock() }
    if let h = ("{\"type\":\"interim\",\"bytes\":\(pcm.count)}\n").data(using: .utf8) {
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
var lastInterimBytes = 0   // utterance length at the last interim emit (throttle interim snapshots)
var sinceInterimMs = 0     // time since the last interim emit
var speechMs = 0           // accumulated speech duration this utterance (for the barge-in gate)
var bargeFired = false     // one {"type":"barge"} per utterance while Orrin is speaking
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
        if !inSpeech {
            inSpeech = true; silenceMs = 0; sinceInterimMs = 0; lastInterimBytes = 0
            speechMs = 0; bargeFired = false
            utterance.removeAll(keepingCapacity: true)
            emit("{\"type\":\"speech_start\"}")
        }
        utterance.append(bytes)
        silenceMs = 0
        speechMs += frameMs
        // Instant barge-in: sustained real speech while Orrin is speaking → fire once, so the parent
        // cuts playback immediately. The RMS floor (above the start gate) rejects low-level residual
        // echo the AEC didn't fully remove; the duration floor rejects brief clicks/transients.
        if !bargeFired && isSpeaking() && speechMs >= getBargeMinMs() && level >= getBargeRms() {
            emit("{\"type\":\"barge\"}")
            bargeFired = true
        }
        // Streaming partials: every ~kInterimMs of fresh speech, hand the parent a snapshot to
        // transcribe so endpointing + intent can start before the turn ends.
        sinceInterimMs += frameMs
        if sinceInterimMs >= kInterimMs && utterance.count > lastInterimBytes {
            emitInterim(utterance)
            lastInterimBytes = utterance.count
            sinceInterimMs = 0
        }
    } else if inSpeech {
        utterance.append(bytes)                 // keep a little trailing silence
        silenceMs += frameMs
        let lenMs = utterance.count / 2 * 1000 / Int(kSampleRate)
        if silenceMs >= getEndSilence() || lenMs >= kMaxUtteranceMs {
            if lenMs - silenceMs >= kMinUtteranceMs { emitUtterance(utterance) }
            inSpeech = false
            utterance.removeAll(keepingCapacity: true)
            setEndSilence(kEndSilenceDefaultMs)   // reset to neutral for the next turn
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
func parseObj(_ line: String) -> [String: Any]? {
    guard let d = line.data(using: .utf8) else { return nil }
    return (try? JSONSerialization.jsonObject(with: d)) as? [String: Any]
}

let stdinThread = Thread {
    while let line = readLine(strippingNewline: true) {
        if line.contains("endsilence") {
            if let ms = parseObj(line)?["ms"] as? Int { setEndSilence(ms) }
        }
        else if line.contains("bargecfg") {
            let o = parseObj(line); setBargeCfg(ms: o?["ms"] as? Int, rms: o?["rms"] as? Int)
        }
        else if line.contains("speaking") {
            if let on = parseObj(line)?["on"] as? Bool { setSpeaking(on) }
        }
        else if line.contains("unmute") { setMuted(false) }
        else if line.contains("mute") { setMuted(true) }
        else if line.contains("quit") { exit(0) }
    }
    exit(0)   // stdin closed (parent gone) → shut down
}
stdinThread.start()

RunLoop.main.run()
