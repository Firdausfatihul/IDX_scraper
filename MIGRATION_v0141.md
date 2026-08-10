# v0.14.1 · Responsive Hotfix

v0.14.1 is a cumulative presentation-layer hotfix on top of v0.14.0. It can be installed directly over v0.10.0 or any later release, so v0.14.0 does not need to be installed first.

The hotfix does not change scraping, source selection, triage, duplicate suppression, prompts, scheduler behavior, database schemas, recovery semantics, or company isolation.

## What changed

- Adds strict width containment for every workspace, panel, grid, flex child, modal, and dynamic transcript region so long machine-generated strings cannot widen the page.
- Converts grid tracks that receive dynamic content to `minmax(0, 1fr)` and adds explicit `min-width: 0` boundaries.
- Allows long run IDs, announcement IDs, URLs, JSON event fields, file paths, audit text, and source links to wrap safely.
- Moves Library ledgers into local horizontal scrollers on narrow screens instead of allowing the entire page to overflow.
- Sizes Prompt Studio, Share, Profile, and Ticker Inspector dialogs against their available container rather than combining viewport widths with modal-shell padding.
- Adds responsive breakpoints for 760 px and 520 px, including single-column run controls, Observatory cards, result actions, company cards, and modal controls.
- Preserves horizontally scrollable tab/navigation strips where wrapping would make the controls harder to use.

No database migration is required.
