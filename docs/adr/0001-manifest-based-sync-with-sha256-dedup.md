# Manifest-based sync with SHA-256 dedup

We store a `.lighthouse.json` manifest per course directory mapping each
`topic_id` to `{sha256, filename, size, downloaded_at, last_modified}`. On
sync, we fetch the content TOC, compare `LastModifiedDate` from the TOC
response against the manifest's `last_modified`, and verify that the recorded
local file is still a regular file of the recorded size and has the recorded
SHA-256 before skipping it. This validation re-reads and rehashes local bytes,
so same-size local edits do not get silently skipped. Skipped and orphaned
reports use the hash and other metadata already in the manifest after that
check; orphaned entries do not need a disk rehash. The extra local I/O and CPU
cost is the tradeoff for detecting local tampering or edits. SHA-256
cross-referencing catches duplicate uploads (the same PDF attached to multiple
topics by the professor).

The alternatives were: (1) pure existence-based skipping — too coarse, misses
updated files; (2) size comparison alone — catches missing/truncated files but
misses same-size re-edits; (3) always-download-and-hash — perfectly accurate
but wasteful, downloading even unchanged files over the network; (4) HEAD
requests for change detection — rejected because D2L does not support HEAD
for content topic file endpoints. The manifest approach using TOC
`LastModifiedDate`, local size and digest validation, and selective downloads
is the best balance: one TOC fetch, local hashing for unchanged candidates,
and bounded file transfers.

Status: accepted
