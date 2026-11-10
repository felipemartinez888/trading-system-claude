// Paper (MVP-4): the paper runtime, its books and the REFERENCE strategy, read from /api/paper/*.
// Runtime state is SYSTEM; book values, fills, trades and reference signals carry their own
// SIMULATED / STALE / UNAVAILABLE label from the backend. Paper trading is simulated: nothing here
// can place an order, reset a book or recover a session. The only action is recording a new paper
// session, which the backend opens at the next monitoring start. Empty states are shown only for
// loaded data; reasons always come from the backend.

import { useState } from "react";

import { readToken } from "../api/client";
import {
  useNewPaperSessionMutation,
  usePaperActivity,
  usePaperBooks,
  usePaperFills,
  usePaperStatus,
} from "../api/hooks";
import type {
  PaperBooksValue,
  PaperObservation,
  PaperStatusValue,
  ReferenceSignal,
  Sourced,
} from "../api/types";
import { ProvenanceBadge } from "../components/ProvenanceBadge";
import { ValidationBadge } from "../components/ValidationBadge";
import { NOT_AVAILABLE, formatMs, pct, show } from "../format";

export const MVP_NOTE =
  "MVP readiness only: this page is not evidence of M1–M4 acceptance, and paper results are not " +
  "evidence of strategy performance.";

/** Items per list as served by /api/paper/activity (default limit). */
const ACTIVITY_LIMIT = 100;

const BOOK_ROLE: Record<string, string> = {
  baseline: "Strategy intents through the risk gate (the book that counts).",
  shadow: "The same intents without the risk gate: hypothetical outcomes of blocked entries.",
  benchmark: "Equal-weight buy-and-hold of the spot universe from the first observed bar.",
};

const FAILURE_STATES = new Set([
  "FAULTED",
  "UNAVAILABLE",
  "STOPPED_OVERFLOW",
  "SUBSCRIPTION_LOST",
  "CONSUMER_DIED",
]);

function stateClass(state: string): string {
  if (state === "RUNNING") return "chip chip--ok";
  if (FAILURE_STATES.has(state)) return "chip chip--bad";
  if (state === "DISABLED") return "chip";
  return "chip chip--stale";
}

/** Loading / error / no-value line for one API section; null when there is a value to show. */
function SectionState<T>({
  what,
  isError,
  error,
  sourced,
}: {
  what: string;
  isError: boolean;
  error: unknown;
  sourced: Sourced<T> | undefined;
}) {
  if (isError) {
    return (
      <p className="bad" role="alert">
        {what} could not be loaded: {String(error)}
      </p>
    );
  }
  if (sourced === undefined) return <p className="muted">Loading {what}…</p>;
  if (sourced.value === null) {
    return (
      <p className="muted">
        {what}: {NOT_AVAILABLE}
        {sourced.reason ? ` — ${sourced.reason}` : ""}.
      </p>
    );
  }
  return null;
}

function Badge<T>({ sourced }: { sourced: Sourced<T> | undefined }) {
  if (sourced === undefined) return null;
  return (
    <ProvenanceBadge provenance={sourced.provenance} reason={sourced.reason} asOfMs={sourced.as_of_ms} />
  );
}

function Row({ k, v, className }: { k: string; v: string; className?: string | undefined }) {
  return (
    <div className="kv__row">
      <dt>{k}</dt>
      <dd className={className ?? (v === NOT_AVAILABLE ? "muted" : undefined)}>{v}</dd>
    </div>
  );
}

// When this run's bars start to count as observed, or why they do not yet (P-1).
function observationText(o: PaperObservation | null | undefined): string {
  if (o === undefined) return NOT_AVAILABLE;
  if (o === null) return "not established (no market-data poll in this run yet)";
  if (o.state === "blocked") return `blocked — ${o.problem ?? o.code ?? "unknown reason"}`;
  const parts = [
    o.offset_ms !== undefined ? `offset ${o.offset_ms} ms` : null,
    o.max_rtt_ms !== undefined ? `max round trip ${o.max_rtt_ms} ms` : null,
    o.allowance_ms !== undefined ? `allowance ${o.allowance_ms} ms` : null,
  ].filter((p) => p !== null);
  return `from ${formatMs(o.observing_from_ms)}${parts.length ? ` (${parts.join(", ")})` : ""}`;
}

function RuntimeHeader({ sourced }: { sourced: Sourced<PaperStatusValue> }) {
  const status = sourced.value!;
  const prev = status.previous_run;
  const missing = status.awaiting_missing ?? {};
  const audit = status.audit_log;
  const engine = status.service_state !== null;
  return (
    <section className="card" data-testid="paper-runtime">
      <h2 className="card__title">
        Paper runtime{" "}
        <span className={stateClass(status.state)} data-testid="paper-state">
          {status.state}
        </span>
        <Badge sourced={sourced} />
      </h2>
      {status.reason ? (
        <p className={FAILURE_STATES.has(status.state) ? "bad" : "muted"} data-testid="paper-reason">
          {status.reason}
        </p>
      ) : null}
      {status.hint ? (
        <p className="bad" data-testid="paper-hint">
          <strong>What to do:</strong> {status.hint}
        </p>
      ) : null}
      <dl className="kv">
        <Row k="Enabled" v={`${show(status.enabled)} (${show(status.settings_source)})`} />
        <Row k="Session" v={show(status.session_id)} />
        <Row k="Next session (recorded)" v={show(status.next_session_id)} />
        <Row k="Strategy" v={show(status.strategy)} />
        <Row
          k="Observation"
          v={observationText(status.observation)}
          className={status.observation?.state === "blocked" ? "bad" : undefined}
        />
        <Row k="Opened at" v={formatMs(status.opened_at_ms)} />
        <Row k="Last processed bar close" v={formatMs(status.values_as_of_ms)} />
        <Row k="Live bar since this start" v={show(status.live_bar_since_start)} />
        <Row k="Engine state" v={engine ? show(status.service_state) : `${NOT_AVAILABLE} (no paper engine)`} />
        <Row
          k="Fault"
          v={engine ? (status.fault ?? "none") : NOT_AVAILABLE}
          className={status.fault ? "bad" : undefined}
        />
        <Row
          k="Dropped feed updates"
          v={String(status.dropped_updates)}
          className={status.dropped_updates > 0 ? "bad" : undefined}
        />
        <Row
          k="Session audit log"
          v={
            audit === null
              ? NOT_AVAILABLE
              : `${audit.ok ? "ok" : (audit.reason ?? "not ok")}${audit.warnings.length ? ` — warnings: ${audit.warnings.join("; ")}` : ""}`
          }
          className={audit !== null && !audit.ok ? "bad" : undefined}
        />
        <Row
          k="Downtime backfill"
          v={
            status.backfill
              ? `${status.backfill.ok ? "ok" : "not ok"}${status.backfill.problems?.length ? ` — ${status.backfill.problems.join("; ")}` : ""}`
              : "not attempted"
          }
        />
      </dl>
      {Object.keys(missing).length > 0 ? (
        <p className="bad" data-testid="paper-missing">
          Waiting for missing minutes:{" "}
          {Object.entries(missing)
            .map(([k, v]) => `${k}: ${v.length}`)
            .join(", ")}
        </p>
      ) : null}
      {prev ? (
        <p
          className={FAILURE_STATES.has(prev.state) || prev.state === "AWAITING_BACKFILL" ? "bad" : "muted"}
          data-testid="paper-previous-run"
        >
          Previous run: {prev.state}
          {prev.reason ? ` — ${prev.reason}` : ""} (stopped {formatMs(prev.stopped_at_ms)})
        </p>
      ) : null}
    </section>
  );
}

function Limitations({ items }: { items: string[] }) {
  return (
    <details className="card notice" open data-testid="paper-limitations">
      <summary>
        <strong>Known limitations</strong> (simulated paper trading; read before using these values)
      </summary>
      <ul>
        {items.map((l) => (
          <li key={l}>{l}</li>
        ))}
      </ul>
    </details>
  );
}

function Books() {
  const query = usePaperBooks();
  const sourced = query.data?.books;
  const value: PaperBooksValue | null = sourced?.value ?? null;
  const books = value?.books ?? {};
  const summary = value?.summary ?? {};
  const names = Object.keys(books);
  return (
    <section data-testid="paper-books">
      <h2>
        Books <Badge sourced={sourced} />
      </h2>
      <SectionState what="Book values" isError={query.isError} error={query.error} sourced={sourced} />
      {value !== null && names.length === 0 ? (
        <p className="muted">No books in this session.</p>
      ) : null}
      {names.length > 0 ? (
        <>
          <div className="book-grid">
            {names.map((name) => {
              const b = books[name]!;
              const s = summary[name];
              return (
                <article key={name} className="card" data-testid={`book-${name}`}>
                  <h3 className="card__title">
                    {name}
                    {b.halted ? <span className="chip chip--bad">halted</span> : null}
                    {!b.ledger_reconciled ? (
                      <span className="chip chip--bad">ledger not reconciled</span>
                    ) : null}
                  </h3>
                  <p className="muted">{BOOK_ROLE[name] ?? ""}</p>
                  <dl className="kv">
                    <Row k="Equity" v={show(b.equity)} />
                    <Row k="Cash" v={show(b.cash)} />
                    <Row k="Unrealized PnL" v={show(b.unrealized_pnl)} />
                    <Row k="Open positions" v={show(b.open_positions)} />
                    <Row k="Closed trades" v={show(s?.trades)} />
                    <Row k="Max drawdown (of peak equity)" v={pct(s?.max_drawdown)} />
                  </dl>
                </article>
              );
            })}
          </div>
          <h3>Positions (quantity per instrument)</h3>
          <table className="table table--dense" data-testid="paper-positions">
            <thead>
              <tr>
                <th scope="col">Book</th>
                <th scope="col">Instrument</th>
                <th scope="col">Quantity</th>
              </tr>
            </thead>
            <tbody>
              {names.flatMap((name) =>
                Object.entries(books[name]!.positions).map(([sym, qty]) => (
                  <tr key={`${name}:${sym}`}>
                    <td>{name}</td>
                    <td>{sym}</td>
                    <td className="num">{show(qty)}</td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
          <p className="muted">
            Entry price and unrealized PnL per position: {NOT_AVAILABLE} (not exposed by the backend).
          </p>
          <h3>Rejections at fill time (by reason)</h3>
          <table className="table table--dense" data-testid="paper-fill-rejections">
            <thead>
              <tr>
                <th scope="col">Book</th>
                <th scope="col">Reason</th>
                <th scope="col">Count</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(summary).flatMap(([name, s]) =>
                Object.entries(s.rejections_by_reason ?? {}).map(([reason, n]) => (
                  <tr key={`${name}:${reason}`}>
                    <td>{name}</td>
                    <td>{reason}</td>
                    <td className="num">{n}</td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        </>
      ) : null}
    </section>
  );
}

function Activity() {
  const query = usePaperActivity();
  const sourced = query.data?.activity;
  const value = sourced?.value ?? null;
  const trades = value?.trades ?? [];
  const rejections = value?.request_rejections ?? [];
  return (
    <section data-testid="paper-activity">
      <h2>
        Trade history and risk rejections <Badge sourced={sourced} />
      </h2>
      <SectionState what="Activity" isError={query.isError} error={query.error} sourced={sourced} />
      {value !== null ? (
        <>
          <h3>Closed trades (newest last; at most the last {ACTIVITY_LIMIT})</h3>
          {trades.length === 0 ? (
            <p className="muted" data-testid="paper-trades-empty">
              No closed trades in this session.
            </p>
          ) : (
            <div className="table-wrap">
              <table className="table table--dense" data-testid="paper-trades">
                <thead>
                  <tr>
                    <th scope="col">Book</th>
                    <th scope="col">Instrument</th>
                    <th scope="col">Side</th>
                    <th scope="col">Entry (bar open)</th>
                    <th scope="col">Entry price</th>
                    <th scope="col">Exit (bar open)</th>
                    <th scope="col">Exit price</th>
                    <th scope="col">Exit reason</th>
                    <th scope="col">Net PnL</th>
                    <th scope="col">R</th>
                    <th scope="col">Flags</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t, i) => (
                    <tr key={`${t.trade_id}:${i}`}>
                      <td>{t.book}</td>
                      <td>
                        {t.market}:{t.symbol}
                      </td>
                      <td>{t.side}</td>
                      <td>{formatMs(t.entry_bar_open_ms)}</td>
                      <td className="num">{show(t.entry_price)}</td>
                      <td>{formatMs(t.exit_bar_open_ms)}</td>
                      <td className="num">{show(t.exit_price)}</td>
                      <td>{show(t.exit_reason)}</td>
                      <td className="num">{show(t.net_pnl)}</td>
                      <td className="num">{show(t.r_multiple)}</td>
                      <td>
                        {[
                          t.gap_fill ? "gap fill" : "",
                          t.ambiguous_bar ? "ambiguous bar" : "",
                          t.spans_gap ? "spans data gap" : "",
                          t.resolved_from_backfill ? "exit resolved from backfill" : "",
                          t.deferred_no_trade ? "waited for a traded candle" : "",
                          t.spans_exchange_downtime ? "spans exchange downtime" : "",
                        ]
                          .filter(Boolean)
                          .join(", ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <h3>Rejected entry requests (risk gate and validation; at most the last {ACTIVITY_LIMIT})</h3>
          {rejections.length === 0 ? (
            <p className="muted" data-testid="paper-request-rejections-empty">
              No rejected requests in this session.
            </p>
          ) : (
            <table className="table table--dense" data-testid="paper-request-rejections">
              <thead>
                <tr>
                  <th scope="col">Time</th>
                  <th scope="col">Book / stage</th>
                  <th scope="col">Request</th>
                  <th scope="col">Reason</th>
                </tr>
              </thead>
              <tbody>
                {rejections.map((r, i) => (
                  <tr key={`${r.aggregate_id}:${r.request_id ?? "-"}:${r.ts_ms}:${i}`}>
                    <td>{formatMs(r.ts_ms)}</td>
                    <td>{r.aggregate_id}</td>
                    <td className="mono">{show(r.request_id)}</td>
                    <td>{r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      ) : null}
    </section>
  );
}

function Fills() {
  const query = usePaperFills();
  const sourced = query.data?.fills;
  const value = sourced?.value ?? null;
  const items = [...(value ?? [])].reverse();
  return (
    <section data-testid="paper-fills">
      <h2>
        Fills (newest first) <Badge sourced={sourced} />
      </h2>
      <SectionState what="Fills" isError={query.isError} error={query.error} sourced={sourced} />
      {value !== null && items.length === 0 ? (
        <p className="muted">No fills in this session.</p>
      ) : null}
      {items.length > 0 ? (
        <div className="table-wrap">
          <table className="table table--dense">
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Book</th>
                <th scope="col">Order</th>
                <th scope="col">Role</th>
                <th scope="col">Direction</th>
                <th scope="col">Price</th>
                <th scope="col">Qty</th>
                <th scope="col">Fee</th>
              </tr>
            </thead>
            <tbody>
              {items.map((f, i) => (
                <tr key={`${f.aggregate_id}:${f.order_id ?? ""}:${f.ts_ms}:${i}`}>
                  <td>{formatMs(f.ts_ms)}</td>
                  <td>{f.aggregate_id}</td>
                  <td className="mono">{show(f.order_id)}</td>
                  <td>{show(f.role)}</td>
                  <td>{show(f.direction)}</td>
                  <td className="num">{show(f.price)}</td>
                  <td className="num">{show(f.qty)}</td>
                  <td className="num">{show(f.fee)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

function Reference({
  status,
  statusSourced,
  signals,
}: {
  status: PaperStatusValue;
  statusSourced: Sourced<PaperStatusValue>;
  signals: Sourced<ReferenceSignal[]> | null | undefined;
}) {
  const ref = status.reference;
  if (ref === null && status.strategy === "none") {
    return (
      <section data-testid="paper-reference-none">
        <h2>Strategy</h2>
        <p className="muted">
          No live strategy is selected (<code>strategy: {status.strategy}</code>); the benchmark book
          runs alone.
        </p>
      </section>
    );
  }
  if (ref === null) {
    // selected, but built only at the first observed minute (P-1): say what it is waiting for
    const o = status.observation;
    const blocked = o?.state === "blocked";
    let detail: string;
    if (status.state !== "RUNNING") {
      detail = `paper is ${status.state}; the strategy is built at the first observed minute of a run.`;
    } else if (o === undefined || o === null) {
      detail = "waiting for the first market-data poll of this run.";
    } else if (blocked) {
      detail = `observation is blocked: ${o.problem ?? o.code ?? "unknown reason"}. Bars are processed unobserved until trustworthy clock evidence returns.`;
    } else {
      detail = `waiting for its first observed minute (from ${formatMs(o.observing_from_ms)}).`;
    }
    return (
      <section data-testid="paper-reference-deferred">
        <h2>Strategy</h2>
        <p className={blocked ? "bad" : "muted"}>
          <code>{status.strategy}</code> is selected and not started yet: {detail}
        </p>
      </section>
    );
  }
  const items = signals?.value ?? [];
  return (
    <section className="card reference" data-testid="paper-reference">
      <h2 className="card__title">
        Reference strategy {ref.id} v{ref.version}
        <ValidationBadge label={ref.label} strategyId={ref.id} grade={ref.evidence_grade} />
        <Badge sourced={statusSourced} />
      </h2>
      <p role="note" className="reference__banner">
        REFERENCE / NOT VALIDATED — a pipeline strategy for exercising paper trading. It is not
        claimed profitable and its signals are not research results. Its evidence grade field (
        <code>{ref.evidence_grade}</code>) is not a validation result.
      </p>
      <p className="muted">{ref.note}</p>
      <dl className="kv">
        <Row k="CONFIRMED intents sent to paper" v={String(ref.confirmed_intents)} />
        <Row k="WATCH proposals (counted, never traded)" v={String(ref.watch_proposals)} />
        <Row k="History restarts (all instruments)" v={String(ref.history_breaks)} />
        <Row k="Decisions from" v={formatMs(ref.trade_from_ms)} />
      </dl>
      <h3>Warm-up per instrument</h3>
      <table className="table table--dense" data-testid="paper-warmup">
        <thead>
          <tr>
            <th scope="col">Instrument</th>
            <th scope="col">Contiguous {ref.interval} bars</th>
            <th scope="col">Needed</th>
            <th scope="col">Ready</th>
            <th scope="col">Restarts</th>
            <th scope="col">Last restart</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(ref.warmup_progress).map(([key, w]) => (
            <tr key={key}>
              <td>{key}</td>
              <td className="num">
                <progress
                  max={w.needed}
                  value={Math.min(w.strategy_bars, w.needed)}
                  aria-label={`${key} warm-up`}
                />{" "}
                {w.strategy_bars}
              </td>
              <td className="num">{w.needed}</td>
              <td className={w.ready ? "ok" : "bad"}>{w.ready ? "ready" : "warming up"}</td>
              <td className="num">{show(w.history_breaks)}</td>
              <td>
                {w.last_break_detected_at_ms != null
                  ? `${formatMs(w.last_break_detected_at_ms)} (${w.last_break_phase ?? "?"}): ${w.last_break_reason ?? ""}`
                  : w.history_breaks === undefined
                    ? NOT_AVAILABLE
                    : "none"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>
        Reference signals (CONFIRMED only) <Badge sourced={signals ?? undefined} />
      </h3>
      {signals === null || signals === undefined ? (
        <p className="muted">Reference signals: {NOT_AVAILABLE}.</p>
      ) : signals.value === null ? (
        <p className="muted">
          Reference signals: {NOT_AVAILABLE}
          {signals.reason ? ` — ${signals.reason}` : ""}.
        </p>
      ) : items.length === 0 ? (
        <p className="muted" data-testid="paper-signals-empty">
          No reference signals in this run.
        </p>
      ) : (
        <table className="table table--dense" data-testid="paper-signals">
          <thead>
            <tr>
              <th scope="col">Decision (bar close)</th>
              <th scope="col">Instrument</th>
              <th scope="col">Side</th>
              <th scope="col">Ref. price</th>
              <th scope="col">Stop</th>
              <th scope="col">Take profit</th>
              <th scope="col">Valid until</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {items.map((s) => (
              <tr key={s.signal_id}>
                <td>{formatMs(s.decision_time_ms)}</td>
                <td>
                  {s.market}:{s.symbol}
                </td>
                <td>{s.side}</td>
                <td className="num">{show(s.ref_price)}</td>
                <td className="num">{show(s.stop_price)}</td>
                <td className="num">{show(s.take_profit_price)}</td>
                <td>{formatMs(s.valid_until_ms)}</td>
                <td>
                  <ValidationBadge label={s.label} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function newRequestId(): string {
  const rand = Math.random().toString(36).slice(2, 10);
  return `ui-${Date.now().toString(36)}-${rand}`;
}

function NewSession() {
  const [note, setNote] = useState("");
  const mutation = useNewPaperSessionMutation();
  const tokenPresent = readToken() !== null;
  const trimmed = note.trim();
  const result = mutation.data;
  return (
    <section className="card" data-testid="paper-new-session">
      <h2 className="card__title">Operator action</h2>
      <p className="muted">
        Record a new paper session. It opens at the next monitoring start; the current session and
        its journal stay unchanged and read-only. There is no reset, recovery or order action.
      </p>
      <label>
        Note (required, why a new session):{" "}
        <textarea value={note} maxLength={500} rows={2} onChange={(e) => setNote(e.target.value)} />
      </label>
      <div>
        <button
          type="button"
          className="btn"
          disabled={!tokenPresent || trimmed.length === 0 || mutation.isPending}
          onClick={() => mutation.mutate({ note: trimmed, requestId: newRequestId() })}
        >
          Record new paper session
        </button>
        {!tokenPresent ? (
          <span className="muted"> Open the dashboard from the app URL to enable this.</span>
        ) : null}
      </div>
      {mutation.isError ? (
        <p className="bad" role="alert">
          Refused: {String(mutation.error)}
        </p>
      ) : null}
      {result !== undefined ? (
        <p className="ok" role="status">
          {result.created
            ? `Recorded session ${show(result.new_session_id)}; it opens at the next monitoring start.`
            : `Already recorded (session ${show(result.new_session_id)}); nothing changed.`}
        </p>
      ) : null}
    </section>
  );
}

export function PaperPage() {
  const status = usePaperStatus();
  const sourced = status.data?.status;
  const value = sourced?.value ?? null;
  return (
    <div className="page">
      <h1 className="page__title">Paper</h1>
      <p className="muted">
        Simulated paper trading on live observations. No order is ever sent to an exchange.
      </p>
      <p className="notice" role="note" data-testid="paper-mvp-note">
        {MVP_NOTE}
      </p>
      {status.isError ? (
        <p className="bad" role="alert">
          Paper status could not be loaded: {String(status.error)}
        </p>
      ) : sourced === undefined ? (
        <p className="muted">Loading…</p>
      ) : value === null ? (
        <p className="muted" data-testid="paper-unavailable">
          Paper status: {NOT_AVAILABLE}
          {sourced.reason ? ` — ${sourced.reason}` : ""}.
        </p>
      ) : (
        <>
          <RuntimeHeader sourced={sourced} />
          <Limitations items={value.limitations} />
          <Books />
          <Activity />
          <Fills />
          <Reference status={value} statusSourced={sourced} signals={status.data?.reference_signals} />
          <NewSession />
        </>
      )}
    </div>
  );
}
