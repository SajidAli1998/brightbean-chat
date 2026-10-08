/** Toolbar labels keep publication and trigger reachability separate from saves. */
import { describe, expect, it } from "vitest";

import { publishView } from "./publishState";
import type { SaveSlice } from "./store/store";

const version = (n: number, published: boolean) => ({
  id: `v${n}`,
  version: n,
  published,
  updated_at: "",
});

function save(patch: Partial<SaveSlice> = {}): SaveSlice {
  return { state: "clean", version: null, publishedVersion: null, message: null, issues: [], ...patch };
}

describe("publishView", () => {
  const published = save({ version: version(2, true), publishedVersion: version(2, true) });

  it("labels a published flow with enabled triggers and disables a repeat publish", () => {
    const view = publishView(published, "active", 2, 1);

    expect(view.statusLabel).toBe("Published");
    expect(view.publishDisabled).toBe(true);
    expect(view.publishLabel).toBe("Set live");
  });

  it("calls out a published flow whose triggers are all off and offers to turn them on", () => {
    const view = publishView(published, "active", 2, 0);

    expect(view.statusLabel).toBe("Published · Triggers off");
    expect(view.publishDisabled).toBe(false);
    expect(view.publishLabel).toBe("Turn on triggers");
  });

  it("calls out a published flow with no triggers", () => {
    const view = publishView(published, "active", 0, 0);

    expect(view.statusLabel).toBe("Published · No triggers");
    expect(view.publishDisabled).toBe(true);
  });

  it("offers Publish again as soon as an edit is pending", () => {
    const view = publishView(save({ ...published, state: "dirty" }), "active", 2, 1);

    expect(view.publishDisabled).toBe(false);
    expect(view.saveLabel).toBe("Unsaved changes");
  });

  it("names a saved draft while still showing the published status", () => {
    const view = publishView(save({ version: version(3, false), publishedVersion: version(2, true) }), "active", 2, 0);

    expect(view.statusLabel).toBe("Published · Triggers off");
    expect(view.saveLabel).toBe("No changes · Draft v3");
  });

  it("says Archived even when a published version exists", () => {
    const view = publishView(published, "archived", 2, 1);

    expect(view.statusLabel).toBe("Archived");
    expect(view.publishDisabled).toBe(false);
  });

  it("uses Draft before publication", () => {
    const view = publishView(save(), "draft");

    expect(view.statusLabel).toBe("Draft");
    expect(view.saveLabel).toBe("No changes");
  });
});
