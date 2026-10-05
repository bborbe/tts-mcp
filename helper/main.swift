// TTSDuck — ducks every other app's audio while the TTS speaks.
//
// Why this is a separate, bundled binary rather than a few lines in the Python
// server: a CoreAudio process tap needs the kTCCServiceAudioCapture grant, and
// macOS only gives that to a process it can identify — a code-signed bundle.
// A Python process launched through uv cannot hold it, and a process *spawned*
// by that Python inherits the parent's TCC identity, so it cannot hold it
// either. This helper therefore has to be launched by launchd or LaunchServices
// and driven over a socket.
//
// Mechanism: a global process tap (muteBehavior = .mutedWhenTapped) attached to
// a private aggregate device clocked by the default output device, read by an
// IOProc that applies a ramped gain. Because the tap is muted-when-tapped, the
// original audio is silenced only while this process is actually reading it —
// so if this helper dies, audio returns by itself rather than being stranded.
//
// Protocol (newline-delimited over a unix socket):
//   duck <level>   ramp other audio down to <level> (0.0-1.0) and keep reading
//   unduck         ramp back to 1.0, then stop reading
//   status         reply with one line of state
//   quit           tear down and exit

import CoreAudio
import Foundation

// MARK: - Gain state
//
// Written by the control thread, read and advanced by the IOProc on a realtime
// thread. A racy float is benign here: a gain is only ever a number between two
// valid values, so a torn read is impossible and a stale one lasts one buffer.

nonisolated(unsafe) var targetGain: Float = 1.0
nonisolated(unsafe) var currentGain: Float = 1.0
nonisolated(unsafe) var rampStepPerSample: Float = 1.0

func log(_ message: String) {
    FileHandle.standardError.write(("ttsduck: " + message + "\n").data(using: .utf8)!)
}

func fail(_ message: String) -> Never {
    log("FATAL: " + message)
    exit(1)
}

// MARK: - CoreAudio helpers

func defaultOutputDevice() -> AudioDeviceID? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var deviceID = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    let st = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &deviceID)
    return st == noErr ? deviceID : nil
}

func deviceUID(_ device: AudioDeviceID) -> String? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyDeviceUID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var cfStr: CFString?
    let st = withUnsafeMutablePointer(to: &cfStr) { ptr -> OSStatus in
        AudioObjectGetPropertyData(device, &addr, 0, nil, &size, ptr)
    }
    guard st == noErr, let s = cfStr else { return nil }
    return s as String
}

func nominalSampleRate(_ device: AudioDeviceID) -> Double {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyNominalSampleRate,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var rate = Float64(0)
    var size = UInt32(MemoryLayout<Float64>.size)
    let st = AudioObjectGetPropertyData(device, &addr, 0, nil, &size, &rate)
    return st == noErr && rate > 0 ? rate : 48000
}

// MARK: - Engine

/// Owns the tap, the aggregate device and the IOProc.
///
/// Deliberately lazy: nothing is created until the first `duck`, so an idle
/// helper has no effect whatsoever on the system audio path.
final class DuckEngine {
    private var tapID = AudioObjectID(0)
    private var aggregateID = AudioObjectID(0)
    private var procID: AudioDeviceIOProcID?
    private var masterUID: String?
    private var reading = false

    let fadeMilliseconds: Int

    init(fadeMilliseconds: Int) {
        self.fadeMilliseconds = fadeMilliseconds
    }

    /// Rebuilds the tap if the default output device changed underneath us.
    /// Cheaper and less failure-prone than a background watcher, and a USB
    /// audio device coming and going is exactly when this matters.
    private func rebuildIfDeviceChanged() {
        guard let device = defaultOutputDevice(), let uid = deviceUID(device) else {
            log("no default output device")
            return
        }
        if uid == masterUID && aggregateID != 0 { return }
        log("output device is \(uid) — building tap")
        teardown()
        build(device: device, uid: uid)
    }

    private func build(device: AudioDeviceID, uid: String) {
        let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [AudioObjectID]())
        desc.name = "ttsduck"
        // mutedWhenTapped, never .muted: .muted silences the source whether or
        // not we are reading, so a helper that died while ducking would leave
        // the machine silent. This variant releases the moment we stop.
        desc.muteBehavior = .mutedWhenTapped
        desc.isPrivate = true

        let tapStatus = AudioHardwareCreateProcessTap(desc, &tapID)
        guard tapStatus == noErr else { fail("create tap status=\(tapStatus)") }

        let aggDesc: [String: Any] = [
            kAudioAggregateDeviceNameKey as String: "ttsduck",
            kAudioAggregateDeviceUIDKey as String: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey as String: 1,
            kAudioAggregateDeviceMainSubDeviceKey as String: uid,
            kAudioAggregateDeviceSubDeviceListKey as String: [[kAudioSubDeviceUIDKey as String: uid]],
            kAudioAggregateDeviceTapListKey as String: [[
                kAudioSubTapUIDKey as String: desc.uuid.uuidString,
                kAudioSubTapDriftCompensationKey as String: 1,
            ]],
            kAudioAggregateDeviceTapAutoStartKey as String: 1,
        ]
        let aggStatus = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &aggregateID)
        guard aggStatus == noErr else {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = 0
            fail("create aggregate status=\(aggStatus)")
        }

        let rate = nominalSampleRate(device)
        rampStepPerSample = Float(1.0 / (rate * Double(fadeMilliseconds) / 1000.0))

        let ioStatus = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, nil) {
            _, inInputData, _, outOutputData, _ in
            let inList = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInputData))
            let outList = UnsafeMutableAudioBufferListPointer(outOutputData)
            let step = rampStepPerSample
            var gain = currentGain
            let target = targetGain
            for i in 0..<min(inList.count, outList.count) {
                guard let src = inList[i].mData, let dst = outList[i].mData else { continue }
                let count = Int(min(inList[i].mDataByteSize, outList[i].mDataByteSize)) / 4
                let s = src.assumingMemoryBound(to: Float.self)
                let d = dst.assumingMemoryBound(to: Float.self)
                for j in 0..<count {
                    if gain < target { gain = min(target, gain + step) }
                    else if gain > target { gain = max(target, gain - step) }
                    d[j] = s[j] * gain
                }
            }
            currentGain = gain
        }
        guard ioStatus == noErr else { fail("create ioproc status=\(ioStatus)") }
        masterUID = uid
    }

    private func startReading() {
        guard !reading, let proc = procID else { return }
        let st = AudioDeviceStart(aggregateID, proc)
        if st != noErr { log("AudioDeviceStart status=\(st)"); return }
        reading = true
    }

    private func stopReading() {
        guard reading, let proc = procID else { return }
        AudioDeviceStop(aggregateID, proc)
        reading = false
    }

    func duck(to level: Float) {
        rebuildIfDeviceChanged()
        // Start reading *before* touching the gain: the tap mutes the original
        // only while it is being read, so reading at the current gain keeps the
        // handover level-continuous instead of dipping.
        startReading()
        targetGain = max(0.0, min(1.0, level))
    }

    func unduck() {
        targetGain = 1.0
        // Let the ramp finish before releasing the tap, otherwise stopping mid
        // ramp would cut the tail off and step the level.
        let settle = Double(fadeMilliseconds) / 1000.0 + 0.05
        Thread.sleep(forTimeInterval: settle)
        stopReading()
    }

    func teardown() {
        stopReading()
        if let proc = procID {
            AudioDeviceDestroyIOProcID(aggregateID, proc)
            procID = nil
        }
        if aggregateID != 0 {
            AudioHardwareDestroyAggregateDevice(aggregateID)
            aggregateID = 0
        }
        if tapID != 0 {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = 0
        }
        masterUID = nil
        currentGain = 1.0
        targetGain = 1.0
    }

    var statusLine: String {
        "reading=\(reading ? 1 : 0) gain=\(String(format: "%.3f", currentGain)) "
            + "target=\(String(format: "%.3f", targetGain)) device=\(masterUID ?? "none")"
    }
}

// MARK: - Entry point

let args = CommandLine.arguments
let socketPath = args.count > 1 ? args[1] : "/tmp/ttsduck.sock"
let fadeMs = args.count > 2 ? (Int(args[2]) ?? 150) : 150

// A stale socket from a previous run would make bind() fail.
try? FileManager.default.removeItem(atPath: socketPath)

let fd = socket(AF_UNIX, SOCK_STREAM, 0)
guard fd >= 0 else { fail("socket() failed") }

var addr = sockaddr_un()
addr.sun_family = sa_family_t(AF_UNIX)
guard socketPath.utf8.count < MemoryLayout.size(ofValue: addr.sun_path) else {
    fail("socket path too long")
}
_ = withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
    socketPath.withCString { src in
        strncpy(UnsafeMutableRawPointer(ptr).assumingMemoryBound(to: CChar.self), src, 103)
    }
}

let bindResult = withUnsafePointer(to: &addr) { ptr in
    ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
        bind(fd, sa, socklen_t(MemoryLayout<sockaddr_un>.size))
    }
}
guard bindResult == 0 else { fail("bind(\(socketPath)) failed: errno \(errno)") }
guard listen(fd, 8) == 0 else { fail("listen failed") }

let engine = DuckEngine(fadeMilliseconds: fadeMs)
log("listening on \(socketPath) (fade \(fadeMs)ms)")

// No teardown on the way out is needed: the tap is owned by this process, and
// mutedWhenTapped releases the moment we stop reading, so a hard exit restores
// audio by itself. A stale socket file is removed before the next bind.
signal(SIGTERM) { _ in exit(0) }
signal(SIGINT) { _ in exit(0) }

while true {
    let client = accept(fd, nil, nil)
    if client < 0 { continue }

    var buffer = [UInt8](repeating: 0, count: 256)
    let read = recv(client, &buffer, buffer.count - 1, 0)
    if read > 0 {
        let command = String(decoding: buffer[0..<read], as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let parts = command.split(separator: " ", maxSplits: 1)
        var reply = "ok"

        switch parts.first.map(String.init) ?? "" {
        case "duck":
            let level = parts.count > 1 ? (Float(parts[1]) ?? 0.25) : 0.25
            engine.duck(to: level)
        case "unduck":
            engine.unduck()
        case "status":
            reply = engine.statusLine
        case "quit":
            reply = "bye"
            _ = reply.withCString { write(client, $0, strlen($0)) }
            close(client)
            engine.teardown()
            try? FileManager.default.removeItem(atPath: socketPath)
            exit(0)
        default:
            reply = "error unknown command"
        }

        _ = reply.withCString { write(client, $0, strlen($0)) }
    }
    close(client)
}
