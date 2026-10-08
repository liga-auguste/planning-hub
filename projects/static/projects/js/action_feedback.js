/* How a write says it failed, once, for every surface that writes (#233).
 *
 * The rule the project already followed in four places and now follows in
 * all of them: a write that did not land reports it on the control that was
 * clicked, and nowhere else. No banner, no toast, no second feedback shape —
 * the answer belongs where the click was, so a visitor looking at the thing
 * they just pressed sees it.
 *
 *     flashActionFailed(el)
 *
 * The animation itself is `.action-failed` in dashboard.css, next to this
 * for the same reason: the triage list had the handler's shape but none of
 * the CSS, so it could not have reported a failure even if it had wanted to.
 * Both halves are inherited from base_dashboard.html now, so a new surface
 * gets the feedback without deciding anything about it.
 */

/* How long the mark stays, named rather than written twice: #283's project
 * date reloads the page after a *partial* failure — the write landed in
 * part and the bar's answer cannot be given again — and the reload has to
 * outlast the feedback or there would be nothing to see. The animation
 * itself is shorter (.action-failed, two 0.4s passes), so this is the
 * moment the mark comes off rather than the moment it stops moving. */
const ACTION_FAILED_MS = 1500;

/* The null guard is setSimDate's: the control it was clicked from is an
 * optional argument — a moment tile, the banner's "Zurück", or nothing at
 * all — and its failure branch should not have to repeat the check its
 * optimistic paint already makes. */
function flashActionFailed(el) {
    if (!el) return;
    el.classList.add('action-failed');
    setTimeout(() => el.classList.remove('action-failed'), ACTION_FAILED_MS);
}
