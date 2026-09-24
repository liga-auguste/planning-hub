/* The date picker, once, for every surface that shows a task date (#266).
 *
 * What is shared is the *asking*, not the consequence. Swapping the button
 * for an <input type="date">, opening it, tracking which device opened it
 * and swapping back is identical wherever a date is rendered; what a
 * successful move then means is not. The dashboard re-sorts the row,
 * repaints the dot and rewrites its figures; the close-out triage list
 * greys a row and relabels a button; /mein-plan/ and the AI summary reload.
 * So the module asks and hands the answer to a callback:
 *
 *     bindTaskDatePickers(onPick, options)
 *     onPick(taskId, isoDate, dueEl, row) -> Promise<boolean>
 *
 * A falsy answer means the move did not happen. The display element goes back
 * either way — on a resolved answer, a falsy one and a thrown one alike — so
 * onPick owns the failure feedback and this file owns the promise that no
 * surface can leave a bare date input standing where the date was.
 *
 * While onPick runs, the input is marked `pending` (#198). That half is shared
 * because the wait is: every surface's move is the same two Notion round trips.
 * What the wait ends in is not, which is why the flash stays with onPick.
 *
 * Do not move a surface's reschedule() in here. It is the consequence, and
 * every surface's is different; the dashboard's alone depends on six of its
 * own helpers.
 *
 * Bound by the `.task-due[data-task-id]` contract _task_due.html writes, so
 * a new surface is an include plus one call — never a second copy of this.
 */

// Which device the user is driving with, read off the events themselves
// rather than off the element afterwards (#200). Two earlier attempts asked
// the button, and both got it wrong somewhere:
//
//   :focus-visible            — false on the actions menu's route, because
//                               there the button is clicked by script
//                               (dueEl.click(), #239) and never focused at
//                               all. The keyboard user landed on <body>.
//   :focus:not(:focus-visible) — correct in Chrome, wrong in Safari on macOS,
//                               where clicking a <button> does not focus it.
//                               The test is then false for a plain mouse
//                               click too, so the ring came back.
//
// Neither is a bug in the browsers: an element simply cannot say how the
// click that reached it was produced. The events can. capture:true so the
// modality is current even for a handler that stops propagation, and
// pointerdown/keydown rather than click/keyup so it is set before the click
// the listener below acts on.
let lastInputWasKeyboard = false;
document.addEventListener('keydown', () => { lastInputWasKeyboard = true; }, true);
document.addEventListener('pointerdown', () => { lastInputWasKeyboard = false; }, true);

// `within` and `exclude` are what let one page bind two behaviours without
// depending on the order the two calls are made in: the dashboard's summary
// reloads while its rows patch in place, and each call names the region it
// owns rather than claiming everything that is left.
function bindTaskDatePickers(onPick, {rowSelector = '.task-row', within = null, exclude = null} = {}) {
    document.querySelectorAll('.task-due[data-task-id]').forEach(dueEl => {
        if (within && !dueEl.closest(within)) return;
        if (exclude && dueEl.closest(exclude)) return;
        dueEl.addEventListener('click', () => {
            // Read before the swap below detaches the button from its row.
            // closest() called on it afterwards finds nothing at all, which
            // is also why the row is handed to onPick rather than looked up
            // there.
            const row = dueEl.closest(rowSelector);
            const input = document.createElement('input');
            input.type = 'date';
            input.value = dueEl.dataset.rawDate;
            input.className = 'task-due-input';
            // Give focus back to a keyboard user, and to nobody else:
            // swapping the input out leaves focus on a detached element, so
            // without this the next Tab starts at the top of the document. A
            // mouse user asked for none of that — focusing the button again
            // would leave a ring behind that a pointer never requested, and
            // :focus-visible would hide the ring but not the focus itself.
            //
            // Read once, here, rather than again when the input closes: Tab
            // is a keydown, so a mouse user who opens the picker and then
            // tabs away would flip the modality and be pulled back to the
            // button by the very keystroke that meant to leave it. The
            // interaction that opened the picker is the one that owns where
            // focus goes when it shuts.
            const cameFromKeyboard = lastInputWasKeyboard;
            const restore = () => { if (cameFromKeyboard) dueEl.focus(); };
            // The way back, once — whichever of change and blur reaches it
            // first. Both fire for a pick that is followed by a click
            // elsewhere, and a second call would re-run restore() and drag a
            // keyboard user's focus back off whatever they had just moved to.
            // replaceWith() on a detached input is already a no-op; the focus
            // is the half that needs the guard.
            let swappedBack = false;
            const swapBack = () => {
                if (swappedBack) return;
                swappedBack = true;
                input.replaceWith(dueEl);
                restore();
            };
            dueEl.replaceWith(input);
            // Both listeners before the input is focused and opened, because
            // the two lines below are the ones that can throw: showPicker()
            // rejects a call it does not consider user-activated, and an
            // input with no way back would then sit in the row in place of
            // the date until the next page load. Registered first, a throw
            // costs the picker and nothing else — the user clicks away, blur
            // fires, the date returns.
            input.addEventListener('change', async () => {
                // onPick owns what the new date means, this owns putting the
                // display element back. try/finally rather than a plain
                // await: a callback that *throws* rather than answering falsy
                // would otherwise skip the swap back and leave the picker
                // wedged in the row. Every surface's onPick has a path there
                // — response.json() on a 200 that is not JSON, or any of the
                // dashboard's own patching helpers — and the point of this
                // module is that no surface has to know that.
                //
                // #198: the write is visible while it runs. A reschedule is
                // two Notion round trips (increment_postpone_count is
                // read-then-write, notion.py), so the row sat there holding
                // the newly picked date with nothing saying it was being
                // saved. Marked rather than `disabled`: disabling blurs the
                // input, and blur is the very thing that swaps the display
                // element back — mid-request. Cleared in the same `finally`
                // for the same reason it exists, so a thrown callback cannot
                // leave the input marked either.
                input.classList.add('pending');
                input.setAttribute('aria-busy', 'true');
                try {
                    await onPick(dueEl.dataset.taskId, input.value, dueEl, row);
                } finally {
                    input.classList.remove('pending');
                    input.removeAttribute('aria-busy');
                    swapBack();
                }
            });
            input.addEventListener('blur', swapBack);
            input.focus();
            // Needs transient user activation, which the click that got us
            // here supplies — a <button> gives the keyboard the same thing
            // on Enter and Space without a line of code for it (#195, #200).
            // Last, so nothing this handler still owes is behind it.
            input.showPicker();
        });
    });
}
