// System › Logs & audit (M0): recent events from the hash-chained event log.

import { useEvents } from "../api/hooks";
import { ProvenanceBadge } from "../components/ProvenanceBadge";
import { formatUtc, shortHash } from "../format";
import { shown, useBackendFreshness } from "../freshness";

const LIMIT = 200;

export function LogsPage() {
  const events = useEvents(LIMIT);
  const freshness = useBackendFreshness();
  const data = events.data;
  const page = data?.events.value ?? null;
  const chain = data?.chain.value ?? null;

  return (
    <div className="page">
      <h1 className="page__title">System › Logs &amp; audit</h1>
      <p className="muted">
        Every runtime fact is an append-only, hash-chained event. Newest {LIMIT} shown, newest
        first.
        {chain !== null ? (
          <>
            {" "}
            Chain at startup:{" "}
            <span className={chain.ok ? "ok" : "bad"}>
              {chain.ok ? "verified" : `FAILED at seq ${chain.first_bad_seq ?? "?"} (${chain.reason ?? "?"})`}
            </span>{" "}
            {data !== undefined ? (
              <span data-testid="logs-chain-provenance">
                <ProvenanceBadge {...shown(data.chain, freshness)} compact />
              </span>
            ) : null}
            .
          </>
        ) : null}
      </p>
      {events.isError ? (
        <p className="bad" role="alert">
          Events could not be loaded: {String(events.error)}
        </p>
      ) : data === undefined ? (
        <p className="muted">Loading…</p>
      ) : page === null ? (
        <p className="bad">
          <ProvenanceBadge provenance={data.events.provenance} reason={data.events.reason} compact />{" "}
          {data.events.reason}
        </p>
      ) : page.items.length === 0 ? (
        <p data-testid="events-empty">
          The event log is empty. Events appear as soon as the app records its session start.
        </p>
      ) : (
        <>
        <p>
          Read from the local event store{" "}
          <ProvenanceBadge {...shown(data.events, freshness)} compact />
        </p>
        <table className="table table--dense" data-testid="events-table">
          <caption className="sr-only">Recent events</caption>
          <thead>
            <tr>
              <th scope="col">Seq</th>
              <th scope="col">Time (UTC)</th>
              <th scope="col">Type</th>
              <th scope="col">Aggregate</th>
              <th scope="col">Payload</th>
              <th scope="col">Hash</th>
            </tr>
          </thead>
          <tbody>
            {[...page.items].reverse().map((e) => (
              <tr key={e.seq}>
                <td className="num">{e.seq}</td>
                <td>{formatUtc(e.ts)}</td>
                <td className="mono">{e.type}</td>
                <td className="mono" title={e.aggregate_id}>
                  {e.aggregate_type}:{shortHash(e.aggregate_id, 10)}
                </td>
                <td className="mono payload">{JSON.stringify(e.payload)}</td>
                <td className="mono" title={e.hash}>
                  {shortHash(e.hash, 10)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </>
      )}
      {page?.has_more ? (
        <p className="muted">Older events exist (head seq {page.head_seq}); paging arrives with M1.</p>
      ) : null}
    </div>
  );
}
