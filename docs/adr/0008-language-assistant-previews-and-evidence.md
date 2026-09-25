# Language requests produce previews; cleaning records remain evidence

The Claude Agent SDK with Sonnet translates a request into a bounded cleaning plan or
answers from supplied cleaning records. It has no robot, shell, filesystem or MCP tools.
Its output is validated against known room IDs, supported modes and pass limits before
the web app displays it. The user reviews the same plan used by manual controls and
explicitly confirms before an existing cleaning endpoint receives a request.

The shared clean use case reads cleaning and requested carpet settings back before
departure. A mismatch prevents the start. Control requests share the existing run slot
while applying settings so concurrent app requests cannot overwrite the reviewed plan.

An app `CleaningRun` proves a start request was accepted, not completion. Robot history
is a separate `CleanRecord` behind `CleaningHistoryPort`. Its completion flag, error,
duration and area can support a result summary; its lack of room attribution must not
be filled by guessing from nearby app requests. Cached records are explicitly stale
after a refresh failure. Language summaries receive both evidence types separately.

The assistant is optional. Room controls work without Claude authentication or while
the model is unavailable. Prompts, room names and recent records are sent to Claude
only when the user asks; the UI discloses this next to the prompt.
