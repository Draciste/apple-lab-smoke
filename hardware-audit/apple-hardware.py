#!/usr/bin/env python3
"""Small public-safe native macOS hardware audit. No user or browser profiles."""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
result = {"scope": "Synthetic macOS hardware and dependency audit", "platform": platform.system(), "observations": {}}

def clean(value, private_dir=None, limit=12000):
    value = value.replace(str(Path.home()), "<home>")
    if private_dir:
        value = value.replace(str(private_dir), "<temporary>")
    # Do not include arbitrary environment variables, command arguments or logs.
    return value[:limit]

def run(args, timeout=20, private_dir=None, limit=12000):
    try:
        p = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, errors="replace", timeout=timeout)
        return {"code": p.returncode, "stdout": clean(p.stdout, private_dir, limit), "stderr": clean(p.stderr, private_dir, limit)}
    except subprocess.TimeoutExpired:
        return {"state": "timeout", "seconds": timeout}
    except OSError as e:
        return {"state": "unavailable", "error": str(e.errno)}

def filtered_profiler():
    p = run(["system_profiler", "-json", "SPHardwareDataType", "SPDisplaysDataType"], timeout=40, limit=200000)
    if p.get("code") != 0:
        return {"state": "unavailable", "code": p.get("code"), "reason": p.get("state", "profiler failed")}
    try:
        raw = json.loads(p["stdout"])
        allowed = {"_name", "machine_model", "machine_name", "chip_type", "number_processors", "number_cores", "physical_memory", "sppci_model", "spdisplays_vendor", "spdisplays_device_type", "spdisplays_vram", "spdisplays_vram_shared", "spdisplays_metal", "spdisplays_resolution", "spdisplays_pixels", "spdisplays_connection_type", "spdisplays_ndrvs"}
        def filter_value(item):
            if isinstance(item, list):
                return [filter_value(x) for x in item][:24]
            if isinstance(item, dict):
                return {k: filter_value(v) for k, v in item.items() if k in allowed}
            return item
        return {k: filter_value(v) for k, v in raw.items() if k in ("SPHardwareDataType", "SPDisplaysDataType")}
    except (ValueError, TypeError) as e:
        return {"state": "unavailable", "reason": "could not parse bounded profiler JSON", "error": type(e).__name__}

try:
    if sys.platform != "darwin":
        raise RuntimeError("This probe requires macOS")
    o = result["observations"]
    o["os"] = {"sw_vers": run(["sw_vers"]), "uname": run(["uname", "-srm"]), "architecture": platform.machine()}
    o["cpu"] = {key: run(["sysctl", "-n", key]) for key in ("machdep.cpu.brand_string", "machdep.cpu.features", "machdep.cpu.leaf7_features", "hw.optional.arm64", "hw.ncpu")}
    o["systemProfiler"] = filtered_profiler()
    o["versions"] = {"node": run(["node", "--version"]), "ffmpeg": run(["ffmpeg", "-version"]), "ffprobe": run(["ffprobe", "-version"]), "clang": run(["clang", "--version"]), "sdk": run(["xcrun", "--show-sdk-version"]), "swift": run(["swiftc", "--version"])}
    # otool lists load commands, not evidence that files in the dyld shared cache are absent.
    selected = {name: shutil.which(name) for name in ("node", "ffmpeg", "ffprobe")}
    o["dependencies"] = {name: (run(["otool", "-L", binary]) if binary else {"state": "unavailable"}) for name, binary in selected.items()}
    o["dependenciesNote"] = "otool lists install names; dyld shared-cache names need not exist as files. This is not a runtime missing-library diagnosis."
    with tempfile.TemporaryDirectory(prefix="apple-hw-") as temp:
        t = Path(temp)
        binary = t / "metal-audit"
        build = run(["swiftc", str(HERE / "apple-hardware.swift"), "-framework", "Metal", "-o", str(binary)], timeout=90, private_dir=t)
        o["metal"] = {"build": build}
        if build.get("code") == 0:
            probe = run([str(binary)], timeout=30, private_dir=t)
            if probe.get("code") == 0:
                try:
                    o["metal"]["actual"] = json.loads(probe["stdout"])
                except ValueError:
                    o["metal"]["actual"] = {"state": "invalid-output"}
            else:
                o["metal"]["actual"] = probe
        out = t / "videotoolbox.mp4"
        encode = run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=2:duration=1", "-an", "-c:v", "h264_videotoolbox", "-allow_sw", "0", "-y", str(out)], timeout=45, private_dir=t)
        vt = {"encode": encode, "scope": "H.264 VideoToolbox, hardware-only requested; no hardware decode assertion"}
        if encode.get("code") == 0 and out.is_file() and out.stat().st_size:
            vt["fileBytes"] = out.stat().st_size
            vt["probe"] = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height,nb_frames", "-of", "json", str(out)], private_dir=t)
            vt["decode"] = run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(out), "-f", "null", "-"], timeout=25, private_dir=t)
            vt["note"] = "Successful allow_sw=0 encode is bounded FFmpeg hardware-request evidence; hardware encoder property not independently queried. Decode may be software."
        else:
            vt["note"] = "Hardware-request encode unavailable or failed; no software fallback counted as success."
        o["videoToolbox"] = vt
    result["state"] = "observed"
except Exception as e:
    result["state"] = "incomplete"
    result["error"] = clean(str(e))
print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
if result["state"] != "observed":
    sys.exit(1)
