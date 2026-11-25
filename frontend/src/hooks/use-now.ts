"use client";

import { useEffect, useState } from "react";

/** Current time as state, ticking every `intervalMs`. Lets "running for 3m" and
 * open-ended Gantt bars advance without calling Date.now() during render. */
export function useNow(intervalMs = 5_000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
