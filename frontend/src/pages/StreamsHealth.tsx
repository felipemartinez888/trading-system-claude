// System › Health, market-data part (M1): streams, connections, clock, REST weight, gaps, detector.

import { useGaps, useStreamsHealth } from "../api/hooks";
import type { Sourced } from "../api/types";
import { ProvenanceBadge } from "../components/ProvenanceBadge";
import { formatDuration, formatUtc } from "../format";
import { type Freshness, shown, useBackendFreshness, useNow } from "../freshness";

function Badge<T>({ s, f }: { s: Sourced<T>; f: Freshness }) {
  return <ProvenanceBadge {...shown(s, f)} compact />;
}

export function StreamsHealth() {
  const health = useStreamsHealth();
  const gaps = useGaps();
  const freshness = useBackendFreshness();
  const now = useNow();
  const h = health.data;

  if (health.isError) {
    return (
      <p className="bad" role="alert">
        Stream health could not be loaded: {String(health.error)}
      </p>
    );
  }
  if (h === undefined) return <p className="muted">Loading stream health…</p>;

  const streams = h.streams.value;
  const connections = h.connections.value;
  const weights = h.rest_weight.value;
  const gapList = gaps.data?.gaps.value ?? [];

  return (
    <>
      <section className="card" aria-labelledby="h-streams">
        <h2 id="h-streams" className="card__title">
          Market-data streams <Badge s={h.streams} f={freshness} />
        </h2>
        {streams === null ? (
          <p className="muted" data-testid="streams-empty">
            {h.streams.reason}. Press <strong>Start</strong>: one row per stream (kline 1m,
            bookTicker, markPrice) appears with its state and freshness budget.
          </p>
        ) : (
          <table className="table table--dense" data-testid="streams-table">
            <thead>
              <tr>
                <th scope="col">Stream</th>
                <th scope="col">State</th>
                <th scope="col">Since</th>
                <th scope="col">Last message</th>
                <th scope="col">Budget</th>
                <th scope="col">Disconnects</th>
              </tr>
            </thead>
            <tbody>
              {streams.map((s) => (
                <tr key={s.stream}>
                  <td className="mono">{s.stream}</td>
                  <td>
                    {s.state.value ?? "—"} <Badge s={s.state} f={freshness} />
                  </td>
                  <td>{formatUtc(new Date(s.since_ms).toISOString())}</td>
                  <td>
                    {s.last_message_ms === null
                      ? "never"
                      : `${formatDuration(Math.max(0, now - s.last_message_ms) / 1000)} ago`}
                  </td>
                  <td>{formatDuration(s.budget_ms / 1000)}</td>
                  <td className="num">{s.disconnects}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card" aria-labelledby="h-conn">
        <h2 id="h-conn" className="card__title">
          Connections <Badge s={h.connections} f={freshness} />
        </h2>
        {connections === null ? (
          <p className="muted">{h.connections.reason}</p>
        ) : (
          <table className="table table--dense" data-testid="connections-table">
            <thead>
              <tr>
                <th scope="col">Group</th>
                <th scope="col">Connected</th>
                <th scope="col">Reconnects</th>
                <th scope="col">Recycles</th>
                <th scope="col">Crashes</th>
                <th scope="col">Malformed</th>
                <th scope="col">Messages</th>
                <th scope="col">Last error</th>
              </tr>
            </thead>
            <tbody>
              {connections.map((c) => (
                <tr key={c.group}>
                  <td className="mono">{c.group}</td>
                  <td>{c.connected ? "yes" : "no"}</td>
                  <td className="num">{c.reconnects}</td>
                  <td className="num">{c.recycles}</td>
                  <td className={`num${c.crashes > 0 ? " bad" : ""}`}>{c.crashes}</td>
                  <td className="num">{c.malformed}</td>
                  <td className="num">{c.messages}</td>
                  <td>{c.last_error ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <dl className="kv">
          <dt>Clock offset (server − local)</dt>
          <dd data-testid="clock-offset">
            {h.clock_offset_ms.value === null ? "—" : `${h.clock_offset_ms.value} ms`}{" "}
            <Badge s={h.clock_offset_ms} f={freshness} />
          </dd>
          <dt>REST weight used</dt>
          <dd>
            {weights === null
              ? "—"
              : Object.entries(weights)
                  .map(([m, w]) =>
                    `${m}: ${Object.entries(w)
                      .map(([win, [used, limit]]) => `${used}/${limit ?? "?"} per ${win}`)
                      .join(", ")}`,
                  )
                  .join(" · ")}{" "}
            <Badge s={h.rest_weight} f={freshness} />
          </dd>
          <dt>REST banned</dt>
          <dd>
            {h.rest_banned.value === null
              ? "—"
              : Object.entries(h.rest_banned.value)
                  .map(([m, b]) => `${m}: ${b ? "BANNED" : "no"}`)
                  .join(" · ")}{" "}
            <Badge s={h.rest_banned} f={freshness} />
          </dd>
          <dt>Demo detector</dt>
          <dd>
            {h.demo_detector.value === null
              ? "—"
              : `${h.demo_detector.value.enabled ? "on" : "off"}, threshold ±${(h.demo_detector.value.threshold * 100).toFixed(2)}%, ${h.demo_detector.value.emitted} WATCH from ${h.demo_detector.value.bars_seen} bars, ${h.demo_detector.value.skipped_unhealthy} skipped (unhealthy)`}{" "}
            <Badge s={h.demo_detector} f={freshness} />
          </dd>
        </dl>
      </section>

      <section className="card" aria-labelledby="h-gaps">
        <h2 id="h-gaps" className="card__title">
          Data gaps (closed 1m bars){" "}
          {gaps.data ? <Badge s={gaps.data.gaps} f={freshness} /> : null}
          <span className="muted">
            {" "}
            open: {h.open_gaps.value ?? "—"} ({h.open_gap_minutes.value ?? "—"} min)
          </span>
        </h2>
        {gapList.length === 0 ? (
          <p className="muted" data-testid="gaps-empty">
            No gaps recorded. A gap appears when a closed minute is neither received live nor
            returned by REST; it stays listed until recovered.
          </p>
        ) : (
          <table className="table table--dense" data-testid="gaps-table">
            <thead>
              <tr>
                <th scope="col">Symbol</th>
                <th scope="col">From</th>
                <th scope="col">To</th>
                <th scope="col">Minutes</th>
                <th scope="col">Still missing</th>
                <th scope="col">Cause</th>
                <th scope="col">Resolution</th>
                <th scope="col">Resolved</th>
              </tr>
            </thead>
            <tbody>
              {gapList.map((g) => (
                <tr key={g.gap_id} className={g.missing_minutes > 0 ? "row--unobserved" : undefined}>
                  <td>
                    {g.market}:{g.symbol}
                  </td>
                  <td>{formatUtc(g.start)}</td>
                  <td>{formatUtc(g.end)}</td>
                  <td className="num">{g.minutes}</td>
                  <td className="num">{g.missing_minutes}</td>
                  <td>{g.cause}</td>
                  <td>{g.resolution}</td>
                  <td>{g.resolved_at === null ? "—" : formatUtc(g.resolved_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
