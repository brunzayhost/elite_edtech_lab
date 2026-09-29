"""Nextcloud artefact sync (WebDAV).

Design decisions
----------------
* Spool first, upload second: results are written to a local spool directory
  before any network call. If Nextcloud is down, nothing is lost and
  ``flush_spool()`` can be run again later (cron or at the next generation).
* Idempotent: the remote name embeds a timestamp and the git commit, and WebDAV
  PUT overwrites, so re-uploading the same file is harmless.
* Least privilege: authenticates with a Nextcloud *app password* for a
  dedicated bot user, never the admin password.
* Timeouts + retries on every request, because a dead peer must not hang the pipeline.
* UTF-8 everywhere with ``ensure_ascii=False``, so Traditional Chinese is
  stored as readable characters instead of ``\\uXXXX`` escapes.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class NextcloudSync:
    def __init__(self) -> None:
        self.base = os.environ["NEXTCLOUD_URL"].rstrip("/")
        self.user = os.environ["NEXTCLOUD_USER"]
        self.remote_dir = os.environ.get("NEXTCLOUD_DIR", "EdTechLab/lessons").strip("/")
        self.spool = Path(os.environ.get("SPOOL_DIR", "./spool"))
        self.spool.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.auth = (self.user, os.environ["NEXTCLOUD_APP_PASSWORD"])
        # TLS verification stays ON unless explicitly disabled in .env for testing.
        self.session.verify = os.getenv("NEXTCLOUD_VERIFY_TLS", "1") != "0"
        # Retry transient errors with exponential backoff. PUT and MKCOL are
        # safe to repeat, so they are allowed here (urllib3 excludes them by default).
        retry = Retry(
            total=4, backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "PUT", "MKCOL", "PROPFIND"}),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    # -- URL helpers ---------------------------------------------------------
    def _url(self, path: str) -> str:
        # quote() percent-encodes spaces/Chinese characters; "/" is preserved.
        return f"{self.base}/remote.php/dav/files/{quote(self.user)}/{quote(path)}"

    def _ensure_dirs(self, path: str) -> None:
        """Create each folder level with MKCOL. 405 means 'already exists', which is fine."""
        parts, current = path.strip("/").split("/"), ""
        for part in parts:
            current = f"{current}/{part}".strip("/")
            r = self.session.request("MKCOL", self._url(current), timeout=30)
            if r.status_code not in (201, 405):
                r.raise_for_status()

    # -- Public API ----------------------------------------------------------
    def save(self, lesson: dict[str, Any], provenance: dict[str, Any], stem: str) -> Path:
        """Write a lesson + provenance to the spool, then try to upload it.

        Returns the spool path. After a successful upload the local copy is
        removed, so the path only still exists if the upload was deferred.
        An upload failure is logged but never raised, so data is never lost.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        commit = str(provenance.get("git_commit", "unknown"))[:12]
        filename = f"{stamp}_{commit}_{stem}.json"
        local = self.spool / filename
        record = {"provenance": provenance, "lesson": lesson}
        local.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            self._upload(local)
        except requests.RequestException as exc:
            print(f"[nextcloud] upload deferred, kept in spool: {exc}")
        return local

    def flush_spool(self) -> int:
        """Upload everything still in the spool; return the number uploaded."""
        uploaded = 0
        for f in sorted(self.spool.glob("*.json")):
            try:
                self._upload(f)
                uploaded += 1
            except requests.RequestException as exc:
                print(f"[nextcloud] still failing for {f.name}: {exc}")
        return uploaded

    # -- Internals -----------------------------------------------------------
    def _upload(self, local: Path) -> None:
        self._ensure_dirs(self.remote_dir)
        with local.open("rb") as fh:
            r = self.session.put(self._url(f"{self.remote_dir}/{local.name}"), data=fh, timeout=60)
        r.raise_for_status()
        # Delete from the spool only AFTER a confirmed success, so nothing is lost.
        local.unlink()
