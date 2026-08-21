"""One sync/download engine for per-course Content Topic pipelines.

Owns the entire per-course pipeline — TOC walk, manifest reconciliation,
topic download, SHA-256 dedup, assignment hooks, single save — for every
mode:

    SYNC      incremental: skip topics whose manifest ``last_modified``
              matches the TOC's ``LastModifiedDate``
    DOWNLOAD  re-download every matching topic; unrelated manifest entries
              (other topics, assignment attachments) are preserved
    FORCE     wipe the whole manifest first, then behave like DOWNLOAD
    PLAN      ``--dry-run``: walk the TOC and decide, but perform NO
              file-body fetches, NO filesystem writes and NO manifest
              mutation (``FORCE`` + ``PLAN`` therefore deletes nothing)

The engine emits no human output to stdout; assignment-phase failures log
to stderr outside the topic pipeline (assignments.py).  Every outcome —
including warnings — is returned as data for the caller to render.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from .api import LighthouseClient
from .assignments import assignment_key, download_for_course, sync_for_course
from .manifest import MANIFEST_FILENAME, Manifest, ManifestCorruptError, compute_sha256
from .utils import _sanitize_filename, get_course_name, resolve_course_folder_name


class Mode(Enum):
    """Explicit pipeline mode for :func:`run_course`."""

    SYNC = "sync"
    DOWNLOAD = "download"
    FORCE = "force"
    PLAN = "plan"

def build_entry(tid: str, name: str, path: str, content_or_entry: bytes | dict, sha: str = "") -> dict:
    """Build a sync/download entry dict. content_or_entry is bytes (content) or dict (manifest entry)."""
    size = len(content_or_entry) if isinstance(content_or_entry, bytes) else content_or_entry.get("size", 0)
    if not sha and isinstance(content_or_entry, bytes):
        sha = compute_sha256(content_or_entry)
    return {"topic_id": tid, "filename": name, "path": path, "size": size, "size_kb": round(size / 1024, 1), "sha256": sha, **({"extension": Path(name).suffix.lower()} if name and "." in name else {})}


def fetch_toc_and_name(client: LighthouseClient, org_id: int) -> tuple[dict, str]:
    """Fetch content TOC and course name. Raises on failure."""
    return client.get_content_toc(org_id), get_course_name(client, org_id)


def download_and_persist_topic(
    client: LighthouseClient,
    org_id: int,
    topic: dict,
    dest: Path,
    manifest: Manifest,
) -> tuple[bytes, str, Path]:
    """Download a topic, write to disk, update manifest. Returns (content, name, path)."""
    tid = topic.get("topic_id")
    if tid is None:
        raise ValueError(f"Topic missing 'topic_id': {topic.get('title', 'unknown')}")
    tid = str(tid)
    if topic.get("type", "").lower() == "html":
        content, sanitized_name = client.get_topic_html(org_id, int(tid))
    else:
        content, filename = client.download_topic_file(org_id, int(tid))
        sanitized_name = _sanitize_filename(filename)
    file_dest = dest / Path(topic["path"]).parent
    # Defense in depth: module/topic titles are sanitized per component at
    # flatten time, but never trust assembled paths — clamp anything that
    # resolves outside the course root.
    if not file_dest.resolve().is_relative_to(dest.resolve()):
        file_dest = dest
    file_dest.mkdir(parents=True, exist_ok=True)
    filepath = file_dest / sanitized_name
    filepath.write_bytes(content)
    manifest.add_entry(tid, content=content, filename=sanitized_name, last_modified=topic.get("last_modified") or "")
    return content, sanitized_name, filepath


def parse_type_filter(types: str) -> tuple[set[str], list[str]]:
    """Parse a comma-separated content-type filter string into a validated set.

    Accepts "file", "html", or comma-separated combos. Returns
    ``(valid_set, unknown_values)`` so the caller can record unknown
    values as warnings. Falls back to ``{"file"}`` when nothing valid
    remains.
    """
    valid, raw = {"file", "html"}, {t.strip().lower() for t in types.split(",")}
    return (raw & valid) or {"file"}, sorted(raw - valid)


def flatten_all_topics(modules: list[dict], prefix: str = "") -> list[dict[str, Any]]:
    """Collect all downloadable topics from the content TOC.

    Returns list of {topic_id, title, url, type, path, last_modified}.
    """
    topics: list[dict[str, Any]] = []
    for mod in modules:
        safe_prefix = _sanitize_filename(mod.get("Title", ""))
        new_prefix = f"{prefix}/{safe_prefix}" if prefix else safe_prefix
        topics.extend(flatten_all_topics(mod.get("Modules", []), new_prefix))
        for topic in mod.get("Topics", []):
            topics.append({
                "topic_id": topic.get("TopicId"), "title": topic.get("Title", ""),
                "url": topic.get("Url"), "type": topic.get("TypeIdentifier", ""),
                "path": f"{new_prefix}/{_sanitize_filename(topic.get('Title', ''))}",
                "last_modified": topic.get("LastModifiedDate", ""),
            })
    return topics


def filter_topics_by_type(modules: list[dict], type_set: set[str]) -> list[dict]:
    """Flatten topic tree and keep only topics matching *type_set*.

    Returns a list of topic dicts whose ``type`` (lowercased) is present
    in *type_set* (e.g. ``{"file"}`` or ``{"file", "html"}``).
    """
    return [
        t for t in flatten_all_topics(modules) if t.get("type", "").lower() in type_set
    ]


def _empty_result(org_id: int, mode: Mode) -> dict[str, Any]:
    """Skeleton result dict with every collection present (never printed)."""
    return {
        "org_id": org_id, "mode": mode, "course_name": "", "dest": None,
        "manifest_path": None, "topic_count": 0, "planned": [],
        "downloaded": [], "skipped": [], "updated": [], "duplicates": [],
        "orphaned": [], "errors": [], "warnings": [],
        "assignments": {"downloaded": [], "skipped": [], "updated": [], "errors": []},
        "manifest_total": 0, "saved": False, "empty": False,
    }


def _load_manifest(manifest_path: Path, result: dict[str, Any]) -> Manifest:
    """Load the manifest, recording a warning + error entry when corrupt."""
    manifest = Manifest()
    try:
        return Manifest.load(manifest_path)
    except ManifestCorruptError as exc:
        result["warnings"].append(f"{exc}. Performing full sync.")
        result["errors"].append({"error": str(exc), "type": "manifest_corrupt"})
        return manifest


def _track_duplicate(sha_hashes: dict[str, list[dict]], file_hash: str, tid: str, filename: str) -> None:
    """Record an entry hash for per-course SHA-256 duplicate detection."""
    sha_hashes.setdefault(file_hash, []).append({"topic_id": tid, "filename": filename})


def run_course(
    client: LighthouseClient,
    org_id: int,
    root: Path,
    *,
    mode: Mode = Mode.SYNC,
    types: str = "file",
    include_assignments: bool = False,
    assignment_id: int | None = None,
) -> dict[str, Any]:
    """Run the Content Topic pipeline for one course and return result data.

    Raises when the TOC or course name cannot be fetched — the caller
    decides whether that aborts the command or only skips the course.
    """
    result = _empty_result(org_id, mode)
    toc, course_name = fetch_toc_and_name(client, org_id)
    result["course_name"] = course_name

    type_set, unknown = parse_type_filter(types)
    for u in unknown:
        result["warnings"].append(f"Unknown content type: {u}")
    downloadable = filter_topics_by_type(toc.get("Modules", []), type_set)
    result["topic_count"] = len(downloadable)

    dest = root / resolve_course_folder_name(course_name, org_id)
    manifest_path = dest / MANIFEST_FILENAME
    result["dest"], result["manifest_path"] = dest, manifest_path

    if mode is Mode.PLAN:
        result["planned"] = [
            {"topic_id": t["topic_id"], "title": t["title"], "path": t["path"]}
            for t in downloadable
        ]
        return result

    if mode is Mode.FORCE and manifest_path.exists():
        manifest_path.unlink()
    manifest = _load_manifest(manifest_path, result)

    if not downloadable and not include_assignments:
        result["empty"] = True
        return result

    dest.mkdir(parents=True, exist_ok=True)

    downloaded, skipped, updated = result["downloaded"], result["skipped"], result["updated"]
    errors = result["errors"]
    sha_hashes: dict[str, list[dict]] = {}
    orphan_candidates = set(manifest.entries.keys()) if mode is Mode.SYNC else set()

    for topic in downloadable:
        tid = str(topic["topic_id"])
        existing = manifest.get(tid)
        orphan_candidates.discard(tid)

        if mode is Mode.SYNC and existing is not None:
            if existing.get("last_modified") == (topic.get("last_modified") or ""):
                filename = existing.get("filename", "")
                rel_path = str(Path(topic["path"]).parent / filename)
                skipped.append(build_entry(tid, filename, rel_path, existing))
                if file_hash := existing.get("sha256", ""):
                    _track_duplicate(sha_hashes, file_hash, tid, filename)
                continue
            target_list = updated
        else:
            target_list = downloaded

        try:
            content, sanitized_name, filepath = download_and_persist_topic(client, org_id, topic, dest, manifest)
            file_hash = compute_sha256(content)
            _track_duplicate(sha_hashes, file_hash, tid, sanitized_name)
            target_list.append(build_entry(tid, sanitized_name, str(filepath.relative_to(dest)), content, file_hash))
        except Exception as e:
            errors.append({"topic_id": tid, "filename": topic.get("title", ""), "error": str(e)})

    assignments = result["assignments"]
    if mode is Mode.SYNC:
        live_orphans = {tid: manifest.get(tid) for tid in orphan_candidates if manifest.get(tid)}
        if include_assignments:
            downloaded_a, skipped_a, updated_a, errors_a = sync_for_course(client, org_id, dest, manifest)
            assignments.update(downloaded=downloaded_a, skipped=skipped_a, updated=updated_a, errors=errors_a)
            for entry in skipped_a + updated_a + downloaded_a:
                live_orphans.pop(assignment_key(entry.get("folder_id", 0), entry.get("file_id", 0)), None)
        result["orphaned"] = [build_entry(tid, e.get("filename", ""), "", e) for tid, e in live_orphans.items()]
    elif include_assignments:
        downloaded_a, errors_a = download_for_course(
            client, org_id, dest, manifest, folder_ids=[assignment_id] if assignment_id else None,
        )
        assignments["downloaded"], assignments["errors"] = downloaded_a, errors_a

    if downloaded or updated or assignments["downloaded"] or assignments["updated"] or errors or assignments["errors"]:
        manifest.save(manifest_path)
        result["saved"] = True
    result["duplicates"] = [
        {"topic_id": e["topic_id"], "filename": e["filename"], "sha256": h}
        for h, es in sha_hashes.items() if len(es) > 1 for e in es
    ]
    result["manifest_total"] = len(manifest)
    return result
