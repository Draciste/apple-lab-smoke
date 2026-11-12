#!/usr/bin/env python3
"""Install explicitly described signed browser apps into a private macOS directory.

Manifest: {"channels": [{"id": "chrome-beta", "url": "https://...",
 "format": "dmg", "app_name": "Google Chrome Beta.app",
 "team_id": "EQHXZ8M8AV", "bundle_id": "com.google.Chrome.beta",
 "version": "optional CFBundleShortVersionString"}]}.

Run on macOS only. This does not execute the browsers or install system packages.
Safari Technology Preview's system installer is deliberately not run.
"""
import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

SYSTEM_APPS = ["Google Chrome.app", "Firefox.app", "Opera.app", "Microsoft Edge.app", "Safari.app"]


def run(argv, timeout=120, check=True):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{argv[0]} failed ({result.returncode}): {result.stderr[-2000:]}")
    return result


def metadata(app):
    with (app / "Contents" / "Info.plist").open("rb") as f:
        data = plistlib.load(f)
    return {"bundle_id": data.get("CFBundleIdentifier"),
            "version": data.get("CFBundleShortVersionString"),
            "build": data.get("CFBundleVersion"),
            "executable": data.get("CFBundleExecutable")}


def inspect(app, expected):
    info = metadata(app)
    if info["bundle_id"] != expected["bundle_id"]:
        raise RuntimeError(f"bundle ID mismatch: {info['bundle_id']!r}")
    if expected.get("version") and str(info["version"]) != str(expected["version"]):
        raise RuntimeError(f"version mismatch: {info['version']!r}")
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], timeout=300)
    sig = run(["/usr/bin/codesign", "-dv", "--verbose=4", str(app)])
    match = re.search(r"^TeamIdentifier=(.+)$", sig.stderr, re.MULTILINE)
    if not match or match.group(1).strip() != expected["team_id"]:
        raise RuntimeError("signing team mismatch or unavailable")
    run(["/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=2", str(app)], timeout=300)
    return info


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise RuntimeError("refusing non-HTTPS redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, dest):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("download URL must be HTTPS without credentials")
    opener = urllib.request.build_opener(HTTPSOnly())
    req = urllib.request.Request(url, headers={"User-Agent": "AppleLab-browser-channels/1"})
    with opener.open(req, timeout=90) as response, dest.open("xb") as out:
        if urllib.parse.urlsplit(response.geturl()).scheme.lower() != "https":
            raise RuntimeError("download did not end on HTTPS")
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 4 * 1024**3:
                raise RuntimeError("download exceeds 4 GiB")
            out.write(chunk)


def find_app(root, name):
    # Do not follow symlink directories in untrusted images/packages.
    matches = []
    for base, dirs, _ in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
        for d in dirs:
            if d == name:
                matches.append(Path(base) / d)
        dirs[:] = [d for d in dirs if not d.endswith('.app')]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one {name!r}, found {len(matches)}")
    return matches[0]


def validate(c):
    for key in ("id", "url", "format", "app_name", "team_id", "bundle_id"):
        if not isinstance(c.get(key), str) or not c[key]:
            raise ValueError(f"missing or invalid {key}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", c["id"]):
        raise ValueError("invalid channel id")
    if Path(c["app_name"]).name != c["app_name"] or not c["app_name"].endswith('.app'):
        raise ValueError("invalid app name")
    if not re.fullmatch(r"[A-Z0-9]{10}", c["team_id"]):
        raise ValueError("invalid team ID")
    if c["format"] not in ("dmg", "pkg"):
        raise ValueError("unsupported package format")
    if "sha256" in c and (not isinstance(c["sha256"], str) or
                          not re.fullmatch(r"[0-9a-fA-F]{64}", c["sha256"])):
        raise ValueError("invalid SHA-256")
    if "existing_system_app" in c:
        name = c["existing_system_app"]
        if (not isinstance(name, str) or Path(name).name != name or
                not name.endswith(".app") or not c.get("version")):
            raise ValueError("existing_system_app requires an app name and exact version")
    if "safari" in c["id"].lower() or "safari" in c["app_name"].lower():
        raise ValueError("Safari packages require separate supported system-installer review")


def install(c, root):
    validate(c)
    if c.get("existing_system_app"):
        existing = Path("/Applications") / c["existing_system_app"]
        if existing.exists():
            info = metadata(existing)
            if (info["bundle_id"] == c["bundle_id"] and
                    str(info["version"]) == str(c["version"])):
                return {"state": "system_existing_verified", "path": str(existing),
                        **inspect(existing, c)}
    target_dir = root / c["id"]
    target = target_dir / c["app_name"]
    if target.exists():
        return {"state": "existing_verified", "path": str(target), **inspect(target, c)}
    if target_dir.exists():
        raise RuntimeError("channel directory exists without expected app; manual reconciliation required")
    stage = Path(tempfile.mkdtemp(prefix=".browser-stage-", dir=root))
    cleanup_safe = True
    try:
        archive = stage / ("download." + c["format"])
        download(c["url"], archive)
        if c.get("sha256"):
            digest = hashlib.sha256()
            with archive.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != c["sha256"].lower():
                raise RuntimeError("download SHA-256 mismatch")
        mount = None
        try:
            if c["format"] == "dmg":
                mount = stage / "mount"
                mount.mkdir()
                run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen",
                     "-mountpoint", str(mount), str(archive)], timeout=300)
                source = find_app(mount, c["app_name"])
            else:
                signature = run(["/usr/sbin/pkgutil", "--check-signature", str(archive)], timeout=300)
                if c["team_id"] not in signature.stdout:
                    raise RuntimeError("package signature output does not contain expected team ID")
                expanded = stage / "expanded"
                run(["/usr/sbin/pkgutil", "--expand-full", str(archive), str(expanded)], timeout=600)
                source = find_app(expanded, c["app_name"])
            # Copy first; verification occurs on exactly the bundle that will be retained.
            staged_app = stage / c["app_name"]
            run(["/usr/bin/ditto", "--norsrc", str(source), str(staged_app)], timeout=600)
            info = inspect(staged_app, c)
            ready = stage / "ready"
            ready.mkdir()
            os.rename(staged_app, ready / c["app_name"])
            os.rename(ready, target_dir)
            return {"state": "installed_verified", "path": str(target), **info}
        finally:
            if mount is not None:
                try:
                    result = run(["/usr/bin/hdiutil", "detach", str(mount)], timeout=120, check=False)
                    if result.returncode:
                        cleanup_safe = False
                        raise RuntimeError(f"could not detach {mount}: {result.stderr[-1000:]}")
                except Exception:
                    cleanup_safe = False
                    raise
    finally:
        if cleanup_safe:
            shutil.rmtree(stage)
        else:
            print(f"WARNING: preserved staging directory {stage} for mount reconciliation", file=sys.stderr)


def write_receipt(path, receipt):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--root", type=Path, default=Path.home() / "AppleLab" / "browser-channels")
    ap.add_argument("--receipt", type=Path, required=True)
    args = ap.parse_args()
    if sys.platform != "darwin":
        ap.error("this helper must run natively on macOS")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    channels = manifest["channels"]
    if not isinstance(channels, list):
        ap.error("channels must be a list")
    ids = [c.get("id") for c in channels]
    if len(ids) != len(set(ids)):
        ap.error("duplicate channel ids")
    args.root.mkdir(parents=True, exist_ok=True)
    receipt = {"system_inventory": {}, "channels": {}}
    for name in SYSTEM_APPS:
        path = Path('/Applications') / name
        try:
            receipt["system_inventory"][name] = ({"path": str(path), **metadata(path)} if path.exists()
                                                  else {"state": "absent"})
        except Exception as e:
            receipt["system_inventory"][name] = {"path": str(path), "error": str(e)}
    write_receipt(args.receipt, receipt)
    failed = False
    for channel in channels:
        name = channel.get("id", "<missing>")
        try:
            receipt["channels"][name] = install(channel, args.root)
        except Exception as e:
            failed = True
            receipt["channels"][name] = {"state": "failed", "error": str(e)}
        write_receipt(args.receipt, receipt)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
