import { cn } from "@/lib/cn";

interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
}

export function SearchInput({ value, onChange, placeholder = "Search...", className }: SearchInputProps) {
  return (
    <div className={cn("relative", className)}>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={cn(
          "w-full px-4 py-2 bg-[var(--bg-tertiary)] border border-[var(--border)] rounded-md",
          "text-[var(--text-primary)] placeholder:text-[var(--text-secondary)]",
          "focus:outline-none focus:ring-2 focus:ring-[var(--accent)] focus:border-transparent",
        )}
      />
    </div>
  );
}
