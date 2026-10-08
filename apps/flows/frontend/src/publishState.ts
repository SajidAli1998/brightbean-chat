/** What the toolbar says about publication, trigger reachability and the draft. */
import { UNSAVED } from "./persistence/autosave";
import type { SaveSlice } from "./store/store";

export type PublishTone = "success" | "warning" | "plain";

export interface PublishView {
  statusLabel: string;
  statusTone: PublishTone;
  saveLabel: string;
  publishDisabled: boolean;
  publishLabel: string;
  publishHint: string | null;
}

const SAVE_COPY: Record<string, string> = {
  clean: "No changes",
  dirty: "Unsaved changes",
  saving: "Saving…",
  saved: "Saved",
  rejected: "Not saved",
  error: "Save failed",
};

const PENDING: ReadonlySet<string> = new Set(UNSAVED);

export function publishView(
  save: SaveSlice,
  flowStatus: string | undefined,
  triggerCount = 0,
  enabledCount = 0,
): PublishView {
  const saveCopy = SAVE_COPY[save.state] ?? save.state;
  const pending = PENDING.has(save.state);
  const currentVersionPublished = !pending && save.version?.published === true;
  const allOff = triggerCount > 0 && enabledCount === 0;
  const saveLabel = pending
    ? saveCopy
    : save.version?.published
      ? saveCopy
      : save.version
        ? `${saveCopy} · Draft v${save.version.version}`
        : saveCopy;

  if (flowStatus === "archived") {
    return {
      statusLabel: "Archived",
      statusTone: "plain",
      saveLabel,
      publishDisabled: false,
      publishLabel: "Set live",
      publishHint: null,
    };
  }

  if (flowStatus === "active") {
    return {
      statusLabel: allOff ? "Published · Triggers off" : triggerCount === 0 ? "Published · No triggers" : "Published",
      statusTone: allOff || triggerCount === 0 ? "warning" : "success",
      saveLabel,
      publishDisabled: Boolean(currentVersionPublished && !allOff),
      publishLabel: currentVersionPublished && allOff ? "Turn on triggers" : "Set live",
      publishHint:
        currentVersionPublished && !allOff ? "This version is already published. Make a change to set it live again." : null,
    };
  }

  return {
    statusLabel: "Draft",
    statusTone: "plain",
    saveLabel,
    publishDisabled: false,
    publishLabel: "Set live",
    publishHint: null,
  };
}
