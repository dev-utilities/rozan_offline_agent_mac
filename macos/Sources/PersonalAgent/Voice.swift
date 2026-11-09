import AVFoundation
import Speech

/// Dictation with Apple's on-device speech recognition only: audio never leaves the Mac.
/// If on-device recognition is not available for the current language, dictation is refused.
@MainActor
final class Voice {
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var text = ""
    private(set) var listening = false
    var onState: ((Bool) -> Void)?
    var onText: ((String) -> Void)?
    var onPartial: ((String) -> Void)?
    var onError: ((String) -> Void)?

    func toggle() { listening ? stop() : start() }

    func start() {
        shellLog("voice: start requested")
        SFSpeechRecognizer.requestAuthorization { status in
            shellLog("voice: speech authorization = \(status.rawValue)")
            Task { @MainActor in
                guard status == .authorized else {
                    self.onError?("Speech recognition is not allowed. System Settings → Privacy & Security → Speech Recognition.")
                    return
                }
                AVCaptureDevice.requestAccess(for: .audio) { ok in
                    shellLog("voice: microphone access = \(ok)")
                    Task { @MainActor in
                        if ok { self.begin() } else {
                            self.onError?("Microphone access is not allowed. System Settings → Privacy & Security → Microphone.")
                        }
                    }
                }
            }
        }
    }

    private func begin() {
        guard let recognizer = SFSpeechRecognizer(), recognizer.isAvailable else {
            onError?("Speech recognition is not available right now."); return
        }
        shellLog("voice: recognizer \(recognizer.locale.identifier) onDevice=\(recognizer.supportsOnDeviceRecognition)")
        guard recognizer.supportsOnDeviceRecognition else {
            onError?("On-device speech recognition is not available for \(recognizer.locale.identifier); "
                     + "dictation stays off so audio never leaves this Mac.")
            return
        }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.requiresOnDeviceRecognition = true
        req.shouldReportPartialResults = true
        request = req
        text = ""
        let input = engine.inputNode
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) { buf, _ in req.append(buf) }
        do {
            engine.prepare()
            try engine.start()
        } catch {
            shellLog("voice: engine failed: \(error)")
            onError?("Could not start the microphone: \(error.localizedDescription)"); return
        }
        shellLog("voice: listening")
        task = recognizer.recognitionTask(with: req) { result, error in
            let best = result?.bestTranscription.formattedString
            let final = result?.isFinal ?? false
            Task { @MainActor in
                if let best { self.text = best; self.onPartial?(best) }
                if let error {
                    shellLog("voice: recognition ended: \(error.localizedDescription)")
                    if self.text.isEmpty {
                        let msg = error.localizedDescription
                        self.onError?(msg.lowercased().contains("dictation") || msg.lowercased().contains("siri")
                            ? "macOS dictation is turned off. Turn it on in System Settings → Keyboard → Dictation, "
                              + "then press ⌃⌥D again. (Recognition still runs on this Mac only.)"
                            : "Dictation stopped: \(msg)")
                    }
                }
                if final || error != nil { self.finish() }
            }
        }
        listening = true
        onState?(true)
    }

    func stop() {
        guard listening else { return }
        request?.endAudio()          // the recognizer then delivers its final result
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        // finish() runs when the final result arrives; make sure it does even if none comes
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            if self.listening { self.finish() }
        }
    }

    private func finish() {
        guard listening else { return }
        listening = false
        task?.cancel(); task = nil; request = nil
        if engine.isRunning { engine.stop(); engine.inputNode.removeTap(onBus: 0) }
        onState?(false)
        shellLog("voice: finished, \(text.count) characters")
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if !t.isEmpty { onText?(t) }
    }
}
