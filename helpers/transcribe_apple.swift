// On-device transcription with Apple's SpeechTranscriber (macOS 26+, Apple Silicon).
//
// Reads a 16kHz mono WAV, writes Scribe-shaped JSON (word-level start/end) to
// stdout's target path so pack_transcripts.py and render.py work unchanged.
// Built and cached by transcribe_apple.py; not meant to be run by hand.
//
//   transcribe_apple <audio.wav> <locale> <out.json>

import AVFoundation
import Foundation
import Speech

struct Word: Encodable {
    let text: String
    let start: Double
    let end: Double
    let type = "word"
    let speaker_id = "speaker_0"
    let logprob: Double?
}

struct Transcript: Encodable {
    let language_code: String
    let text: String
    let words: [Word]
    let provider = "apple_speech"
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

@main
struct TranscribeApple {
    static func main() async {
        let args = CommandLine.arguments
        guard args.count == 4 else { fail("usage: transcribe_apple <audio.wav> <locale> <out.json>") }
        let audioURL = URL(fileURLWithPath: args[1])
        let outURL = URL(fileURLWithPath: args[3])

        guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: args[2])) else {
            fail("SpeechTranscriber does not support locale \(args[2])")
        }
        let transcriber = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: [],
            attributeOptions: [.audioTimeRange, .transcriptionConfidence]
        )

        do {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                FileHandle.standardError.write(Data("downloading speech model for \(locale.identifier)…\n".utf8))
                try await request.downloadAndInstall()
            }
        } catch {
            fail("could not install speech assets: \(error)")
        }

        // Drain results concurrently: the analyzer only finishes once they are consumed.
        let collector = Task { () throws -> [Word] in
            var words: [Word] = []
            for try await result in transcriber.results {
                for run in result.text.runs {
                    guard let range = run.audioTimeRange else { continue }
                    let text = String(result.text[run.range].characters)
                        .trimmingCharacters(in: .whitespacesAndNewlines)
                    if text.isEmpty { continue }
                    let start = range.start.seconds
                    let end = range.end.seconds
                    // Attach bare punctuation to the word before it, as Scribe does.
                    if text.allSatisfy({ $0.isPunctuation }), let last = words.popLast() {
                        words.append(Word(text: last.text + text, start: last.start,
                                          end: max(last.end, end), logprob: last.logprob))
                        continue
                    }
                    let confidence = run.transcriptionConfidence.map { log(max($0, 1e-6)) }
                    words.append(Word(text: text, start: start, end: end, logprob: confidence))
                }
            }
            return words
        }

        do {
            let file = try AVAudioFile(forReading: audioURL)
            let analyzer = SpeechAnalyzer(modules: [transcriber])
            try await analyzer.start(inputAudioFile: file, finishAfterFile: true)
            let words = try await collector.value.sorted { $0.start < $1.start }
            let transcript = Transcript(
                language_code: locale.identifier,
                text: words.map(\.text).joined(separator: " "),
                words: words
            )
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .withoutEscapingSlashes]
            try encoder.encode(transcript).write(to: outURL)
            FileHandle.standardError.write(Data("\(words.count) words → \(outURL.path)\n".utf8))
        } catch {
            fail("transcription failed: \(error)")
        }
    }
}
