/* Adding a task, once, for every surface that shows a plan (#148).
 *
 *     bindTaskAddRows(csrfToken)
 *
 * The whole write, not just the asking. task_date_picker.js kept the
 * consequence with each surface because each surface's is genuinely
 * different — the dashboard re-sorts a row and rewrites its figures, the
 * triage list greys one and relabels a button. An add has one consequence
 * everywhere and always will while the endpoint answers the way it does:
 * add_task_view busts the caches and returns no figures, so there is
 * nothing to patch and the page reloads. Two identical fetches in two
 * templates would be exactly the duplication #233 and #266 were about.
 *
 * The token is the one thing that genuinely differs — the dashboard renders
 * a hidden input the page owns, /mein-plan/ only ever had the template
 * variable — so it is the argument.
 *
 * When the row reaches the cross-project work list (#53's follow-up) with a
 * consequence of its own, that is the moment a callback earns itself. Not
 * before: a split invented for a second caller that does not exist yet is a
 * shape nobody can check.
 *
 * Bound by the `.task-add-row[data-project-id]` contract
 * _task_add_row.html writes, which is also where the two conditions live
 * that decide whether a row is rendered at all.
 */

function bindTaskAddRows(csrfToken) {
    document.querySelectorAll('.task-add-row[data-project-id]').forEach(row => {
        const nameEl = row.querySelector('.task-add-name');
        const dateEl = row.querySelector('.task-add-date');
        const submitEl = row.querySelector('.task-add-submit');

        const submit = async () => {
            const name = nameEl.value.trim();
            const iso = dateEl.value;
            // Refused here rather than sent and answered with a 400: the
            // endpoint enforces the same two rules (add_task_view), this is
            // what keeps an empty click from costing a round trip.
            if (!name) { flashActionFailed(nameEl); nameEl.focus(); return; }
            if (!iso) { flashActionFailed(dateEl); dateEl.focus(); return; }
            // #198: the write is visible while it runs, and the row cannot
            // be submitted twice in the meantime. The double-submit case is
            // the client's precisely because create_task does not
            // deduplicate the way create_tasks does (notion.py) — a second
            // click would write a second page.
            if (row.classList.contains('pending')) return;
            row.classList.add('pending');
            submitEl.setAttribute('aria-busy', 'true');
            submitEl.disabled = true;
            let response;
            try {
                response = await fetch('/task/add/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken},
                    body: JSON.stringify({project_id: row.dataset.projectId, name: name, date: iso}),
                });
            } catch {
                // A rejected fetch (offline, server unreachable) never
                // reaches the .ok check — treat it exactly like an error
                // response (#159).
                response = null;
            }
            if (!response || !response.ok) {
                // What was typed stays in the fields, so a failure can be
                // retried rather than retyped. The row comes out of pending
                // for the same reason.
                row.classList.remove('pending');
                submitEl.removeAttribute('aria-busy');
                submitEl.disabled = false;
                flashActionFailed(submitEl);
                return;
            }
            // Deliberately still pending: the reload is what clears it, and
            // an enabled button between the confirmed write and the new
            // page is a second click nothing would catch.
            window.location.reload();
        };

        submitEl.addEventListener('click', submit);
        // Enter from the name field submits the row. Not a <form>: every
        // write on these pages is a fetch, and a real form's default submit
        // would navigate away mid-request.
        nameEl.addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); submit(); }
        });
    });
}
