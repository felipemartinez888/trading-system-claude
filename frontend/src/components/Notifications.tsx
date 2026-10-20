// In-app notifications: bell with unread badge, inbox panel, optional local sound (off by default).
// The list comes from /api/notifications (events in the hash-chained log, plus connection crashes)
// and from `delta` pushes. Read/unread is local to this browser (localStorage).

import { type ChangeEvent, useEffect, useRef, useState } from "react";

import { useNotifications } from "../api/hooks";
import type { NotificationOut } from "../api/types";
import { formatUtc } from "../format";
import { ProvenanceBadge } from "./ProvenanceBadge";

const SEEN_KEY = "tsc.notifications.seenTs";
const SOUND_KEY = "tsc.notifications.sound";

function readStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // storage unavailable (private mode): state stays in memory
  }
}

/** Counted as unread: warnings, errors and demo WATCH signals newer than the last opening. */
export function isAttention(n: NotificationOut): boolean {
  return n.severity !== "info" || n.kind === "signal_created";
}

function beep(): void {
  const Ctor =
    typeof window !== "undefined"
      ? (window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext)
      : undefined;
  if (Ctor === undefined) return;
  try {
    const ctx = new Ctor();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = 880;
    gain.gain.value = 0.05;
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.15);
    osc.onended = () => void ctx.close();
  } catch {
    // audio not permitted; notifications stay visual
  }
}

export function NotificationBell() {
  const query = useNotifications();
  const [open, setOpen] = useState(false);
  const [seenTs, setSeenTs] = useState<string>(() => readStorage(SEEN_KEY) ?? "");
  const [sound, setSound] = useState<boolean>(() => readStorage(SOUND_KEY) === "on");
  const lastAlerted = useRef<string | null>(null);

  const items = query.data?.notifications.value ?? [];
  const unread = items.filter((n) => isAttention(n) && n.ts > seenTs);
  const newest = unread[0]?.ts ?? null;

  useEffect(() => {
    if (!sound || newest === null) return;
    if (lastAlerted.current !== null && newest > lastAlerted.current) beep();
    lastAlerted.current = newest;
  }, [newest, sound]);

  const toggle = () => {
    if (!open && items[0] !== undefined) {
      setSeenTs(items[0].ts);
      writeStorage(SEEN_KEY, items[0].ts);
    }
    setOpen(!open);
  };

  return (
    <span className="bell">
      <button
        type="button"
        className="btn btn--ghost bell__button"
        aria-expanded={open}
        aria-controls="notification-inbox"
        aria-label={`Notifications, ${unread.length} unread`}
        onClick={toggle}
      >
        Notifications
        {unread.length > 0 ? (
          <span className="bell__badge" data-testid="notification-badge">
            {unread.length > 99 ? "99+" : unread.length}
          </span>
        ) : null}
      </button>
      {open ? (
        <NotificationInbox
          items={items}
          seenTs={seenTs}
          error={query.isError ? String(query.error) : null}
          sound={sound}
          onSound={(on) => {
            setSound(on);
            writeStorage(SOUND_KEY, on ? "on" : "off");
          }}
        />
      ) : null}
    </span>
  );
}

export interface NotificationInboxProps {
  items: NotificationOut[];
  seenTs: string;
  error: string | null;
  sound: boolean;
  onSound: (on: boolean) => void;
}

export function NotificationInbox({ items, seenTs, error, sound, onSound }: NotificationInboxProps) {
  return (
    <div id="notification-inbox" className="inbox" role="dialog" aria-label="Notifications">
      <div className="inbox__header">
        <strong>Notifications</strong>
        <label className="inbox__sound">
          <input
            type="checkbox"
            checked={sound}
            onChange={(e: ChangeEvent<HTMLInputElement>) => onSound(e.target.checked)}
          />{" "}
          Sound
        </label>
      </div>
      {error !== null ? (
        <p className="bad" role="alert">
          Notifications could not be loaded: {error}
        </p>
      ) : items.length === 0 ? (
        <p className="muted" data-testid="inbox-empty">
          Nothing yet. Stream state changes, data gaps, crashes and demo WATCH events appear here
          once monitoring has run.
        </p>
      ) : (
        <ul className="inbox__list" data-testid="inbox-list">
          {items.map((n) => (
            <li
              key={n.id}
              className={`inbox__item inbox__item--${n.severity}${n.ts > seenTs && isAttention(n) ? " inbox__item--unread" : ""}`}
            >
              <div className="inbox__title">
                <ProvenanceBadge provenance={n.provenance} compact />
                <span>{n.title}</span>
              </div>
              <div className="inbox__meta">
                <time dateTime={n.ts}>{formatUtc(n.ts)}</time>
                {n.detail ? <span> · {n.detail}</span> : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
