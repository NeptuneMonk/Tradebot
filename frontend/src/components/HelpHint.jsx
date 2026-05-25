import { HelpCircle } from "lucide-react";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";

/**
 * HelpHint — tiny inline help icon with a Shadcn tooltip. Drop next to dense
 * metric labels (TP %, scanner thresholds, Doctor categories, etc.) so users
 * can hover for plain-English explanations without cluttering the layout.
 *
 *   <HelpHint label="What is this?">Detailed explanation here…</HelpHint>
 *
 * Designed for the cyberpunk dark console aesthetic: small (10px) icon,
 * mono-font tooltip body, max 280px wide.
 */
export default function HelpHint({ children, label = "help", side = "top", className = "" }) {
  return (
    <Tooltip delayDuration={120}>
      <TooltipTrigger asChild>
        <span
          role="img"
          aria-label={label}
          className={`inline-flex items-center align-middle text-neutral-600 hover:text-neutral-300 transition-colors cursor-help ${className}`}
          tabIndex={0}
        >
          <HelpCircle className="w-3 h-3" strokeWidth={2} />
        </span>
      </TooltipTrigger>
      <TooltipContent
        side={side}
        className="max-w-[280px] bg-neutral-900 border border-neutral-700 text-neutral-200 font-mono text-[11px] leading-relaxed px-2.5 py-1.5 whitespace-normal"
      >
        {children}
      </TooltipContent>
    </Tooltip>
  );
}
