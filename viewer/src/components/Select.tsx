import * as SelectPrimitive from "@radix-ui/react-select";
import { cn } from "@/lib/cn";

export interface SelectOption {
  value: string;
  label: string;
}

interface SelectProps {
  value: string;
  onValueChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  className?: string;
  ariaLabel?: string;
}

export function Select({
  value, onValueChange, options, placeholder, className, ariaLabel,
}: SelectProps) {
  return (
    <SelectPrimitive.Root value={value} onValueChange={onValueChange}>
      <SelectPrimitive.Trigger
        aria-label={ariaLabel}
        className={cn(
          "inline-flex items-center justify-between gap-2 px-3 py-2 rounded-md text-sm",
          "bg-[var(--bg-tertiary)] border border-[var(--border)]",
          "text-[var(--text-primary)] hover:border-[var(--accent)]",
          "focus:outline-none focus:ring-2 focus:ring-[var(--accent)]",
          "min-w-[10rem]",
          className,
        )}
      >
        <SelectPrimitive.Value placeholder={placeholder} />
        <SelectPrimitive.Icon className="text-[var(--text-secondary)]">
          ▾
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          className={cn(
            "z-50 overflow-hidden rounded-md border border-[var(--border)]",
            "bg-[var(--bg-secondary)] text-[var(--text-primary)] shadow-lg",
          )}
          position="popper"
          sideOffset={4}
        >
          <SelectPrimitive.Viewport className="p-1 max-h-80">
            {options.map((opt) => (
              <SelectPrimitive.Item
                key={opt.value}
                value={opt.value}
                className={cn(
                  "relative flex cursor-pointer select-none items-center px-3 py-2 text-sm rounded",
                  "outline-none data-[highlighted]:bg-[var(--accent)] data-[highlighted]:text-white",
                  "data-[state=checked]:font-semibold",
                )}
              >
                <SelectPrimitive.ItemText>{opt.label}</SelectPrimitive.ItemText>
              </SelectPrimitive.Item>
            ))}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
}
