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
