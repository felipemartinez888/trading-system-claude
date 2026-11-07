# Redacted derivative — read before using these files

`inventory_before_plan.json` and `inventory_after_plan.json` in this folder are **redacted derivatives**
of the original `data\` inventory snapshots taken around the read-only `plan` dry run on the real
canonical lake (2026-11-06T21:00:23Z / 21:00:27Z, around the `plan` run at 21:00:25Z).

- **Original, unredacted inventories:** 1,940 entries each, byte-identical to each other
  (SHA-256 `0b34e24f7bd1cd689f76dbdf3eef2cbccd9b74327e3fd24433eaa5cfd5380732`, as a plain file with LF line
  endings — line-ending normalization on checkout can change this hash without changing the content).
  Kept privately, outside every Git repository, by the owner; not published.
- **Redaction:** all 45 entries whose key starts with `SEALED:` (the sealed 2026 holdout — file path,
  content-hash-derived filename, exact byte size and nanosecond modification time, for all five symbols
  and all nine sealed months) were removed. No other entry was added, removed or changed.
- **Published derivative:** 1,895 entries each, byte-identical to each other and, entry for entry, to the
  originals above (every surviving key maps to the exact same value as in the original). SHA-256
  `754744a2bb1a592fe9e16dc5239e8e330b242ff50dfa4879dd646ef946758899` for each redacted file as committed
  here (LF line endings).
- **Why:** these two files were captured as evidence that the read-only `plan` command writes nothing to
  `data\` (see [the D-17 promotion plan](../../M2-D17-promotion-plan.md)). That proof needs only the
  non-sealed entries; the sealed ones were incidental to a full-folder snapshot and are metadata about
  the sealed 2026 holdout (part existence, size, timing) that should not be published, even though no
  holdout market-data value was ever read, decoded or included here.
