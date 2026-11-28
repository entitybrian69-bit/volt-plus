#!/usr/bin/env python3
"""Build the GitHub Pages `_site` directory with all 3 direct Release APK downloads."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request

DISALLOWED = re.compile(
    r"(debug|dev|pre[-_.]?release|alpha|beta|rc\d*|nightly|snapshot|canary|unsigned)",
    re.I,
)
TARGET_NAMES = {
    "arm64": "Volt-Plus-arm64-v8a.apk",
    "armv7": "Volt-Plus-armeabi-v7a.apk",
    "universal": "Volt-Plus-universal.apk",
}


def api_get(url: str, token: str):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "VoltPlus-Pages-Sync",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_file(url: str, dest: Path, token: str) -> None:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/octet-stream",
            "User-Agent": "VoltPlus-Pages-Sync",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def match_abi(name: str):
    lower = name.lower()
    if not lower.endswith(".apk") or DISALLOWED.search(lower):
        return None
    if "universal" in lower:
        return "universal"
    if "arm64" in lower or "aarch64" in lower:
        return "arm64"
    if "armeabi-v7a" in lower or "armv7" in lower or "armeabi" in lower:
        return "armv7"
    return None


def populate_from_run(run_id: str, repo: str, out_dir: Path, manifest: dict, run_meta: dict) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / str(run_id)
        res = subprocess.run(
            ["gh", "run", "download", str(run_id), "--repo", repo, "--dir", str(run_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if res.returncode != 0:
            return False
        found_files = {}
        for build_json in run_dir.rglob("BUILD.json"):
            meta = json.loads(build_json.read_text(encoding="utf-8"))
            if meta.get("profile") != "release":
                return False
        for apk in run_dir.rglob("*.apk"):
            abi = match_abi(apk.name)
            if abi and abi not in found_files:
                found_files[abi] = apk
        if len(found_files) != 3:
            return False
        sha = run_meta.get("headSha") or os.environ.get("GITHUB_SHA", "")
        sha_short = sha[:7]
        run_number = run_meta.get("runNumber") or os.environ.get("GITHUB_RUN_NUMBER", "")
        tag_suffix = f"#{run_number}" if run_number else sha_short
        manifest.update(
            {
                "status": "ready",
                "tag_name": f"v1.0 ({tag_suffix})" if tag_suffix else "v1.0",
                "version": "1.0",
                "name": "Volt Plus Release 1.0",
                "published_at": run_meta.get("createdAt") or "",
                "commit": sha,
                "notes": (
                    f"Latest signed Release build (commit {sha_short or 'latest'}). "
                    "Official Release channel — no debug, no pre-release."
                ),
            }
        )
        for abi, apk in found_files.items():
            dest = out_dir / TARGET_NAMES[abi]
            shutil.copyfile(apk, dest)
            data = dest.read_bytes()
            manifest["assets"][abi] = {
                "filename": TARGET_NAMES[abi],
                "url": f"./downloads/{TARGET_NAMES[abi]}",
                "size_bytes": len(data),
                "size_mb": f"{len(data) / (1024 * 1024):.1f} MB",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", help="Exact Volt Plus APK workflow run ID to bundle")
    parser.add_argument("--site", type=Path, default=Path("_site"))
    args = parser.parse_args()

    token = os.environ["GH_TOKEN"]
    site = args.site
    if site.exists():
        shutil.rmtree(site)
    downloads = site / "downloads"
    media = site / "assets" / "media"
    shutil.copytree(Path("website"), site)
    downloads.mkdir(parents=True, exist_ok=True)
    media.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path("packaging/android/media/intro.mp4"), media / "intro.mp4")
    (site / ".nojekyll").write_text("", encoding="utf-8")

    manifest = {
        "status": "pending_deploy",
        "tag_name": "v1.0",
        "version": "1.0",
        "name": "Volt Plus Release",
        "published_at": "",
        "notes": "Official signed Release APK builds (64-bit arm64-v8a, 32-bit armeabi-v7a, and Universal).",
        "assets": {},
    }

    bundled = False
    if args.run_id:
        run_info = api_get(f"https://api.github.com/repos/{args.repo}/actions/runs/{args.run_id}", token)
        bundled = populate_from_run(
            args.run_id,
            args.repo,
            downloads,
            manifest,
            {
                "headSha": run_info.get("head_sha", ""),
                "createdAt": run_info.get("created_at") or run_info.get("updated_at", ""),
                "runNumber": str(run_info.get("run_number", "")),
            },
        )

    if not bundled:
        releases = api_get(f"https://api.github.com/repos/{args.repo}/releases?per_page=30", token)
        selected_release = None
        selected_assets = {}
        for rel in releases:
            if rel.get("draft") or rel.get("prerelease"):
                continue
            tag_text = f"{rel.get('tag_name', '')} {rel.get('name', '')}"
            if DISALLOWED.search(tag_text):
                continue
            found = {}
            for asset in rel.get("assets", []):
                abi = match_abi(asset.get("name", ""))
                if abi and abi not in found:
                    found[abi] = asset
            if len(found) == 3:
                selected_release = rel
                selected_assets = found
                break

        if selected_release is not None:
            tag = selected_release.get("tag_name", "v1.0").strip()
            manifest.update(
                {
                    "status": "ready",
                    "tag_name": tag,
                    "version": tag.lstrip("vV"),
                    "name": selected_release.get("name") or tag,
                    "published_at": selected_release.get("published_at") or "",
                    "notes": selected_release.get("body") or manifest["notes"],
                }
            )
            for abi, asset in selected_assets.items():
                dest = downloads / TARGET_NAMES[abi]
                print(f"Downloading release asset {asset['name']} -> {dest}", flush=True)
                download_file(asset["url"], dest, token)
                data = dest.read_bytes()
                manifest["assets"][abi] = {
                    "filename": TARGET_NAMES[abi],
                    "url": f"./downloads/{TARGET_NAMES[abi]}",
                    "size_bytes": len(data),
                    "size_mb": f"{len(data) / (1024 * 1024):.1f} MB",
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            bundled = True

    if not bundled:
        runs = json.loads(
            subprocess.check_output(
                [
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    args.repo,
                    "--workflow",
                    "volt-plus.yml",
                    "--status",
                    "success",
                    "--limit",
                    "15",
                    "--json",
                    "databaseId,createdAt,headSha,number",
                ],
                text=True,
            )
        )
        for run in runs:
            if populate_from_run(
                str(run["databaseId"]),
                args.repo,
                downloads,
                manifest,
                {
                    "headSha": run.get("headSha", ""),
                    "createdAt": run.get("createdAt", ""),
                    "runNumber": str(run.get("number", "")),
                },
            ):
                bundled = True
                break

    if not bundled:
        raise SystemExit("Failed to bundle all 3 release APKs (arm64, armv7, universal)")

    (site / "latest-release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        "Prepared latest-release.json:",
        json.dumps({k: v for k, v in manifest.items() if k != "notes"}, indent=2),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
