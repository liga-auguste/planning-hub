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

/* The null guard is setSimDate's: the control it was clicked from is an
 * optional argument — a moment tile, the banner's "Zurück", or nothing at
 * all — and its failure branch should not have to repeat the check its
 * optimistic paint already makes. */
function flashActionFailed(el) {
    if (!el) return;
    el.classList.add('action-failed');
    setTimeout(() => el.classList.remove('action-failed'), 1500);
}
