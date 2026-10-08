/** The persistent trigger summary, separate from step editing. */
import { useBuilder } from "../store/context";
import { TriggerSection } from "./TriggerSection";

export function TriggerSidebar() {
  const triggerCount = useBuilder((state) => state.triggers.length);
  const stepCount = useBuilder((state) => state.nodeOrder.length);

  return (
    <aside className="fb-trigger-sidebar" aria-label="When it runs">
      <TriggerSection />
      {triggerCount === 0 && stepCount === 0 ? (
        <section className="fb-editor-start">
          <p className="fb-editor-start-title">Two things make a flow</p>
          <p className="fb-editor-start-body">
            Something that starts it, and something it does. Choose what starts it here, then add
            your first step on the canvas.
          </p>
        </section>
      ) : null}
    </aside>
  );
}
