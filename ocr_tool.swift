import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count > 1 else {
    fputs("Usage: ocr_tool <image_path>\n", stderr)
    exit(1)
}

let imagePath = CommandLine.arguments[1]
let imageURL = URL(fileURLWithPath: imagePath)

guard let nsImage = NSImage(contentsOf: imageURL),
      let cgImage = nsImage.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fputs("Failed to load image: \(imagePath)\n", stderr)
    exit(2)
}

let requestHandler = VNImageRequestHandler(cgImage: cgImage, options: [:])
let request = VNRecognizeTextRequest { req, error in
    if let error = error {
        fputs("OCR error: \(error.localizedDescription)\n", stderr)
        exit(3)
    }
    guard let observations = req.results as? [VNRecognizedTextObservation] else { return }
    for obs in observations {
        if let candidate = obs.topCandidates(1).first {
            let y = obs.boundingBox.minY
            print(String(format: "%.3f|%@", y, candidate.string))
        }
    }
}

request.recognitionLevel = .accurate
request.usesLanguageCorrection = false

do {
    try requestHandler.perform([request])
} catch {
    fputs("Perform error: \(error.localizedDescription)\n", stderr)
    exit(4)
}
