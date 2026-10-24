import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// Testing Library's async wrapper drains with setTimeout(0) and only knows to
// advance *Jest's* fake timers; without this, user-event under vi fake timers
// waits forever.
(globalThis as { jest?: unknown }).jest = {
  advanceTimersByTime: (ms: number) => vi.advanceTimersByTime(ms),
};

afterEach(() => cleanup());
