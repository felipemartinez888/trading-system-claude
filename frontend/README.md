# Dashboard (React + TypeScript + Vite)

The dashboard served by `tsclaude app`. The build output goes to `../src/tsclaude/web/static`.
That folder is never committed; CI builds it, and the Windows launcher builds it once on first run.

```sh
pnpm install
pnpm test        # vitest + Testing Library (jsdom)
pnpm typecheck   # tsc --noEmit (strict)
pnpm build       # typecheck + vite build -> ../src/tsclaude/web/static
pnpm watch       # rebuild on change; reload http://127.0.0.1:8765/ served by `tsclaude app`
```

## pnpm versions and reproducibility

- **Build-script approval.** esbuild must run its install script. `pnpm-workspace.yaml`
  approves it with `allowBuilds` (read by pnpm 10.26 and later; pnpm 11 and later read only this),
  and `package.json` keeps `pnpm.onlyBuiltDependencies` for pnpm 10.0 to 10.25. pnpm 9, which CI
  pins, runs every install script. `tests/unit/test_frontend_toolchain.py` keeps both lists equal.
  With these files no manual `pnpm approve-builds` is needed. If pnpm still reports
  `ERR_PNPM_IGNORED_BUILDS`, another dependency gained an install script. Review it before
  adding it to both lists.
- **No lock file is committed yet** (owner decision D-G). `pnpm install` resolves the newest
  versions inside the `package.json` ranges, so two installs on different days can differ. A
  `pnpm-lock.yaml` written by a local install is yours. It is not part of the repository until
  D-G is decided.
- **Versions in use:**
  - CI pins pnpm 9 (`.github/workflows/ci.yml`);
  - the owner's PC runs pnpm 12.

  A committed lock file is reproducible only with one pnpm version everywhere. Choosing that
  version, and pinning it with `packageManager` so CI and the launcher use it too, is part of D-G.

Rules (CLAUDE.md, ADR-0010, DASHBOARD.md):

- Every displayed value comes from the API together with its `provenance`. Render it with
  `<ProvenanceBadge>`. The UI may downgrade a value to `STALE` when its own snapshot is old, but
  it never upgrades provenance.
- No invented numbers. Pages without data show an empty state that says what will appear and in
  which milestone.
- Mutations send `X-TSC-Token`, read from `<meta name="tsc-token">`, which the backend injects into
  `index.html`. Pages not served by the backend (for example `pnpm dev`) have no token, so their
  mutations are refused.
- Charts: TradingView Lightweight Charts™ (Apache-2.0) for price. Keep its attribution (the
  logo and the "Charts by TradingView" line). In tests it is mocked globally in
  `src/test/setup.ts`, because jsdom has no canvas.
- Live updates: `/ws/live` sends `status` on every change and at most one `delta` per second.
  `delta` messages are merged into the query cache (`applyDelta`), and every query also polls as
  a fallback.
