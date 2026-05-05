import { Component, ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
}
interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[viewer] uncaught render error", error, info);
  }

  reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      return (
        <div className="min-h-screen flex items-center justify-center p-6">
          <div className="max-w-2xl w-full bg-[var(--bg-secondary)] border border-rose-700 rounded-lg p-6">
            <h1 className="text-xl font-bold text-rose-400 mb-2">
              Something went wrong rendering this page.
            </h1>
            <p className="text-sm text-[var(--text-secondary)] mb-3">
              The viewer caught a render-time exception. Reload the page or
              return to the records list.
            </p>
            <pre className="text-xs whitespace-pre-wrap break-words font-mono bg-[var(--bg-primary)] p-3 rounded mb-4 max-h-72 overflow-auto">
              {this.state.error.message}
              {this.state.error.stack ? "\n\n" + this.state.error.stack : ""}
            </pre>
            <div className="flex gap-2">
              <button
                onClick={this.reset}
                className="px-3 py-1.5 rounded-md text-sm bg-[var(--accent)] text-white hover:bg-[var(--accent-hover)]"
              >
                Dismiss
              </button>
              <a
                href="/records"
                className="px-3 py-1.5 rounded-md text-sm bg-[var(--bg-tertiary)] hover:bg-[var(--border)]"
              >
                Go to Records
              </a>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
