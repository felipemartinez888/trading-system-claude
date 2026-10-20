import { useLiveStatus } from "./api/hooks";
import { EmptyState } from "./components/EmptyState";
import { StatusBar } from "./components/StatusBar";
import { DEFAULT_PATH, WORKSPACES, findPage, usePath } from "./navigation";
import { HealthPage } from "./pages/HealthPage";
import { LogsPage } from "./pages/LogsPage";

function PageContent({ path }: { path: string }) {
  const page = findPage(path);
  if (page === undefined) {
    return (
      <div className="page">
        <h1 className="page__title">Page not found</h1>
        <p className="muted">There is no page at {path}. Choose one from the left rail.</p>
      </div>
    );
  }
  if (page.id === "health") return <HealthPage />;
  if (page.id === "logs") return <LogsPage />;
  const workspace = WORKSPACES.find((w) => w.pages.includes(page));
  return (
    <div className="page">
      <h1 className="page__title">
        {workspace?.title} › {page.title}
      </h1>
      <EmptyState page={page} />
    </div>
  );
}

export function App() {
  const link = useLiveStatus();
  const [rawPath, go] = usePath();
  const path = rawPath === "/" || rawPath === "/index.html" ? DEFAULT_PATH : rawPath;

  return (
    <div className="shell">
      <StatusBar link={link} />
      <nav className="rail" aria-label="Workspaces">
        {WORKSPACES.map((ws) => (
          <div key={ws.id} className="rail__workspace">
            <h2 className="rail__title">{ws.title}</h2>
            <ul className="rail__pages">
              {ws.pages.map((p) => (
                <li key={p.id}>
                  <a
                    href={p.path}
                    className={`rail__link${p.path === path ? " rail__link--active" : ""}`}
                    aria-current={p.path === path ? "page" : undefined}
                    onClick={(event) => {
                      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
                        return;
                      }
                      event.preventDefault();
                      go(p.path);
                    }}
                  >
                    {p.title}
                    {p.implemented ? null : <span className="rail__later"> · later</span>}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>
      <main className="content" id="main">
        <PageContent path={path} />
      </main>
    </div>
  );
}
