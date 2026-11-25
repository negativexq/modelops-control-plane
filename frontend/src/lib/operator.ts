"use client";

import { useSyncExternalStore } from "react";

/**
 * The operator's self-reported name, kept in localStorage and sent as the
 * X-Operator header on every request. It puts a name on manual actions in the
 * audit trail; it is not authentication and doesn't pretend to be.
 */

const STORAGE_KEY = "modelops.operator";
// Mirrors the control plane's validation (app/control_plane/api.py).
export const OPERATOR_PATTERN = /^[\p{L}\p{N}_ .@'-]{1,64}$/u;

const listeners = new Set<() => void>();

export function getOperator(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setOperator(name: string | null) {
  const clean = name?.split(/\s+/).filter(Boolean).join(" ") ?? "";
  try {
    if (clean) window.localStorage.setItem(STORAGE_KEY, clean);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Private mode etc.: the name just won't persist.
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === STORAGE_KEY) listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

export function useOperator(): string | null {
  return useSyncExternalStore(subscribe, getOperator, () => null);
}
