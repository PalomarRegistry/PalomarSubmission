# Verso's own stylesheets, as one bundle shipped them

`highlighter.css` is the inline `<style>` block `verso-html` emits into a
rendered module, and `code.css` is the page-shell stylesheet it writes beside
it. Both were taken from
`renders/PALOMAR-2026-08-08-000001-v2/406a7704e90ea28d8fc254402b5e33f74a16e1410ab843b6e8e5261a7a1ff6cf/`
in PalomarDatabase, rendered by Verso
`916bb962ceb8b88e6a731db6d28e862f99e834c4`.

They are here as evidence, not as input: nothing in `render_challenge.py` reads
them. `test_render_challenge.py` uses them to check that every selector Verso
paints a colour with is one the injected stylesheet answers, or one this
repository has decided on purpose to leave alone. Without them a test can only
confirm that the rules Palomar wrote are well formed, which is exactly the
question that was not in doubt.

Refresh them when the pinned Verso moves and its stylesheets change. The test
will name any selector the new version adds; decide whether it is reachable
before adding it to either side.
