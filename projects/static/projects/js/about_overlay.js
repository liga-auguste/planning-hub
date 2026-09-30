/* The "Über dieses Projekt" overlay: opened from the sidebar, closed by its
 * × or by a click on the backdrop (#200).
 *
 * A file of its own rather than a script block per page because all four
 * pages carrying the sidebar extend base_dashboard.html, which is what
 * loads it — the same argument that moved the date picker in #266. It is
 * also the only place the binding can live at all: the opener is in
 * _sidebar_nav.html and the overlay in _about_overlay.html, so either
 * template holding it would have to reach into the other.
 *
 * Loaded after task_date_picker.js and reading its lastInputWasKeyboard,
 * which is a top-level `let` in the same classic-script scope. That is the
 * modality answer #257 settled on because it is the browser-independent one
 * — an element cannot say how the click that reached it was produced.
 *
 * Deliberately not a dialog: no focus trap and no Escape binding. The × is
 * a real <button>, so the overlay is already dismissable from the keyboard,
 * and a true modal is a larger change than #200 takes on.
 */
(function () {
    const overlay = document.getElementById('about-overlay');
    const opener = document.getElementById('about-open');
    if (!overlay || !opener) return;

    // Focus moves into the overlay on open and back to the opener on close.
    // Without the second half, hiding the overlay would leave focus on a
    // hidden element and the next Tab would start at the top of the
    // document — the gap task_date_picker.js's restore() closes for the
    // date. Gated the same way and for the same reason: a mouse user asked
    // for no ring, and re-focusing the opener would leave one behind.
    function open() {
        overlay.style.display = 'flex';
        if (lastInputWasKeyboard) overlay.querySelector('#about-close')?.focus();
    }

    function close() {
        overlay.style.display = 'none';
        if (lastInputWasKeyboard) opener.focus();
    }

    opener.addEventListener('click', open);
    overlay.querySelector('#about-close')?.addEventListener('click', close);
    // The backdrop is the overlay element itself, so only a click that
    // landed on it rather than on the card inside counts as "outside".
    overlay.addEventListener('click', (event) => {
        if (event.target === overlay) close();
    });
})();
