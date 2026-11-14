import { useUi } from "./store";

/** Operator results. A refusal quotes the database verbatim and stays up longer. */
export default function Toasts() {
  const toasts = useUi((s) => s.toasts);
  const dismiss = useUi((s) => s.dismissToast);
  return (
    <div className="toasts">
      {toasts.map((t) => {
        const refused = t.result.refused ?? null;
        const tone = t.result.ok ? "good" : refused ? "bad" : "warn";
        const repeated = refused !== null && t.result.message.includes(refused.message);
        return (
          <div key={t.id} className={`toast glass ${tone}`} role={t.result.ok ? "status" : "alert"}>
            <span className="toast-title">{t.title}</span>
            <button type="button" className="icon-btn" onClick={() => dismiss(t.id)} aria-label={`Dismiss: ${t.title}`}>
              <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden="true">
                <path d="M2 2l8 8M10 2l-8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
              </svg>
            </button>
            {!repeated && <p className="toast-msg">{t.result.message}</p>}
            {refused && (
              <>
                <span className="toast-rule">Refused by Atlas · rule: {refused.rule}</span>
                <pre className="verbatim">{refused.message}</pre>
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
