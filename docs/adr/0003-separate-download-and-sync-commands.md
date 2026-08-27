# Separate download and sync commands

Two distinct commands instead of one: `download` for bulk one-shot fetching and `sync` for incremental idempotent updates. Both write the same manifest format. A combined command with mode detection was considered but rejected because the semantics are genuinely different — `download` should always re-download (fresh setup, catch-up), while `sync` should skip unchanged files (cron-friendly). A single command with `--incremental` flag would make the default behavior ambiguous.

Status: accepted

Consequences:
- Users must learn two commands, but the mental model is clear: download once, sync ongoing
- Both commands share the same manifest format, so switching between them is seamless
- `--force` on either command replaces the course's local manifest metadata and
  re-downloads matching content; existing files at those target paths may be
  overwritten, while unrelated files are not deleted
- `download --dry-run` is a read-only plan: it writes no files, directories, or
  manifest metadata. `sync` has no dry-run option.
