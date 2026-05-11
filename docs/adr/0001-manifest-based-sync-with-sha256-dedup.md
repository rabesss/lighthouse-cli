# Manifest-based sync with SHA-256 dedup

We store a `.lighthouse.json` manifest per course directory mapping each `topic_id` to `{sha256, filename, size, downloaded_at, last_modified}`. On sync, we fetch the content TOC, compare `LastModifiedDate` from the TOC response against the manifest's `last_modified`, and only re-download when content has changed. SHA-256 cross-referencing catches duplicate uploads (same PDF attached to multiple topics by the professor).

The alternatives were: (1) pure existence-based skipping — too coarse, misses updated files; (2) size comparison — catches size changes but misses same-size re-edits; (3) always-download-and-hash — perfectly accurate but wasteful, downloading even unchanged files over the network; (4) HEAD requests for change detection — rejected because D2L does not support HEAD for content topic file endpoints. The manifest approach using TOC `LastModifiedDate` is the best balance: one TOC fetch + selective downloads.

Status: accepted
