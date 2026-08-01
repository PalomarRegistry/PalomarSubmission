# Mathlib cache trust note

**Investigated:** 1 August 2026.

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
