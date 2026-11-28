# Tau Ceti cache trust note

**Decided:** 28 November 2026. Palomar trusts the compiled outputs that Tau
Ceti's trusted cache channel serves, the way it trusts Mathlib's official
cache ([`mathlib-cache-trust.md`](mathlib-cache-trust.md)).

## Decision

When a submission depends on `TauCetiProject/TauCeti`, Palomar may restore Tau
Ceti modules from the channel configured as `trusted_lake_cache` on that root
in [`allowed-challenge-repositories.json`](../allowed-challenge-repositories.json)
instead of compiling them. Restored outputs are trusted like outputs Palomar
built itself: they are frozen before candidate code runs, and they may define
what a Challenge statement means.

The trusted computing base therefore grows by:

- the `tauceti-cache` R2 bucket and the `cache.taucetiproject.org` custom
  domain, in the `tauceti` Cloudflare account;
- whoever can read the upload credential given to the trusted publisher
  (`LAKE_CACHE_TRUSTED_KEY` in TauCeti; today a bucket-scoped token, so it can
  also write the shared prefix and the shared key can also write this one);
- the workflows that produce and publish the trusted outputs: TauCeti's
  `nightly-verify-schedule.yml`, and TauCetiTools' `nightly-verify.yml` and
  `publish-lake-cache.yml` at `main`, plus TauCetiTools `main` as the source of
  the setup action and staging helpers they run;
- Tau Ceti's `main` branch, whose code runs unsandboxed in the clean build;
- Mathlib's official cache, which that build reads as Palomar does, through a
  GitHub Actions cache of Mathlib's archives that TauCeti's main CI saves
  before any Tau Ceti code runs.

"Trusted cache" means this accepted trust decision. As with Mathlib, nothing
authenticates an archive against the source it was built from.

## What the channel contains, and what it excludes

Tau Ceti's cache has two channels in the same bucket. The shared one
(`/artifacts`, `/revisions`) is written by merge-queue builds, which run
unmerged PR code while producing their outputs, and by main CI, which restores
from that same channel and builds incrementally on it. Tau Ceti's own nightly
check documents that a published shared map therefore has no independent
provenance, and on 24 November 2026 a merge-queue batch did publish two corrupt
archives there. Palomar never reads the shared channel.

The trusted channel (`/trusted/artifacts`, `/trusted/revisions`) has one
producer: the nightly clean build of `main`. That build restores no Tau Ceti
output from any cache (`LAKE_RESTORE_ARTIFACTS=false`, a workspace-local Lake
cache that is never fetched into), compiles every module from source, and must
pass its axiom and module-system audits. Its own `lake build -o` map and
archives are staged, and an isolated publisher job on a fresh runner uploads
them with `trusted: true`. That publisher refuses the trusted channel unless the
run is a scheduled or manual run on `main` without a source candidate, and an
unset trusted endpoint never falls back to the shared one. So no merge-queue or
PR build contributes to a trusted map, and no trusted map is built on another
cached output.

The separation of the two prefixes is enforced by workflow code. The current
credential is scoped to the bucket, not to a prefix. A separate bucket with its
own token would turn this into a credential boundary.

## How the cache key relates to the bytes

Lake keys each module's outputs by a 64-bit input hash of its source, options,
toolchain and imports' input hashes, and names archives by a 64-bit content
hash. Neither is cryptographic (`Lake/Build/Trace.lean` says so), and no
signature covers a map. As with Mathlib, the key binds the name an honest client
asks for to the source; it does not bind the bytes served under that name.

Because outputs are keyed by input hash, a map is usable for any revision, not
only the one it was published for: a module whose inputs are unchanged has the
same key, and any module whose inputs differ simply misses. Palomar relies on
this to use maps from revisions near the pinned one. The assumption is the one
Lake's own incremental builds and Mathlib's cache already make: distinct inputs
that reach the same trusted channel do not collide on a 64-bit hash.

## How Palomar consumes the channel

`build_allowlisted_roots` in
[`verify_submission.py`](../scripts/verify_submission.py) handles each
qualified root after its Lake state is reset and its exact manifest closure is
linked. For a root with `trusted_lake_cache`, before the usual build of the
imported modules:

1. It lists canonical-branch revisions near the pin from the local Git object
   store: the pin's first-parent ancestors and the fetched `main` head's
   first-parent descendants of the pin, nearest first, up to the configured
   window (2,048 each way, about a day and a half of Tau Ceti commits).
2. A verifier-selected `curl`, run under `env -i` with `--disable`, HTTPS only,
   inside the same bubblewrap and cgroup boundary with only one fresh
   directory writable, fetches those revisions' maps for the root's own
   `lean-toolchain` scope. It keeps the nearest ancestor and nearest descendant
   that have one, merging their entries. Maps that are oversized or not plain
   module-archive maps are ignored.
3. With network disabled, `lake cache add` records the merged map in a fresh,
   disposable Lake cache directory, naming a cache service whose endpoint does
   not exist. Lake therefore cannot fetch anything itself.
4. With network disabled, `lake build --no-build -v` over the same targets
   restores what is already in that directory and names, in its log, each
   further archive it would fetch. `curl` fetches exactly those names from the
   trusted artifact endpoint, and the probe repeats, one layer of the import
   graph per round. After eight rounds the rest of the selected maps' archives
   are fetched at once. Unexpected file names, links, special files or sizes
   fail closed; the 256 MiB per-file and 16 GiB total caps bound the downloads.
5. The ordinary `lake build` of the imported modules then runs as before, with
   network disabled and the cache directory readable only. Lake restores what
   it can and compiles everything else from the verified source. The cache
   directory is deleted afterwards, and the build tree must contain no symlink
   or hard link that escapes it.

Lake runs without network throughout, and the candidate's Lake configuration
never runs in these phases. Inherited `LAKE_CACHE_DIR`, `LAKE_CONFIG`,
`LAKE_NO_CACHE`, `LAKE_ARTIFACT_CACHE` and `LAKE_RESTORE_ARTIFACTS` values are
removed from every phase, so only this step can point Lake at a cache. Because
the cache stays read-only during the build, Lake never writes into Mathlib's
frozen outputs, and the restored Tau Ceti outputs are frozen by the existing
write protections before the candidate runs.

A missing channel, map or archive, a toolchain Palomar cannot scope, or a Lake
version without these commands only means more is built from source. The
mechanical report records, per root, the map revisions used, the number of
entries, probe rounds, archives and bytes fetched, and timings, under
`trusted_root_caches`.

## Failure and recovery implications

A tampered or wrong archive in the trusted channel could change what a
Challenge that imports Tau Ceti means, exactly as a tampered Mathlib archive
could. The comparator does not catch this, because both exports would read the
same compiled definitions; it does catch changes to outputs only the Solution
uses. Recovery is: remove or repair the affected trusted objects, rerun the
nightly clean build, and rerun affected verifications, identified by the map
revisions recorded in their reports.

## Residual questions for cache operators

- whether the trusted channel should move to its own bucket and token;
- whether R2 object versioning or retention is enabled for `tauceti-cache`;
- whether the edge-cache rule should also cover `/trusted/artifacts/`.
