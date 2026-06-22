# Uptrace trace search onboarding

## Request

Add a convenient way for an authenticated Agent Console user to look up an
Uptrace trace by trace ID without first navigating through Uptrace's overview.
The referenced deployment is `https://uptrace.performio.co`, and the screenshot
shows organisation/project route values `1/8`.

## Current application shape

- Django 6 application managed with `uv`.
- `ops` owns operational tools such as Locate Tenant, Lock Monitor, Squelch, and
  managed Commands. Trace lookup belongs in this app rather than `agents`.
- `ops/urls.py` contains the operational routes and `ops/views.py` uses
  function-based, `login_required` views with explicit HTTP method decorators.
- Full templates extend `agents/templates/base.html`; HTMX requests render
  `_partial.html` fragments into `#content`.
- Sidebar navigation is defined in `agents/templates/base.html`. Every HTMX
  navigation link includes `hx-indicator="#page-loader"` and a `data-navpath`.
- Settings currently contain development literals and do not yet establish an
  environment-variable convention for external services.
- The working tree contains substantial user work, including an in-progress
  move of operational features from `agents` into `ops`. Any implementation
  must preserve and build on that state.

## Uptrace findings

- Uptrace supports filtering spans using `_trace_id` and exposes distributed
  trace waterfall views.
- Current official material documents OpenTelemetry ingestion and generation of
  a trace URL from an active application span, but does not establish a stable,
  public read API for embedding Uptrace's trace waterfall in another product.
- Uptrace's own UI URL/query shape is therefore best treated as deployment- and
  version-specific configuration, not hard-coded application behavior.
- An iframe is a poor default because browser framing policy, Uptrace auth
  cookies, and SSO can prevent or weaken embedding. Rebuilding the trace viewer
  against undocumented internal endpoints would be brittle.

## Recommended first version

Create an `ops` Trace Lookup page with one field:

1. Accept a 32-character hexadecimal W3C trace ID (optionally normalize a full
   `traceparent` value by extracting its trace ID).
2. Validate it server-side and show inline errors with HTTP 422.
3. Build the target from a configured Uptrace URL template, with URL encoding.
4. Open or redirect to Uptrace in a new tab, where the user's existing Uptrace
   login/SSO session applies.
5. Keep credentials and ingestion DSNs out of the browser and source tree.

Suggested configuration:

```text
UPTRACE_TRACE_URL_TEMPLATE=https://uptrace.performio.co/<verified-route>?query=<encoded-trace-query>
```

The exact route/query should be captured from this Uptrace deployment by doing
one manual trace-ID search and copying the resulting browser URL. Keeping the
entire template configurable avoids coupling Agent Console to a particular
Uptrace release.

## Possible later enhancement

If the installed Uptrace version provides a supported authenticated read API,
add a server-side client and render a small summary (root service, operation,
duration, status, start time) in Agent Console, retaining “Open waterfall in
Uptrace” for the full trace. Confirm the API and its auth/permission model before
building this; do not consume private UI endpoints.

## Expected implementation surface

- `config/settings.py`: URL template loaded from environment.
- `ops/forms.py`: trace ID/traceparent normalization and validation.
- `ops/views.py`: authenticated GET/POST view with explicit error feedback.
- `ops/urls.py`: named trace lookup route.
- `ops/templates/trace_lookup.html` and
  `ops/templates/partials/_trace_lookup.html`: HTMX form and result.
- `agents/templates/base.html`: sidebar entry.
- `ops/tests.py`: validation, authentication, escaping/encoding, missing
  configuration, GET/POST behavior, and HTMX/full-template behavior.

## Verification

- Targeted `uv run pytest` tests for `ops`.
- `uv run ruff check` on touched Python files.
- `uv run pyright` if the project baseline permits it.
- Manual browser check of full and HTMX navigation plus a real trace deep-link.
