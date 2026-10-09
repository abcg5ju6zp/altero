"""What the instance holds, and what it costs on disk.

An operator running a server has one question the API never had to answer: how
much disk is this, and whose. Upstream cannot answer it honestly — group files
bill to the owner's quota, which is what the forums are full of complaints
about — and altero can, because files are stored once per digest.

So two numbers rather than one. **Nominal** is what each library would cost on
its own, summed: the answer to "what is this group costing us". **Real** is what
is on the disk, counting a file shared between libraries once: the answer to
"what do I have to buy". The difference is what deduplication saves, and it is
reported rather than hidden because an operator planning for growth needs to
know it is there.

Two things that do not add up are reported alongside: bytes on disk nothing
references any more, and attachments whose bytes are not there. The first is
where a self-hosted instance quietly loses disk; the second is what a restore
that lost its files looks like.

Nothing here reads an item. It counts rows and stats files, which is the whole
of what an instance administrator is entitled to — see
:mod:`altero.api.routes.webadmin`.
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from altero.models import (
    Collection,
    Item,
    ItemField,
    Library,
    LibraryType,
    StorageDeleteLease,
    StorageDownload,
    StorageUpload,
    Tag,
    User,
)
from altero.services.storage import (
    LEASE_CANDIDATE,
    LEASE_DELETED,
    STAGING_PREFIX,
    file_path,
)

logger = logging.getLogger("altero.storagestats")

#: Wait before the first retry of an unlink that failed, doubled per attempt
#: and capped at the grace period. A purge is an administrator pressing a
#: button rather than a timer, so this only keeps a persistently failing unlink
#: (a flaky mount, a permission problem) from being retried on every press; it
#: never grows past the wait a candidate already lived through.
UNLINK_RETRY_START = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class LibraryUsage:
    """What one library holds."""

    id: int
    type: LibraryType
    owner_id: int
    name: str
    version: int
    items: int
    trashed: int
    collections: int
    tags: int
    attachments: int
    #: Distinct digests this library references and that are on disk.
    files: int
    #: What those digests weigh. A file shared with another library is counted
    #: here in full and in the other library in full, which is what "what would
    #: this library cost on its own" means.
    bytes: int
    #: Digests it references with no bytes behind them.
    missing: int


@dataclass(frozen=True, slots=True)
class Usage:
    """What the whole instance holds."""

    libraries: list[LibraryUsage] = field(default_factory=list)
    users: int = 0
    groups: int = 0
    #: Sum of every library's own cost. Counts a shared file once per library.
    nominal_bytes: int = 0
    #: What is actually on disk and referenced. Counts a shared file once.
    real_bytes: int = 0
    #: Bytes on disk that no item in any library references.
    orphan_bytes: int = 0
    orphan_files: int = 0
    #: Digests some item claims that are not on disk.
    missing_files: int = 0
    #: Everything in the store, referenced or not.
    stored_files: int = 0
    stored_bytes: int = 0

    @property
    def saved_bytes(self) -> int:
        """What storing files once per digest has saved."""
        return self.nominal_bytes - self.real_bytes


def _is_digest(name: str) -> bool:
    """Whether a file name is one the store would have written."""
    return len(name) == 32 and all(c in "0123456789abcdef" for c in name)


def scan_store(root: Path) -> dict[str, int]:
    """Return the size of every stored file, by digest.

    One walk and one stat per file, because every caller wants all of it. A
    store nobody has uploaded to yet has no directory, which is not an error:
    a fresh instance is exactly that.

    Anything not named like a digest is not part of the store and is passed
    over — a stray note, an editor's backup, whatever an operator left in the
    directory. Counting those as orphans would put somebody else's file on a
    list of things to delete.
    """
    if not root.is_dir():
        return {}

    sizes: dict[str, int] = {}
    for path in root.rglob("*"):
        # Named by digest; see altero.services.storage.file_path.
        if path.is_file() and _is_digest(path.name):
            sizes[path.name] = path.stat().st_size
    return sizes


async def _digests_by_library(session: AsyncSession) -> dict[int, set[str]]:
    """Return the file digests each library references.

    A set per library: a library is charged for bytes, not for attachments, so
    the same file attached to two items in it costs it once.
    """
    rows = await session.execute(
        select(Item.library_id, ItemField.value)
        .join(ItemField, ItemField.item_id == Item.id)
        .where(ItemField.field == "md5", ItemField.value != "")
    )
    digests: dict[int, set[str]] = defaultdict(set)
    for library_id, digest in rows:
        digests[library_id].add(digest)
    return digests


async def _counts(
    session: AsyncSession,
    column: InstrumentedAttribute[int],
    condition: ColumnElement[bool] | None = None,
) -> dict[int, int]:
    """Return a per-library count of whatever ``column``'s table holds."""
    statement = select(column, func.count()).group_by(column)
    if condition is not None:
        statement = statement.where(condition)
    return {library_id: count for library_id, count in await session.execute(statement)}


def _sweep_staging(root: Path, cutoff: float) -> tuple[int, int]:
    """Delete files a write into the store never finished.

    `storage.store_file` writes under a name of its own and moves the file
    onto its digest, so one left behind is a process that died between the two
    -- or one still writing, which is why these are held to the same grace
    period as an orphan. They are not stored files: `scan_store` passes over
    anything not named like a digest, so nothing else would ever remove one.
    """
    if not root.is_dir():
        return 0, 0

    removed = freed = 0
    for path in root.rglob(f"{STAGING_PREFIX}*"):
        try:
            if not path.is_file():
                continue
            described = path.stat()
            if described.st_mtime >= cutoff:
                continue
            path.unlink()
        except OSError:  # pragma: no cover - a file somebody else removed
            logger.warning("Could not remove %s", path)
            continue
        removed += 1
        freed += described.st_size

    return removed, freed


def _now() -> datetime:
    """The moment a lease is measured against, in the form the column holds."""
    return datetime.now(UTC).replace(tzinfo=None)


def _mtime_at(epoch_seconds: float) -> datetime:
    """Turn a filesystem mtime into the same naive-UTC value the columns hold."""
    return datetime.fromtimestamp(epoch_seconds, UTC).replace(tzinfo=None)


def _retry_delay(grace: timedelta, attempts: int) -> timedelta:
    """How long after the ``attempts``th failed unlink to wait before trying."""
    return min(grace, UNLINK_RETRY_START * 2 ** (attempts - 1))


async def _owned_digests(session: AsyncSession, now: datetime) -> set[str]:
    """Every digest that must not be deleted at this moment.

    Three kinds of owner:

    * a field on an item in any library -- a personal one or a group's, and
      trashed items count, because their bytes are still restored from the
      trash and still synced;
    * an authorized, not-yet-registered upload, whose bytes can be on disk
      before the item field that names them;
    * a download permission that has not expired, handed out by the 302 a
      client is still following.

    The first set is what the old sweep checked; the other two are ownership
    that exists only while a request is in flight.
    """
    referenced = {
        digest for digests in (await _digests_by_library(session)).values() for digest in digests
    }
    uploading = set(await session.scalars(select(StorageUpload.md5).distinct()))
    downloading = set(
        await session.scalars(
            select(StorageDownload.md5).distinct().where(StorageDownload.expires >= now)
        )
    )
    return referenced | uploading | downloading


async def _prune_terminal(session: AsyncSession, now: datetime, grace: timedelta) -> None:
    """Forget leases whose deletion is older than one review window.

    A terminal row only exists to answer a request whose commit or response
    was lost and to leave a short record of what was decided; once it is as
    old as the grace period it has served both. This prunes by age alone -- a
    row whose bytes have since reappeared on disk is deleted here and then
    re-leased with a fresh identity by the acquisition pass in the same run.
    """
    await session.execute(
        delete(StorageDeleteLease).where(
            StorageDeleteLease.status == LEASE_DELETED,
            StorageDeleteLease.deleted_at.is_not(None),
            StorageDeleteLease.deleted_at < now - grace,
        )
    )
    await session.commit()


async def _acquire_candidates(
    session: AsyncSession,
    root: Path,
    old: dict[str, int],
    now: datetime,
    grace: timedelta,
) -> None:
    """Lease every old file nobody currently owns, and commit the leases.

    This is the first phase and deliberately ends its own transaction before
    any file is touched: the lease has to be durable before the decision pass
    locks it, so an upload arriving in between has something concrete to
    revoke -- and a process that dies here has only written rows, never
    removed bytes.

    The ownership snapshot narrows the candidates; it does not decide
    anything. A reference, upload or download appearing afterwards is what the
    decision pass re-checks under locks.
    """
    owned = await _owned_digests(session, now)
    leases = {lease.digest: lease for lease in await session.scalars(select(StorageDeleteLease))}

    for digest in old:
        if digest in owned:
            continue
        existing = leases.get(digest)
        if existing is not None and existing.status == LEASE_CANDIDATE:
            # Keep waiting on the decision already recorded.
            continue
        if existing is not None:
            # A `deleted` lease whose file is back on disk and old again: the
            # bytes are a new observation (a restore, a fresh upload), so the
            # terminal claim no longer describes them. Drop it and lease
            # afresh below.
            await session.delete(existing)

        try:
            described = file_path(root, digest).stat()
        except OSError:
            # It was on the scan a moment ago and is already gone; the next
            # scan does not list it, so there is nothing to lease here.
            continue
        session.add(
            StorageDeleteLease(
                digest=digest,
                status=LEASE_CANDIDATE,
                size=described.st_size,
                mtime_ns=described.st_mtime_ns,
                # mtime plus grace, not now plus grace: a file already older
                # than the grace is decidable in this same run.
                available_at=_mtime_at(described.st_mtime) + grace,
            )
        )
    try:
        await session.commit()
    except IntegrityError:
        # Another purge run acquired the same candidates at the same time.
        # Roll our duplicates back; whichever rows survived are decided by
        # the pass below in this same process.
        await session.rollback()


async def _locked_owner_exists(
    session: AsyncSession,
    model: type[StorageUpload] | type[StorageDownload],
    digest: str,
    now: datetime,
) -> bool:
    """Whether an in-flight upload/download row owns ``digest``, locking it.

    The lock is the point, not the answer. The upload path removes its row in
    the same transaction that revokes the lease and writes the item
    reference, and it takes the lease lock first -- so by the time this runs
    while holding that lock such a row cannot be deleted underneath the
    decision. Locking it as well makes that hold for anything else that
    removes an owner row (the retention sweep) without going through a lease.

    ``FOR UPDATE`` is emitted on PostgreSQL and dropped on SQLite, which
    serializes writers anyway.
    """
    statement = select(model.key).where(model.md5 == digest).with_for_update().limit(1)
    if model is StorageDownload:
        statement = statement.where(model.expires >= now)
    return await session.scalar(statement) is not None


async def _decide_candidate(
    session: AsyncSession, root: Path, digest: str, now: datetime, grace: timedelta
) -> tuple[str, int]:
    """Run the second phase for one mature lease. Returns (outcome, freed).

    ``outcome`` is ``removed`` (bytes unlinked this time), ``retained`` (an
    owner appeared -- candidate kept), ``released`` (the file is a different
    incarnation, or the claim was lost -- lease dropped), ``converged``
    (bytes already gone -- lease closed, nothing counted) or ``failed``
    (unlink refused -- retry recorded). Only ``removed`` ever reports freed
    bytes: a file missing for any other reason is not space this operation
    freed.

    The very first thing this does is *write*. It has to: ``FOR UPDATE`` is a
    real lock on PostgreSQL but nothing at all on SQLite, where WAL lets a
    read transaction watch another connection commit in the middle of it. A
    decision that only read before unlinking could therefore attach-and-commit
    against its own snapshot and still delete the newly referenced bytes --
    the original race. Starting the transaction with an UPDATE that matches
    only a still-pending candidate takes the decision lock on both backends
    (a row lock on PostgreSQL under READ COMMITTED, the database writer lock
    on SQLite) before a single recheck is read. The read transaction the
    candidate enumeration opened is closed first, so the UPDATE is the first
    statement of this transaction and never upgrades a stale snapshot.

    The transaction is closed with a commit rather than a rollback on entry:
    the session factory sets expire_on_commit=False, so a pending-free commit
    ends the read transaction without invalidating the objects the caller is
    still holding after the purge returns.

    Everything from the owner rechecks to the unlink then happens holding
    that lock, so an upload's revoke either committed before this began --
    the claim matches nothing -- or waits for this transaction and answers
    from the disk it reads after. The failure direction is always "keep the
    file": a failed check leaves the candidate and the bytes where they were.
    """
    await session.commit()
    claimed = await session.execute(
        update(StorageDeleteLease)
        .where(
            StorageDeleteLease.digest == digest,
            StorageDeleteLease.status == LEASE_CANDIDATE,
        )
        .values(updated=now)
    )
    if (getattr(claimed, "rowcount", 0) or 0) == 0:
        # Revoked by an upload, or decided by another purge, while this pass
        # was getting to it. Nothing is ours to delete.
        await session.commit()
        return "released", 0

    lease = await session.get(StorageDeleteLease, digest, populate_existing=True)
    if lease is None or lease.status != LEASE_CANDIDATE:
        # Defensive: the UPDATE matched, so this cannot normally happen.
        await session.commit()  # pragma: no cover - belt and braces
        return "released", 0  # pragma: no cover

    referenced = {
        each for digests in (await _digests_by_library(session)).values() for each in digests
    }
    if lease.digest in referenced:
        await session.commit()
        return "retained", 0
    if await _locked_owner_exists(session, StorageUpload, lease.digest, now):
        await session.commit()
        return "retained", 0
    if await _locked_owner_exists(session, StorageDownload, lease.digest, now):
        await session.commit()
        return "retained", 0

    path = file_path(root, lease.digest)
    try:
        described = path.stat()
    except OSError:
        # The bytes are already gone. This is the convergent state for a
        # process that unlinked and crashed before committing, for an
        # operator who removed the file, and for any other story the database
        # cannot distinguish -- which is exactly why it claims no bytes.
        lease.status = LEASE_DELETED
        lease.deleted_at = now
        lease.updated = now
        await session.commit()
        return "converged", 0

    if described.st_size != lease.size or described.st_mtime_ns != lease.mtime_ns:
        # A different incarnation at the same content-addressed name -- a
        # fresh atomic replace. It earns its own grace period; dropping the
        # lease lets a later sweep acquire one with a new identity.
        await session.delete(lease)
        await session.commit()
        return "released", 0

    try:
        path.unlink()
    except OSError as error:
        # Retryable, and recorded rather than retried immediately: attempts
        # and the backoff are what keep one bad mount from pinning the purge,
        # while the file and its candidate stay in place for the next try.
        lease.attempts += 1
        lease.last_error = f"{type(error).__name__}: {error}"[:255]
        lease.retry_after = now + _retry_delay(grace, lease.attempts)
        lease.updated = now
        await session.commit()
        logger.warning("Could not remove %s: %s", path, error)
        return "failed", 0

    lease.status = LEASE_DELETED
    lease.deleted_at = now
    lease.updated = now
    await session.commit()
    return "removed", lease.size


async def purge_orphans(session: AsyncSession, root: Path, *, grace: timedelta) -> tuple[int, int]:
    """Delete stored files no library references. Returns how many, and how big.

    Never on a timer, and never without ``grace``. A file reaches the disk
    before the row that refers to it is committed, so an upload in flight
    looks exactly like an orphan for as long as that request takes; anything
    younger than the grace period is not even leased, which is what keeps
    this from racing every client that is sending something.

    Deletion is now a lease in two phases rather than one read and an
    unlink::

        acquire  --  list old, unowned files; write a candidate lease each,
                     recording the file's size and mtime; commit
        decide   --  per candidate, under a row lock: re-read references and
                     lock the in-flight upload/download rows, re-stat the
                     file, and only then unlink -- marking the lease deleted

    The upload path takes that same lock to revoke a candidate when it takes
    ownership of the digest (`storage.claim_for_upload`), so the old race --
    a digest referenced just after the reference read and unlinked anyway --
    has both decisions serialized on one row. The matrix for what the decide
    pass finds is in `_decide_candidate`; the short version is that every
    branch except a confirmed unlink keeps the file and counts nothing, and a
    process killed at any point leaves rows the next run can finish from.

    Files left half written are swept here too, under the same grace period.
    They are the only other thing in the store that nothing else accounts for.
    """
    now = _now()
    cutoff = time.time() - grace.total_seconds()
    removed, freed = _sweep_staging(root, cutoff)

    sizes = scan_store(root)
    old = {
        digest: size
        for digest, size in sizes.items()
        if (path := file_path(root, digest)).is_file() and path.stat().st_mtime < cutoff
    }

    await _prune_terminal(session, now, grace)
    if old:
        await _acquire_candidates(session, root, old, now, grace)

    # Run even with an empty store: candidates left pointing at files a
    # crashed run already unlinked still have to converge here. Plain scalar
    # rows, not lease objects: each decision begins by rolling the read
    # transaction back, which expires everything in the identity map.
    due = list(
        await session.execute(
            select(
                StorageDeleteLease.digest,
                StorageDeleteLease.available_at,
                StorageDeleteLease.retry_after,
            )
            .where(StorageDeleteLease.status == LEASE_CANDIDATE)
            .order_by(StorageDeleteLease.digest)
        )
    )
    for candidate_digest, available_at, retry_after in due:
        if available_at > now:
            continue
        if retry_after is not None and retry_after > now:
            continue
        _, claimed = await _decide_candidate(session, root, candidate_digest, now, grace)
        if claimed:
            removed += 1
            freed += claimed

    return removed, freed


async def collect(session: AsyncSession, root: Path) -> Usage:
    """Return what the instance holds, as of now.

    Computed on demand rather than kept up to date by the write path. A number
    that is minutes old is fine for the question it answers, and a counter
    maintained on every upload is a thing that can drift from the disk it
    claims to describe — which is exactly the failure this is meant to catch.
    """
    sizes = scan_store(root)
    digests = await _digests_by_library(session)

    items = await _counts(session, Item.library_id)
    trashed = await _counts(session, Item.library_id, Item.deleted.is_(True))
    attachments = await _counts(session, Item.library_id, Item.item_type == "attachment")
    collections = await _counts(session, Collection.library_id)
    tags = await _counts(session, Tag.library_id)

    libraries = list(await session.scalars(select(Library).order_by(Library.id)))

    usage: list[LibraryUsage] = []
    referenced: set[str] = set()
    nominal = 0
    missing_total: set[str] = set()

    for library in libraries:
        held = digests.get(library.id, set())
        present = {digest for digest in held if digest in sizes}
        absent = held - present
        referenced |= present
        missing_total |= absent
        cost = sum(sizes[digest] for digest in present)
        nominal += cost

        usage.append(
            LibraryUsage(
                id=library.id,
                type=library.type,
                owner_id=library.owner_id,
                name=library.name,
                version=library.version,
                items=items.get(library.id, 0),
                trashed=trashed.get(library.id, 0),
                collections=collections.get(library.id, 0),
                tags=tags.get(library.id, 0),
                attachments=attachments.get(library.id, 0),
                files=len(present),
                bytes=cost,
                missing=len(absent),
            )
        )

    orphans = set(sizes) - referenced
    return Usage(
        libraries=usage,
        users=(await session.scalar(select(func.count()).select_from(User))) or 0,
        groups=sum(1 for library in libraries if library.type is LibraryType.GROUP),
        nominal_bytes=nominal,
        real_bytes=sum(sizes[digest] for digest in referenced),
        orphan_bytes=sum(sizes[digest] for digest in orphans),
        orphan_files=len(orphans),
        missing_files=len(missing_total),
        stored_files=len(sizes),
        stored_bytes=sum(sizes.values()),
    )
