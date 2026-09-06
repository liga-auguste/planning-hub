"""How a local `.env` and the ambient environment settle a disagreement.

Applied by `manage.py` before Django starts. Its own module rather than six
lines in the entry point, so the rule can be tested without running
`manage.py` as a subprocess.
"""

# A suffix, not a list of names: a credential added later is covered the day
# it appears, and every secret this project reads is named *_API_KEY
# (ANTHROPIC_API_KEY, NOTION_API_KEY). SECRET_KEY and DB_PASSWORD stay
# outside it on purpose — they are per-deployment configuration, and taking
# away the ability to set them for one invocation is the very cost this rule
# exists to avoid.
CREDENTIAL_SUFFIX = "_API_KEY"


def apply_credentials(environ, values):
    """Lets `.env`'s credentials beat `environ`, and leaves switches alone.

    `load_dotenv()` fills in what the environment is missing and stops there,
    which is the opposite of what a project-local `.env` is for: a stale
    ANTHROPIC_API_KEY sitting in the login session shadows the working key in
    `.env`, and every Claude call comes back 401 with nothing in the code to
    point at. That happened twice by two routes — an `export` in `.zshrc`,
    then `launchctl setenv`, which appears in no profile file at all and
    cannot be grepped for.

    `load_dotenv(override=True)` would fix that and take something else away.
    `.env` carries DEMO_MODE too, and `DEMO_MODE=false manage.py test` is how
    the production test leg (PostgreSQL) is run locally; overriding it would
    run the SQLite leg instead — silently, because both legs pass, so the
    answer looks like the one that was asked for. Hence the line drawn here:
    a credential is ambient pollution, a switch is a decision made for this
    one invocation.

    Mutates `environ` rather than returning a mapping: the caller's `environ`
    is `os.environ`, which is what Django reads.
    """
    for name, value in values.items():
        if name.endswith(CREDENTIAL_SUFFIX) and value:
            environ[name] = value
