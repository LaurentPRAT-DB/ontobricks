# Lakehouse Build Initial Loading State Design

## Goal

When Knowledge Graph → Build opens for a Lakehouse-backed domain, show only a
spinner and loading message until the initial triple-store information request
has completed successfully. Do not expose placeholder information or actions
while the page state is unknown.

## Scope

This change applies only to the Lakehouse Build panel and its initial
`/dtwin/databricks-build/info` request. It does not change Lakebase sync,
Lakehouse build-task polling, adjacency-task polling, or backend APIs.

## Page states

The panel has three explicit initial states:

1. **Loading** — visible by default in server-rendered HTML. It contains the
   shared spinner and `Loading triple store information...` text. The complete
   information-and-actions container is hidden with `d-none`.
2. **Ready** — entered only after a successful HTTP response with
   `data.success === true` and after all returned information has been rendered.
   The loading/error state is hidden and the complete panel content is revealed
   in one update.
3. **Error** — entered when the initial request fails, returns a non-success HTTP
   status, returns invalid JSON, or returns `data.success !== true`. The content
   remains hidden. A compact error message and Retry button replace the spinner.

The Retry button starts the same initial request and immediately returns the
panel to Loading. It is the only action available after an initial failure.

## Subsequent refreshes

After the first successful load, the content remains visible during manual
refreshes and status reloads that follow build completion. This avoids hiding
the panel or causing layout flicker once valid information has already been
shown. A later refresh failure keeps the previously loaded content visible and
uses the existing error logging behavior.

## Markup and behavior

`_query_databricks_build.html` will contain:

- an initially visible loading-state container;
- an initially hidden error-state container with Retry;
- an initially hidden content container wrapping the existing header, actions,
  progress, timed log, result, storage summary, readiness, and status markup.

The HTML defaults prevent a flash of placeholder controls before JavaScript
runs. `query-databricks-build.js` will own a small initial-state transition
function and a boolean recording whether information has loaded successfully.
The existing `loadDatabricksBuildInfo()` function will treat unsuccessful HTTP
and API responses as errors rather than silently revealing incomplete content.

No new CSS is required. The implementation reuses Bootstrap display, alert,
button, spacing, and shared OntoBricks spinner classes.

## Accessibility

The loading state uses `role="status"` and an `aria-live="polite"` message. The
error uses `role="alert"`. Hidden content uses Bootstrap `d-none`, removing its
buttons from keyboard navigation until the Ready transition.

## Verification

Automated frontend contracts will verify:

- loading is visible and content is hidden in server-rendered markup;
- an addressable error state and Retry button exist;
- successful initial loading reveals content only after rendering;
- failed initial loading shows Error and keeps content hidden;
- Retry invokes the same loader;
- subsequent refreshes do not hide already loaded content.

Browser verification will delay and fail the information request to confirm
the spinner-only and error-with-Retry states, then return success to confirm the
content appears as one unit at desktop and 375-pixel widths without console
errors or keyboard-accessible hidden actions.
