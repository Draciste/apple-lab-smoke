import Foundation
import Metal

func emit(_ object: [String: Any]) {
    guard let bytes = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
          let line = String(data: bytes, encoding: .utf8) else { print("{\"state\":\"serialization-failed\"}"); return }
    print(line)
}

guard let device = MTLCreateSystemDefaultDevice() else {
    emit(["state": "unsupported", "reason": "MTLCreateSystemDefaultDevice returned nil"])
    exit(0)
}
var report: [String: Any] = ["state": "incomplete", "device": [
    "name": device.name, "lowPower": device.isLowPower,
    "headless": device.isHeadless, "removable": device.isRemovable,
    "unifiedMemory": device.hasUnifiedMemory
]]
let source = """
#include <metal_stdlib>
using namespace metal;
kernel void audit(device uint *data [[buffer(0)]], uint id [[thread_position_in_grid]]) {
    data[id] = data[id] * data[id] + 7;
}
"""
do {
    let library = try device.makeLibrary(source: source, options: nil)
    guard let function = library.makeFunction(name: "audit"),
          let queue = device.makeCommandQueue(),
          let buffer = device.makeBuffer(length: 4 * MemoryLayout<UInt32>.size, options: .storageModeShared),
          let command = queue.makeCommandBuffer(), let encoder = command.makeComputeCommandEncoder() else {
        throw NSError(domain: "MetalAudit", code: 1, userInfo: [NSLocalizedDescriptionKey: "Metal resource creation failed"])
    }
    let pipeline = try device.makeComputePipelineState(function: function)
    let ptr = buffer.contents().bindMemory(to: UInt32.self, capacity: 4)
    for (index, value) in [UInt32(1), 2, 3, 4].enumerated() { ptr[index] = value }
    encoder.setComputePipelineState(pipeline)
    encoder.setBuffer(buffer, offset: 0, index: 0)
    encoder.dispatchThreads(MTLSize(width: 4, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: 4, height: 1, depth: 1))
    encoder.endEncoding()
    command.commit()
    command.waitUntilCompleted()
    let actual = (0..<4).map { Int(ptr[$0]) }
    report["commandStatus"] = command.status.rawValue
    report["values"] = actual
    report["state"] = command.status == .completed && actual == [8, 11, 16, 23] ? "passed" : "failed"
    if let error = command.error { report["error"] = error.localizedDescription }
} catch {
    report["state"] = "failed"
    report["error"] = error.localizedDescription
}
emit(report)
