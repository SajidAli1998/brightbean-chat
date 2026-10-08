/**
 * Save state, undo/redo, the stats toggle and Publish.
 *
 * Two pieces of copy here are load-bearing. "Saved" and "N problems" are shown
 * side by side rather than folded into one status, because a 200 from the API
 * means the draft *was written* and may still carry graph-stage errors — a
 * draft is allowed to be half-wired. And Publish stays enabled with known
 * errors: what the builder knows is only as of the last save, so disabling it
 * would be a claim it cannot support.
 *
 * An unchanged published version needs no repeat publish unless all its
 * configured triggers are off. In that case the action turns them on, and the
 * status says plainly why the published flow cannot start yet.
 */
import { useState } from "react";

import { ApiError } from "./api/client";
import { TestOnChannel } from "./TestOnChannel";
import type { ValidationPayload } from "./schema/types";
import { publishFlow } from "./api/flows";
import { publishView } from "./publishState";
import { showToast } from "./toast";
import type { Autosave } from "./persistence/autosave";
import { useBuilder, useBuilderStore } from "./store/context";

export function Toolbar({ autosave }: { autosave: Autosave | null }) {
  const store = useBuilderStore();
  const save = useBuilder((state) => state.save);
  const canEdit = useBuilder((state) => state.env.canEdit);
  const statsVisible = useBuilder((state) => state.statsVisible);
  const statsFailed = useBuilder((state) => state.statsFailed);
  const canUndo = useBuilder((state) => state.past.length > 0);
  const canRedo = useBuilder((state) => state.future.length > 0);
  const errorCount = useBuilder((state) => state.validation.errors.length);
  const warningCount = useBuilder((state) => state.validation.warnings.length);
  const flowStatus = useBuilder((state) => state.flow?.status);
  const triggers = useBuilder((state) => state.triggers);
  const view = publishView(save, flowStatus, triggers.length, triggers.filter((trigger) => trigger.enabled).length);
  const [publishing, setPublishing] = useState(false);

  const publish = async () => {
    const enablingOnly = view.publishLabel === "Turn on triggers";
    setPublishing(true);
    try {
      // Flush first, and stop if it did not land. Publishing a draft the server
      // has not seen publishes the *previous* version — and then reports
      // success, which is worse than doing nothing.
      if (autosave && !(await autosave.flush())) {
        store.getState().setSave({
          message: "Not set live: your latest changes could not be saved. Fix the problems below and try again.",
        });
        return;
      }
      const result = await publishFlow(store.getState().env);
      store.getState().applyValidation(result.validation, store.getState().revision);
      // The flow itself, not just the save slice. services.publish() moves a
      // draft *or an archived* flow to active, and the response carries the
      // status it landed on — dropping it left the store reading "archived",
      // so the header this button sits in went on offering Publish for a flow
      // that had just gone live.
      store.getState().setFlow(result.flow);
      store.getState().setTriggers(result.triggers);
      store.getState().setSave({
        state: "saved",
        version: result.version,
        publishedVersion: result.version,
        message: null,
        issues: [],
      });
      // Report the action where the person pressed it, as well as in status.
      showToast({
        tone: "success",
        title: enablingOnly ? "Triggers turned on" : "Flow published",
        body: enablingOnly ? "All triggers are now on." : `Version ${result.version.version} is published.`,
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 422) {
        const payload = error.payload as { validation?: ValidationPayload } | null;
        if (payload?.validation) {
          store.getState().applyValidation(payload.validation, store.getState().revision);
        }
        store.getState().setSave({ message: "Not set live: fix the problems below and try again." });
      } else if (error instanceof ApiError) {
        store.getState().setSave({ message: error.message });
      }
    } finally {
      setPublishing(false);
    }
  };

  return (
    <div className="fb-toolbar">
      <span className={`fb-flow-status fb-flow-status-${view.statusTone}`} aria-live="polite">
        <span className="fb-flow-status-dot" aria-hidden="true" />
        {view.statusLabel}
      </span>
      <span className="fb-toolbar-divider" aria-hidden="true" />
      {canEdit ? (
        <>
          <button type="button" className="fb-toolbar-icon" aria-label="Undo" title="Undo" disabled={!canUndo} onClick={() => store.getState().undo()}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-2"/></svg>
          </button>
          <button type="button" className="fb-toolbar-icon" aria-label="Redo" title="Redo" disabled={!canRedo} onClick={() => store.getState().redo()}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 14 5-5-5-5"/><path d="M20 9H10a6 6 0 0 0 0 12h2"/></svg>
          </button>
        </>
      ) : null}

      <button
        type="button"
        className="fb-toolbar-icon"
        aria-label={statsVisible ? "Hide stats" : "Show stats"}
        title={statsVisible ? "Hide stats" : "Show stats"}
        aria-pressed={statsVisible}
        onClick={() => store.getState().toggleStats()}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V11M10 20V5M16 20v-8M22 20V8"/></svg>
      </button>

      {statsFailed ? <span className="fb-badge fb-badge-warning">Stats unavailable</span> : null}

      {/*
        Editors only. Testing runs the *draft* against a real chat and sends
        real messages, which is an edit-shaped act however read-only the
        surrounding canvas looks; the server enforces `edit_flows` on the
        endpoint either way.
      */}
      {canEdit ? <TestOnChannel /> : null}

      <span className="fb-toolbar-meta">
        {errorCount > 0 ? <span className="fb-badge fb-badge-error">{errorCount} to fix</span> : null}
        {warningCount > 0 ? <span className="fb-badge fb-badge-warning">{warningCount} to check</span> : null}
        <span data-save-state={save.state}>
          {view.saveLabel}
        </span>
        {canEdit ? (
          <button
            type="button"
            className="btn-pill-primary btn-pill-sm"
            disabled={publishing || view.publishDisabled}
            title={view.publishHint ?? undefined}
            onClick={() => void publish()}
          >
            {publishing ? (view.publishLabel === "Turn on triggers" ? "Turning on…" : "Setting live…") : view.publishLabel}
          </button>
        ) : null}
      </span>
    </div>
  );
}
