/* Adding a task, once, for every surface that shows a plan (#148).
 *
 *     bindTaskAddRows(csrfToken, {serialize})
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
 * `serialize` wraps the fetch in whatever the surface uses to keep its
 * session writes from overlapping (#156). An add writes the session in a
 * demo session, and so does the background request that fetches the AI
 * summary; Django saves the whole session dict per response, so two in
 * flight at once mean the later save drops the earlier one's — an add that
 * answered 200 and reloaded, with the task gone from the plan. The surfaces
 * serialise differently (the dashboard has a queue, /mein-plan/ one promise),
 * which is why this takes the wrapper rather than one of them. Defaults to
 * running the fetch straight, so a surface with no background writer passes
 * nothing.
 *
 * When the row reaches the cross-project work list (#53's follow-up) with a
 * consequence of its own, that is the moment a callback earns itself. Not
 * before: a split invented for a second caller that does not exist yet is a
 * shape nobody can check.
 *
 * The date is the one thing this module does not own end to end (#279): the
 * swap, the picker and the way back are task_date_picker.js's
 * openTaskDatePicker(), the same code every task row's date goes through.
 * This file binds it rather than being bound by bindTaskDatePickers(),
 * because that function's contract is a task id and the add row has no task
 * yet — see the partial's comment on why it deliberately carries none.
 *
 * Bound by the `.task-add-row[data-project-id]` contract
 * _task_add_row.html writes, which is also where the two conditions live
 * that decide whether a row is rendered at all.
 */

function bindTaskAddRows(csrfToken, {serialize = fn => fn()} = {}) {
    document.querySelectorAll('.task-add-row[data-project-id]').forEach(row => {
        const nameEl = row.querySelector('.task-add-name');
        const dateEl = row.querySelector('.task-add-date');
        const submitEl = row.querySelector('.task-add-submit');
        // The server rendered today into the button; everything after a pick
        // is kept here rather than read back off the display element, so the
        // value that is sent is never parsed out of a formatted label.
        let iso = dateEl.dataset.rawDate;

        // The "row" role of date_format.py, in the client. The names come
        // from the server (the partial's data-weekdays/data-months), so what
        // is duplicated is this one template literal and nothing else — a
        // test pins it against format_date(..., role="row"). getDay() counts
        // from Sunday and WEEKDAYS_SHORT from Monday, hence the shift.
        const WEEKDAYS = row.dataset.weekdays.split(',');
        const MONTHS = row.dataset.months.split(',');
        const formatRowDate = isoDate => {
            // Noon, so a UTC-negative offset cannot land the parsed date on
            // the previous day the way midnight would.
            const d = new Date(isoDate + 'T12:00:00');
            return `${WEEKDAYS[(d.getDay() + 6) % 7]}, ${d.getDate()}. ${MONTHS[d.getMonth()]}`;
        };

        dateEl.addEventListener('click', () => {
            openTaskDatePicker(dateEl, picked => {
                // A cleared picker answers with an empty string. Dropped
                // rather than stored: an empty label would leave a button
                // with nothing to click on, and the one value the row needs
                // is the one it already has.
                if (!picked) return false;
                iso = picked;
                const label = formatRowDate(picked);
                dateEl.dataset.rawDate = picked;
                // Both in one go, for reschedule()'s reason (dashboard.html):
                // an aria-label overrides the element's own text as the
                // accessible name, so writing only textContent would leave
                // the button reading the new date and announcing the old one.
                dateEl.textContent = label;
                dateEl.setAttribute('aria-label', `Fällig am, aktuell ${label}`);
                return true;
            });
        });

        const submit = async () => {
            const name = nameEl.value.trim();
            // Only the name can be empty now. The date is rendered as today
            // and a cleared pick is dropped above, so #148's second refusal
            // has nothing left to catch — add_task_view still enforces both.
            if (!name) { flashActionFailed(nameEl); nameEl.focus(); return; }
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
                response = await serialize(() => fetch('/task/add/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken},
                    body: JSON.stringify({project_id: row.dataset.projectId, name: name, date: iso}),
                }));
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
