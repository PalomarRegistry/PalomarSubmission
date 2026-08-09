# Mathlib cache trust note

**Investigated:** 1 August 2026. Palomar's two current cache implementations,
reached from three workflow routes, were reconciled on 9 August 2026 against
Submission commit `02d4746ba8322af3ff3e2d173b45a9c553072393`.

**Implementation inspected:** `leanprover-community/mathlib4` at
[`0be66d77ba290828a5260d883ace636f56bce89a`](https://github.com/leanprover-community/mathlib4/tree/0be66d77ba290828a5260d883ace636f56bce89a),
the Mathlib revision used by Palomar's first published entry. Deployment IAM,
credential holders, audit logging, object versioning, and retention were not
available in source and were not independently inspected.

## Decision

Palomar trusts the compiled contents returned by Mathlib's official cache. A
source build is not required before certification. The cache service, its
authorized publishers, and its stored objects are therefore part of Palomar's
trusted computing base independently of the governance of Mathlib's Git
repository.

“Trusted cache” in Palomar documentation means exactly this accepted trust
decision. It does not mean that an archive is cryptographically authenticated
against the corresponding source tree.

## What the cache key establishes

Mathlib computes a 64-bit module key from a root hash and the module's source
closure. At the inspected revision, the root hash includes `lakefile.lean`,
`lean-toolchain`, `lake-manifest.json`, the Lean compiler Git hash, and an
explicit generation counter. A module hash additionally includes its relative
path, normalized source contents, and imported-module hashes. See
[`Cache/Hashing.lean`](https://github.com/leanprover-community/mathlib4/blob/0be66d77ba290828a5260d883ace636f56bce89a/Cache/Hashing.lean)
and the repository's
[`Cache/README.md`](https://github.com/leanprover-community/mathlib4/blob/0be66d77ba290828a5260d883ace636f56bce89a/Cache/README.md#file-hashing).

This binds the *name requested* by the client to the pinned toolchain,
manifest, source path, source contents, and import closure. It prevents an
honest cache client from accidentally requesting an object for different
sources.

It does not bind the bytes returned at that name. The key names an `.ltar`
object but is not a digest of that object's bytes. The downloader fetches the
named object over HTTPS and passes it to the toolchain-bundled `leantar` for
unpacking; no detached signature or expected archive-content digest is checked
against the source-derived key. `leantar`'s corrupted-archive handling is an
integrity/format check, not publisher authentication. See
[`Cache/Requests.lean`](https://github.com/leanprover-community/mathlib4/blob/0be66d77ba290828a5260d883ace636f56bce89a/Cache/Requests.lean)
and
[`Cache/IO.lean`](https://github.com/leanprover-community/mathlib4/blob/0be66d77ba290828a5260d883ace636f56bce89a/Cache/IO.lean).

## Transport and publication authorization

The default Azure download endpoint is
`https://lakecache.blob.core.windows.net/mathlib4`; Mathlib also supports a
Cloudflare R2 backend and configurable mirrors. HTTPS authenticates the chosen
endpoint and protects the transfer from ordinary network modification.

Uploading is separately authorized. The inspected client accepts an Azure
OAuth bearer token, an Azure SAS token, or Cloudflare S3 credentials. Normal
Azure uploads use `If-None-Match: *`, while explicit overwrite commands are
supported for authorized publishers. These client-side facts do not establish
who currently holds credentials, what server-side roles they have, whether
object versioning is enabled, or how replacement is monitored.

## How Palomar consumes the cache

Palomar has two deliberately different download implementations. Submission
verification and its checked compatibility fixture share one implementation;
accepted-Challenge rendering uses the other. All three workflow routes accept
the cache bytes under the trust decision above; none derives or checks an
expected archive-content digest from the pinned source.

### Submission verification

The dispatched
[`submission.yml`](../.github/workflows/submission.yml) workflow and the
checked fixture in
[`compatibility.yml`](../.github/workflows/compatibility.yml), through
[`smoke_trusted_challenge.py`](../scripts/smoke_trusted_challenge.py), call
[`get_mathlib_cache`](../scripts/verify_submission.py). Before the
network-enabled phase, that function requires the selected Mathlib package to
be a real Git checkout whose `HEAD` equals the selected manifest revision,
whose origin is exactly `leanprover-community/mathlib4`, and whose Git-visible
worktree is clean. In ordinary verification, the package allowlist and its
official revisions were established earlier from the submitted and Mathlib
manifests; the compatibility fixture exercises the same function against its
checked repository and manifest. After those checks, the verifier deletes and
recreates each `.lake` tree with only empty `build` and `config` directories
for every package in Mathlib's verified official closure. This second reset
discards ignored files written by earlier candidate Lake elaboration and
occurs immediately before the verifier creates the closure links and enables
cache networking.

The verifier then makes the selected Mathlib package, not the submitted
project, the Lake workspace root and runs `lake exe cache get` inside the
combined Landrun/systemd boundary. The submitted project's Lakefile is not
elaborated in that network-enabled phase. Landrun and the transient systemd
unit forward only the named sandbox variables to the payload; those names do
not include GitHub, Azure, AWS, or Cloudflare credentials, and the workflow
supplies no cache credential. The network-enabled command can write only the
fresh `build` and `config` directories in the verified Mathlib closure; the
source trees and verifier-created closure links remain read-only. The selected
Mathlib client chooses its configured official download backend. After
download, the verifier builds the
official closure with network disabled before it grants the submitted project
access to those compiled outputs. The checked cold-build route plants
executable ignored files after materialization and requires both to be absent
after the real cache and trusted build phases; the focused regression also
asserts they are already absent at the network-enabled invocation.

### Accepted-Challenge rendering

The dispatched
[`render-challenge.yml`](../.github/workflows/render-challenge.yml) workflow
uses the narrower three-stage path in
[`render_challenge.py`](../scripts/render_challenge.py):

1. Under Landrun/systemd with network disabled, the cache client selected by
   the accepted dependency checkout is forced to a local empty `file://`
   backend. Palomar treats its output only as a request-key declaration: it
   parses the attempted 16-hex archive names and requires the reported count to
   equal the unique key set, with a hard limit of 10,000 archives.
2. A verifier-selected `curl`, outside candidate execution but still inside a
   resource-limited systemd unit, fetches exactly those names from
   `https://lakecache.blob.core.windows.net/mathlib4/f/`. It runs under
   `env -i`, invokes `curl --disable` so the preserved `HOME` cannot supply a
   `.curlrc`, sends no cache credential, and permits only HTTPS. A systemd
   `LimitFSIZE` makes the 256 MiB per-file limit fail closed even when the
   server omits a usable length; `curl --max-filesize` also rejects a declared
   oversize response before transfer where possible. The 8 GiB aggregate is
   checked after the bounded individual downloads, so it rejects an oversized
   result but does not prevent those bytes from first crossing the network or
   consuming the phase's wall-time budget. These resource controls do not
   authenticate archive meaning.
3. With network disabled again, the selected cache client unpacks the
   downloaded files and the renderer performs its Lean/Verso build. On a
   successful unpack, the download directory is removed immediately afterward.

This split prevents Lake, Lean, submitted code, and cache-client code from
holding network access during rendering. It does not make the fixed Azure bytes
cryptographically equivalent to the pinned Mathlib source.

The mechanical-verification artifact records the source/dependency revisions
and workflow URL. The render artifact additionally records cache archive counts
and aggregate downloaded bytes. Neither artifact contains the downloaded
archives or an authenticated per-archive digest. The workflows currently ask
GitHub Actions for 90-day mechanical-report and 30-day render-artifact
retention; those are operational artifact settings, not a cache-authentication
control or an author-facing availability promise.

## Failure and recovery implications

An unavailable or malformed cache object makes verification fail as
infrastructure; it cannot create a passing report by itself. An attacker or
operator with cache publication authority could, however, serve a well-formed
compiled module whose meaning differs from the pinned source. Lean can accept a
well-typed imported compiled definition, so kernel replay alone does not prove
that the cached definition matches Git.

Operational recovery from a suspected object consists of disabling or fixing
the affected backend, rebuilding and republishing the object from the pinned
source, and rerunning affected Palomar verifications. Palomar currently records
the Mathlib source revision and workflow run, not an authenticated digest of
every downloaded archive. Incident response must therefore conservatively
identify affected runs by cache key/revision and time window unless the cache
service's own object history supplies stronger evidence.

## Residual questions for cache operators

The following are deliberately recorded as trusted operational assumptions,
not claims established by this review:

- which people and automation identities can create or overwrite official
  cache objects;
- whether least-privilege roles separate ordinary publication from overwrite;
- whether Azure/R2 object versioning, immutability, or retention is enabled;
- what alerts and audit logs cover object creation, replacement, and deletion;
- how a compromised credential or bad object is revoked and how affected keys
  are enumerated.

Recording an aggregate digest of the archives consumed by each verification
would improve incident attribution, but would not authenticate those archives.
It can be added independently if the cache client exposes a stable, bounded
manifest of the fetched objects.
