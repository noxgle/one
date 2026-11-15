#!/usr/bin/env python3
"""Build documentation locally and upload static files; never execute .env content."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

KEYS = {"FTP_HOST", "FTP_USER", "FTP_PASSWORD", "FTP_DIR", "FTP_TLS", "FTP_ALLOW_SELF_SIGNED", "PUBLIC_SITE_URL", "DOCS_BASE"}


def load_config(path: Path) -> dict[str, str]:
    config = {key: os.environ[key] for key in KEYS if key in os.environ}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not sep or key not in KEYS:
                raise ValueError("Invalid or unsupported configuration key in deployment env file")
            if value.startswith(("'", '"')):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError("Unclosed quoted deployment value")
                value = value[1:-1]
            config[key] = value
    if any(any(ord(c) < 32 or ord(c) == 127 for c in value) for value in config.values()):
        raise ValueError("Control characters are not allowed in deployment configuration")
    return config


def remote_path(value: str, *, allow_root: bool = False) -> str:
    if not value.startswith("/") or any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("Remote paths must be absolute and cannot contain dot segments")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]*", value) or "//" in value:
        raise ValueError("Remote path contains unsupported characters")
    result = value.rstrip("/")
    if not result and not allow_root:
        raise ValueError("Documentation base cannot be the landing root")
    return result


def files_in(root: Path, *, recursive: bool) -> list[Path]:
    files = []
    for path in (root.rglob("*") if recursive else root.iterdir()):
        if path.is_symlink():
            raise ValueError("Refusing to upload symbolic links")
        if path.is_file():
            parts = path.relative_to(root).parts
            if any(part.startswith(".") for part in parts):
                continue
            if recursive or path.suffix.lower() in {".html", ".css", ".js", ".png", ".jpg", ".svg", ".ico"}:
                files.append(path)
    if root / "index.html" not in files:
        raise ValueError("Static output is missing index.html")
    # Assets first, nested HTML second, entry page last. Not an atomic deployment.
    return sorted(files, key=lambda p: (p == root / "index.html", p.suffix == ".html", p.as_posix()))


def curl_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def deploy(root: Path, argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument("--landing", action="store_true", help="Upload landing only (default)")
    targets.add_argument("--docs", action="store_true", help="Build locally and upload documentation only")
    targets.add_argument("--all", action="store_true", help="Prepare both before uploading either")
    parser.add_argument("--dry-run", action="store_true", help="Build and list files; never contact deployment server")
    parser.add_argument("--env-file", type=Path, help="Deployment configuration (default: landing_page/.env)")
    parser.add_argument("--allow-self-signed", action="store_true",
                        help="Skip FTPS certificate verification; TLS remains required (MITM risk)")
    args = parser.parse_args(argv)
    config = load_config(args.env_file or root / "landing_page/.env")
    host = config.get("FTP_HOST", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?", host):
        raise ValueError("FTP_HOST must be a hostname with optional port, without scheme or credentials")
    directory = remote_path(config.get("FTP_DIR", ""), allow_root=True)
    tls = config.get("FTP_TLS", "true")
    if tls not in {"true", "false"}:
        raise ValueError("FTP_TLS must be true or false")
    allow_setting = config.get("FTP_ALLOW_SELF_SIGNED", "false")
    if allow_setting not in {"true", "false"}:
        raise ValueError("FTP_ALLOW_SELF_SIGNED must be true or false")
    allow_self_signed = args.allow_self_signed or allow_setting == "true"
    if allow_self_signed and tls != "true":
        raise ValueError("Self-signed certificate mode requires FTP_TLS=true")
    if allow_self_signed:
        print("WARNING: FTPS certificate verification disabled; TLS encrypts traffic but server identity "
              "is not verified (MITM risk).", file=sys.stderr)
    if not args.dry_run and not all(config.get(key) for key in ("FTP_USER", "FTP_PASSWORD")):
        raise ValueError("FTP_USER and FTP_PASSWORD are required for upload")
    if ":" in config.get("FTP_USER", ""):
        raise ValueError("FTP_USER cannot contain a colon")
    if tls == "false":
        print("WARNING: FTP_TLS=false explicitly permits unencrypted credentials and uploads.", file=sys.stderr)
    uploads: list[tuple[Path, str]] = []
    if args.docs or args.all:
        base = remote_path(config.get("DOCS_BASE", "/docs/")) + "/"
        site = config.get("PUBLIC_SITE_URL", "")
        url = urlsplit(site)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path not in {"", "/"} or url.query or url.fragment):
            raise ValueError("PUBLIC_SITE_URL must be an HTTPS origin, e.g. https://example.org")
        website = root / "website"
        env = dict(os.environ, DOCS_SITE=site, DOCS_BASE=base)
        subprocess.run(["npm", "ci"], cwd=website, env=env, check=True)
        subprocess.run(["npm", "run", "build"], cwd=website, env=env, check=True)
        subprocess.run(["npm", "run", "check:links"], cwd=website, env=env, check=True)
        dist = website / "dist"
        uploads.extend((path, directory + base + path.relative_to(dist).as_posix())
                       for path in files_in(dist, recursive=True))
    if args.all or not args.docs:
        landing = root / "landing_page"
        uploads.extend((path, directory + "/" + path.name) for path in files_in(landing, recursive=False))
    for path, destination in uploads:
        print(f"{'Would upload' if args.dry_run else 'Uploading'}: {path.relative_to(root)} -> {destination}", flush=True)
        if args.dry_run:
            continue
        # Secrets go through stdin, not argv, URL, logs, or a persistent config file.
        options = {"user": config["FTP_USER"] + ":" + config["FTP_PASSWORD"],
                   "upload-file": str(path), "url": "ftp://" + host + quote(destination, safe="/")}
        curl_config = "\n".join(f"{key} = {curl_value(value)}" for key, value in options.items()) + "\n"
        command = ["curl", "--disable", "--config", "-", "--silent", "--fail", "--ftp-create-dirs",
                   "--connect-timeout", "20", "--max-time", "300"]
        if tls == "true":
            command.append("--ssl-reqd")
        if allow_self_signed:
            command.append("--insecure")
        # Suppress transport diagnostics which may repeat URL/config data.
        result = subprocess.run(command, input=curl_config, text=True, capture_output=True)
        if result.returncode:
            raise ValueError(f"Upload failed (curl exit {result.returncode}); deployment may be partial")
    print("Dry run complete; deployment server was not contacted." if args.dry_run else "Deployment complete.")


if __name__ == "__main__":
    try:
        deploy(Path(__file__).resolve().parents[1])
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        # Do not print exception strings from external tools or config parsing.
        message = str(exc) if isinstance(exc, ValueError) else "Local build or deployment command failed"
        print(f"Error: {message}", file=sys.stderr)
        sys.exit(1)
