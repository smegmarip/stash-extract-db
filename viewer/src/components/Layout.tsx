import { Link, useLocation } from "react-router-dom";
import { cn } from "@/lib/cn";

const navItems = [
  { path: "/records", label: "Records" },
  { path: "/performers", label: "Performers" },
  { path: "/status", label: "Status" },
  { path: "/query", label: "Query" },
];

export default function Layout({ children }: { children: React.ReactNode }) {
  const location = useLocation();

  return (
    <div className="min-h-screen flex flex-col">
      <header className="bg-[var(--bg-secondary)] border-b border-[var(--border)] sticky top-0 z-40">
        <div className="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between gap-4">
          <Link to="/" className="text-xl font-bold text-[var(--accent)] whitespace-nowrap">
            Stash Extract Bridge Viewer
          </Link>
          <nav className="flex gap-1">
            {navItems.map((item) => (
              <Link
                key={item.path}
                to={item.path}
                className={cn(
                  "px-4 py-2 rounded-md transition-colors",
                  location.pathname.startsWith(item.path)
                    ? "bg-[var(--accent)] text-white"
                    : "hover:bg-[var(--bg-tertiary)] text-[var(--text-secondary)]",
                )}
              >
                {item.label}
              </Link>
            ))}
          </nav>
        </div>
      </header>

      <main className="flex-1 max-w-7xl mx-auto px-4 py-6 w-full">{children}</main>

      <footer className="bg-[var(--bg-secondary)] border-t border-[var(--border)] py-4 mt-8">
        <div className="max-w-7xl mx-auto px-4 text-sm text-[var(--text-secondary)]">
          Stash Extract Bridge Viewer · read-only
        </div>
      </footer>
    </div>
  );
}
