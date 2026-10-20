import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
  document.head.querySelectorAll('meta[name="tsc-token"]').forEach((m) => m.remove());
});
