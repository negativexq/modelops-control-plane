"use client";

import { Slider as SliderPrimitive } from "radix-ui";
import { cn } from "@/lib/utils";

/**
 * Weight picker drawn as a row of ticks that light up to the value (after
 * bencho.dev's tick slider). Built on Radix Slider, so keyboard, pointer and
 * screen-reader behaviour come for free; the ticks are pure decoration.
 */
export function TickSlider({
  value,
  onChange,
  min = 1,
  max = 50,
  id,
  className,
}: {
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  id?: string;
  className?: string;
}) {
  const ticks = max - min + 1;
  return (
    <SliderPrimitive.Root
      id={id}
      min={min}
      max={max}
      step={1}
      value={[value]}
      onValueChange={([v]) => onChange(v)}
      className={cn("group relative flex h-10 w-full touch-none items-center select-none", className)}
      aria-label="Canary traffic weight"
    >
      <SliderPrimitive.Track className="absolute inset-0 flex items-center justify-between gap-px">
        {Array.from({ length: ticks }, (_, i) => {
          const tickValue = min + i;
          const on = tickValue <= value;
          const major = tickValue % 10 === 0 || tickValue === min;
          return (
            <span
              key={tickValue}
              aria-hidden
              className={cn(
                "w-[2px] rounded-full transition-[background-color,height] duration-150",
                major ? "h-6" : "h-4",
                on ? "bg-canary" : "bg-foreground/15",
                tickValue === value && "h-8 bg-canary",
              )}
            />
          );
        })}
        <SliderPrimitive.Range className="absolute h-full" />
      </SliderPrimitive.Track>
      <SliderPrimitive.Thumb className="block h-10 w-3 rounded-sm opacity-0 outline-none focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring" />
    </SliderPrimitive.Root>
  );
}
