import type { PageDef } from "../navigation";

/** "Nothing yet" state (DASHBOARD §4): what will appear here and which milestone produces it. */
export function EmptyState({ page }: { page: PageDef }) {
  return (
    <section className="empty" aria-labelledby={`empty-${page.id}`}>
      <h2 id={`empty-${page.id}`} className="empty__title">
        Nothing here yet
      </h2>
      <dl className="empty__facts">
        <dt>What will appear</dt>
        <dd>{page.willShow}</dd>
        <dt>When</dt>
        <dd>{page.arrives}</dd>
      </dl>
      <p className="empty__note">
        No placeholder or demo numbers are shown. This page fills in once the backend provides real
        data for it, each value labeled with its provenance.
      </p>
    </section>
  );
}
