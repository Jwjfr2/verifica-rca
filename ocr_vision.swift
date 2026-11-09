// Apple Vision OCR helper for the RCA captcha (on-device, no model download).
// Modes:  vision_ocr <png>...      one line per image: path \t ms \t confidence \t candidates
//         vision_ocr --serve        one path per line on stdin, one result line per image
// Env:    OCR_SCALE     upscale factor before recognition (default 2)
//         OCR_LEVEL     accurate | fast (default accurate)
//         OCR_CANDIDATES  hypotheses per text observation, joined by '|' (default 1)
//         OCR_CROP      1 = crop to the ink bounding box before upscaling
//         OCR_INTERP    nearest | low | medium | high (default high)
import Foundation
import Vision
import AppKit

let env = ProcessInfo.processInfo.environment
let scale = Double(env["OCR_SCALE"] ?? "2") ?? 2
let fastMode = (env["OCR_LEVEL"] ?? "accurate") == "fast"
let nCands = max(1, Int(env["OCR_CANDIDATES"] ?? "1") ?? 1)
let doCrop = (env["OCR_CROP"] ?? "0") == "1"
let interpName = env["OCR_INTERP"] ?? "high"
let roiSpec = env["OCR_ROI"] ?? ""          // "x0,y0,x1,y1" as fractions, y from top

func interpolation(_ name: String) -> CGInterpolationQuality {
    switch name {
    case "nearest": return .none
    case "low": return .low
    case "medium": return .medium
    default: return .high
    }
}

/// Crop to the pixels that differ from the modal border colour (the glyph block).
func inkBounds(_ cg: CGImage) -> CGRect? {
    let w = cg.width, h = cg.height
    var pixels = [UInt8](repeating: 0, count: w * h * 4)
    guard let ctx = CGContext(data: &pixels, width: w, height: h, bitsPerComponent: 8,
                              bytesPerRow: w * 4, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
    ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
    func lum(_ i: Int) -> Int {
        (Int(pixels[i * 4]) * 3 + Int(pixels[i * 4 + 1]) * 6 + Int(pixels[i * 4 + 2])) / 10
    }
    // modal colour from the 4 borders
    var hist = [Int](repeating: 0, count: 32)
    for x in 0..<w { hist[lum(x) / 8] += 1; hist[lum((h - 1) * w + x) / 8] += 1 }
    for y in 0..<h { hist[lum(y * w) / 8] += 1; hist[lum(y * w + w - 1) / 8] += 1 }
    let modal = hist.firstIndex(of: hist.max()!)! * 8 + 4
    var minX = w, maxX = -1, minY = h, maxY = -1
    for y in 0..<h {
        for x in 0..<w {
            if abs(lum(y * w + x) - modal) > 48 {
                if x < minX { minX = x }; if x > maxX { maxX = x }
                if y < minY { minY = y }; if y > maxY { maxY = y }
            }
        }
    }
    if maxX < 0 { return nil }
    let pad = 3
    let x0 = max(0, minX - pad), y0 = max(0, minY - pad)
    let x1 = min(w - 1, maxX + pad), y1 = min(h - 1, maxY + pad)
    return CGRect(x: CGFloat(x0), y: CGFloat(h - 1 - y1), width: CGFloat(x1 - x0 + 1), height: CGFloat(y1 - y0 + 1))
}

func scan(_ path: String) -> String {
    guard let img = NSImage(contentsOfFile: path),
          var cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        return "\(path)\tERR\t0.000\t"
    }
    if doCrop, let box = inkBounds(cg), let cropped = cg.cropping(to: box) {
        cg = cropped
    }
    if scale > 1 {
        let w = Int(Double(cg.width) * scale), h = Int(Double(cg.height) * scale)
        if let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                               space: CGColorSpaceCreateDeviceRGB(),
                               bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) {
            ctx.interpolationQuality = interpolation(interpName)
            ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
            if let scaled = ctx.makeImage() { cg = scaled }
        }
    }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = fastMode ? .fast : .accurate
    req.usesLanguageCorrection = false
    req.recognitionLanguages = ["en-US"]
    if !roiSpec.isEmpty {
        let f = roiSpec.split(separator: ",").compactMap { Double($0) }
        if f.count == 4 {
            req.regionOfInterest = CGRect(x: f[0], y: 1 - f[3], width: f[2] - f[0], height: f[3] - f[1])
        }
    }
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    let t0 = Date()
    do {
        try handler.perform([req])
    } catch {
        return "\(path)\tERR\t0.000\t"
    }
    let ms = Date().timeIntervalSince(t0) * 1000
    let obs = req.results ?? []
    var texts: [String] = []
    var confs: [Double] = []
    for k in 0..<nCands {
        var parts: [String] = []
        var cs: [Double] = []
        for o in obs {
            let all = o.topCandidates(nCands)
            if k < all.count {
                parts.append(all[k].string)
                cs.append(Double(all[k].confidence))
            }
        }
        texts.append(parts.joined(separator: " "))
        confs.append(cs.isEmpty ? 0 : cs.reduce(0, +) / Double(cs.count))
    }
    let text = texts.joined(separator: "|")
    let conf = confs.first ?? 0
    return "\(path)\t\(String(format: "%.1f", ms))\t\(String(format: "%.3f", conf))\t\(text)"
}

func emit(_ s: String) {
    print(s)
    fflush(stdout)
}

let args = Array(CommandLine.arguments.dropFirst())
if args.contains("--serve") {
    while let line = readLine(strippingNewline: true) {
        let p = line.trimmingCharacters(in: .whitespaces)
        if !p.isEmpty { emit(scan(p)) }
    }
    exit(0)
}
for path in args {
    emit(scan(path))
}
