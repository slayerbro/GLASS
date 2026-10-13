// glass-audio — streams the Mac's system audio (what you hear: the Meet call) to
// stdout as little-endian Float32 mono PCM, using ScreenCaptureKit (macOS 13+).
//
//   stderr: "RATE <hz>" once the format is known, "READY" when capturing,
//           "ERROR <message>" on failure (then exits non-zero).
//   Exits when stdin reaches EOF (the GLASS engine went away).
//
// Build (done by setup_engine.sh):
//   swiftc -O -parse-as-library glass-audio.swift -o glass-audio
//
// The first run triggers macOS's "Screen & System Audio Recording" permission
// prompt for the app that launched it (GLASS / Terminal).

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

final class Tap: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private var announced = false
    private let out = FileHandle.standardOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }
        guard let desc = sampleBuffer.formatDescription?.audioStreamBasicDescription else { return }
        let frames = sampleBuffer.numSamples
        if frames <= 0 { return }
        let isFloat = (desc.mFormatFlags & kAudioFormatFlagIsFloat) != 0
        guard isFloat, desc.mBitsPerChannel == 32 else {
            Tap.log("ERROR unsupported audio format flags=\(desc.mFormatFlags) bits=\(desc.mBitsPerChannel)")
            exit(3)
        }
        if !announced {
            announced = true
            Tap.log("RATE \(Int(desc.mSampleRate))")
        }
        var mono = [Float](repeating: 0, count: frames)
        do {
            try sampleBuffer.withAudioBufferList { abl, _ in
                let nonInterleaved = (desc.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0
                if nonInterleaved {
                    // one buffer per channel: average them
                    let n = Float(max(1, abl.count))
                    for buf in abl {
                        guard let p = buf.mData?.assumingMemoryBound(to: Float.self) else { continue }
                        let count = min(frames, Int(buf.mDataByteSize) / MemoryLayout<Float>.size)
                        for i in 0..<count { mono[i] += p[i] / n }
                    }
                } else {
                    // interleaved: L R L R ...
                    let ch = Int(max(1, desc.mChannelsPerFrame))
                    guard let buf = abl.first, let p = buf.mData?.assumingMemoryBound(to: Float.self) else { return }
                    let count = min(frames, Int(buf.mDataByteSize) / MemoryLayout<Float>.size / ch)
                    for i in 0..<count {
                        var s: Float = 0
                        for c in 0..<ch { s += p[i * ch + c] }
                        mono[i] = s / Float(ch)
                    }
                }
            }
        } catch {
            return
        }
        mono.withUnsafeBufferPointer { bp in
            out.write(Data(buffer: bp))  // SIGPIPE ends us if the engine has gone
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        Tap.log("ERROR stream stopped: \(error.localizedDescription)")
        exit(2)
    }

    static func log(_ s: String) {
        FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
    }
}

@main
struct GlassAudio {
    static func main() async {
        // stop when the GLASS engine closes our stdin
        Thread.detachNewThread {
            while readLine(strippingNewline: true) != nil {}
            exit(0)
        }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            guard let display = content.displays.first else {
                Tap.log("ERROR no display available for capture")
                exit(1)
            }
            let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
            let cfg = SCStreamConfiguration()
            cfg.capturesAudio = true
            cfg.excludesCurrentProcessAudio = true
            cfg.sampleRate = 16000
            cfg.channelCount = 1
            // a video stream is mandatory; keep it as small and slow as possible
            cfg.width = 2
            cfg.height = 2
            cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            cfg.queueDepth = 3
            let tap = Tap()
            let stream = SCStream(filter: filter, configuration: cfg, delegate: tap)
            try stream.addStreamOutput(tap, type: .audio, sampleHandlerQueue: DispatchQueue(label: "glass.audio"))
            try stream.addStreamOutput(tap, type: .screen, sampleHandlerQueue: DispatchQueue(label: "glass.video"))
            try await stream.startCapture()
            Tap.log("READY")
            while true {
                try await Task.sleep(nanoseconds: 3_600_000_000_000)
            }
        } catch {
            Tap.log("ERROR \(error.localizedDescription)")
            exit(1)
        }
    }
}
