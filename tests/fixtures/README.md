# Sanitized transcript fixtures

These files are hand-authored, fictional examples of the on-disk formats read
by the viewer. Their record types and field shapes were compared with local
Claude Code, Codex, Cursor, and opencode stores, but no transcript content was
copied from those stores.

Fixture rules:

- use only fictional messages, identifiers, models, commands, and timestamps;
- use `/workspace/example-project` rather than a real home or repository path;
- embed only a generated 1×1 PNG with no metadata or personal screenshot;
- never include credentials, email addresses, remote URLs, usernames, or host
  names;
- keep database-backed fixtures generated in temporary directories rather than
  committing a copy of a user-owned SQLite database.

Review fixture diffs before publishing them. Automated pattern matching cannot
prove that a transcript was anonymized, so privacy depends on keeping these
files fictional rather than copying and attempting to scrub personal sessions.
