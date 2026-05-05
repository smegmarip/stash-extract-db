import { cn } from "@/lib/cn";

interface BadgeProps {
  children: React.ReactNode;
  variant?: "default" | "secondary" | "outline" | "success" | "warning" | "danger";
  className?: string;
  onClick?: () => void;
  title?: string;
}

export function Badge({ children, variant = "default", className, onClick, title }: BadgeProps) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center px-2 py-0.5 rounded text-xs font-medium",
        variant === "default" && "bg-[var(--accent)] text-white",
        variant === "secondary" && "bg-[var(--bg-tertiary)] text-[var(--text-secondary)]",
        variant === "outline" && "border border-[var(--border)] text-[var(--text-secondary)]",
        variant === "success" && "bg-emerald-700 text-white",
        variant === "warning" && "bg-amber-700 text-white",
        variant === "danger" && "bg-rose-700 text-white",
        onClick && "cursor-pointer",
        className,
      )}
      onClick={onClick}
    >
      {children}
    </span>
  );
}
