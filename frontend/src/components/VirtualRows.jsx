import { useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";

/**
 * Windowed list helpers (P3.4). Only the rows inside the scroll container (+ overscan) are mounted, so a 60-row
 * launch tape, a 200-row scanner or a long trade history costs the same as ~15 rows. Row heights are measured
 * (badges wrap), so the estimate only has to be in the right ballpark.
 */
export function VirtualUl({ items, estimate = 56, renderItem, maxHeightClass, className = "", overscan = 8, testId, empty }) {
  const scrollRef = useRef(null);
  const v = useVirtualizer({ count: items.length, getScrollElement: () => scrollRef.current, estimateSize: () => estimate, overscan });
  const rows = v.getVirtualItems();
  return (
    <div ref={scrollRef} className={`overflow-y-auto [contain:layout] [overscroll-behavior:contain] ${maxHeightClass} ${className}`} data-testid={testId}>
      {items.length === 0 && empty}
      <ul style={{ height: v.getTotalSize(), position: "relative" }}>
        {rows.map((row) => (
          <li key={row.key} data-index={row.index} ref={v.measureElement}
              style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${row.start}px)` }}>
            {renderItem(items[row.index], row.index)}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Table body windowing via spacer rows: keeps native <table> layout / sticky header intact. */
export function useVirtualTable(scrollRef, count, estimate = 30, overscan = 10) {
  const v = useVirtualizer({ count, getScrollElement: () => scrollRef.current, estimateSize: () => estimate, overscan });
  const rows = v.getVirtualItems();
  const padTop = rows.length ? rows[0].start : 0;
  const padBottom = rows.length ? v.getTotalSize() - rows[rows.length - 1].end : 0;
  return { rows, padTop, padBottom, measure: v.measureElement };
}

export function SpacerRow({ height, colSpan }) {
  return height > 0 ? <tr aria-hidden style={{ height }}><td colSpan={colSpan} className="p-0 border-0" /></tr> : null;
}
