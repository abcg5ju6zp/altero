"""What a library costs on disk.

The number zotero.org cannot report and a self-hosted instance can. Files are
stored once per digest, so a file attached in two libraries is on disk once and
in both libraries' accounts: what each library would cost on its own is not
what the instance is paying, and an operator asked to plan for disk needs both.
"""

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from altero.keys import generate_api_key
from altero.models import (
    Item,
    ItemField,
    Library,
    LibraryType,
    StorageDeleteLease,
    StorageDownload,
    StorageUpload,
)
from altero.services import itemwrites, storage, storagestats, writes
from altero.services.auth import get_library
from altero.services.storage import LEASE_CANDIDATE, LEASE_DELETED
from tests.factories import make_group, make_item, make_user


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A file store of this test's own, where the settings fixture puts one."""
    return tmp_path / "storage"


def put_file(root: Path, digest: str, body: bytes) -> None:
    """Write bytes into the store the way an upload would."""
    path = storage.file_path(root, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


async def personal_library(session: AsyncSession) -> Library:
    """Return the library `make_user` gives an account."""
    await make_user(session)
    return await get_library(session, LibraryType.USER, 1)


def age(root: Path, digest: str, *, hours: int) -> None:
    """Backdate a stored file, as if it had been sitting there."""
    path = storage.file_path(root, digest)
    when = path.stat().st_mtime - hours * 3600
    os.utime(path, (when, when))


DIGEST = "0" * 32
OTHER = "1" * 32


class TestWhatALibraryHolds:
    async def test_it_counts_items_collections_and_tags(
        self, session: AsyncSession, store: Path
    ) -> None:
        library = await personal_library(session)
        await make_item(session, library)
        await make_item(session, library, deleted=True)

        report = await storagestats.collect(session, store)

        (entry,) = report.libraries
        assert entry.items == 2
        assert entry.trashed == 1

    async def test_it_reports_the_library_version(self, session: AsyncSession, store: Path) -> None:
        """The other half of what motivation.md asks an operator view for."""
        library = await personal_library(session)
        library.version = 17
        await session.commit()

        report = await storagestats.collect(session, store)

        assert report.libraries[0].version == 17


class TestNominalAgainstReal:
    async def test_a_shared_file_is_counted_once_on_disk_and_twice_in_libraries(
        self, session: AsyncSession, store: Path
    ) -> None:
        personal = await personal_library(session)
        group = await make_group(session, group_id=100, owner_id=1)
        put_file(store, DIGEST, b"x" * 1000)
        for library in (personal, group):
            await make_item(session, library, item_type="attachment", fields={"md5": DIGEST})

        report = await storagestats.collect(session, store)

        assert report.nominal_bytes == 2000
        assert report.real_bytes == 1000
        assert report.saved_bytes == 1000

    async def test_one_library_pays_the_whole_of_its_own_file(
        self, session: AsyncSession, store: Path
    ) -> None:
        library = await personal_library(session)
        put_file(store, DIGEST, b"x" * 1000)
        await make_item(session, library, item_type="attachment", fields={"md5": DIGEST})

        report = await storagestats.collect(session, store)

        assert report.libraries[0].bytes == 1000

    async def test_the_same_file_attached_twice_in_one_library_is_counted_once(
        self, session: AsyncSession, store: Path
    ) -> None:
        """A library is charged for bytes, not for attachments."""
        library = await personal_library(session)
        put_file(store, DIGEST, b"x" * 1000)
        for _ in range(2):
            await make_item(session, library, item_type="attachment", fields={"md5": DIGEST})

        report = await storagestats.collect(session, store)

        assert report.libraries[0].bytes == 1000
        assert report.libraries[0].files == 1


class TestWhatDoesNotAddUp:
    async def test_a_file_nothing_references_is_an_orphan(
        self, session: AsyncSession, store: Path
    ) -> None:
        await personal_library(session)
        put_file(store, OTHER, b"y" * 500)

        report = await storagestats.collect(session, store)

        assert report.orphan_files == 1
        assert report.orphan_bytes == 500

    async def test_an_attachment_with_no_bytes_is_reported_as_missing(
        self, session: AsyncSession, store: Path
    ) -> None:
        """A restore that lost its files looks exactly like this."""
        library = await personal_library(session)
        await make_item(session, library, item_type="attachment", fields={"md5": DIGEST})

        report = await storagestats.collect(session, store)

        assert report.missing_files == 1
        assert report.libraries[0].missing == 1

    async def test_a_store_that_was_never_written_to_is_not_an_error(
        self, session: AsyncSession, store: Path
    ) -> None:
        """An instance where nobody has uploaded anything has no directory."""
        await personal_library(session)

        report = await storagestats.collect(session, store / "never-used")

        assert report.real_bytes == 0
        assert report.orphan_files == 0


class TestTheLibrariesItNames:
    async def test_a_library_is_named_by_type_and_owner(
        self, session: AsyncSession, store: Path
    ) -> None:
        """The pair that addresses it in the API, so an operator can act on it."""
        library = await personal_library(session)
        library.name = "Ada's library"
        await session.commit()

        report = await storagestats.collect(session, store)

        assert report.libraries[0].type is LibraryType.USER
        assert report.libraries[0].owner_id == 1
        assert report.libraries[0].name == "Ada's library"


class TestPurgingOrphans:
    """Deleting bytes is the riskiest thing in the operator layer.

    Never on a timer, and never without a grace period: a file reaches the disk
    before the item row that refers to it is committed, so an upload in flight
    looks exactly like an orphan for as long as that request takes.
    """

    async def test_an_old_orphan_goes_when_it_is_asked_for(
        self, session: AsyncSession, store: Path
    ) -> None:
        await personal_library(session)
        put_file(store, OTHER, b"y" * 500)
        age(store, OTHER, hours=48)

        files, freed = await storagestats.purge_orphans(session, store, grace=timedelta(hours=24))

        assert (files, freed) == (1, 500)
        assert not storage.file_path(store, OTHER).exists()

    async def test_a_file_that_has_just_arrived_is_left_alone(
        self, session: AsyncSession, store: Path
    ) -> None:
        """This is the upload in flight, and it is the whole reason for a grace."""
        await personal_library(session)
        put_file(store, OTHER, b"y" * 500)

        files, freed = await storagestats.purge_orphans(session, store, grace=timedelta(hours=24))

        assert (files, freed) == (0, 0)
        assert storage.file_path(store, OTHER).exists()

    async def test_a_referenced_file_is_never_touched(
        self, session: AsyncSession, store: Path
    ) -> None:
        library = await personal_library(session)
        put_file(store, DIGEST, b"x" * 1000)
        age(store, DIGEST, hours=4000)
        await make_item(session, library, item_type="attachment", fields={"md5": DIGEST})

        files, _ = await storagestats.purge_orphans(session, store, grace=timedelta(hours=24))

        assert files == 0
        assert storage.file_path(store, DIGEST).exists()

    async def test_a_file_in_another_library_is_not_an_orphan(
        self, session: AsyncSession, store: Path
    ) -> None:
        """The digest is shared; only the union of every library counts."""
        personal = await personal_library(session)
        group = await make_group(session, group_id=100, owner_id=1)
        put_file(store, DIGEST, b"x" * 1000)
        age(store, DIGEST, hours=4000)
        await make_item(session, group, item_type="attachment", fields={"md5": DIGEST})
        assert personal is not None

        files, _ = await storagestats.purge_orphans(session, store, grace=timedelta(hours=24))

        assert files == 0


def stage(root: Path, digest: str, body: bytes) -> Path:
    """Leave a half-written file where `store_file` would have put one."""
    directory = storage.file_path(root, digest).parent
    directory.mkdir(parents=True, exist_ok=True)
    staged = directory / f"{storage.STAGING_PREFIX}whatever"
    staged.write_bytes(body)
    return staged


class TestFilesLeftHalfWritten:
    """A write into the store that never finished.

    `store_file` writes under a name of its own and moves the file onto its
    digest, so one of these is a process that died between the two. Nothing
    else in here would ever see it: `scan_store` passes over anything not
    named like a digest, which is what keeps an operator's own files out of a
    list of things to delete.
    """

    def test_one_is_not_a_stored_file(self, store: Path) -> None:
        put_file(store, DIGEST, b"x" * 100)
        stage(store, DIGEST, b"half of something")

        assert storagestats.scan_store(store) == {DIGEST: 100}

    async def test_an_abandoned_one_is_swept(self, session: AsyncSession, store: Path) -> None:
        await personal_library(session)
        staged = stage(store, DIGEST, b"y" * 500)
        os.utime(staged, (0, 0))

        files, freed = await storagestats.purge_orphans(session, store, grace=timedelta(hours=24))

        assert (files, freed) == (1, 500)
        assert not staged.exists()

    async def test_one_still_being_written_is_left_alone(
        self, session: AsyncSession, store: Path
    ) -> None:
        """The same grace an upload in flight gets, and for the same reason."""
        await personal_library(session)
        staged = stage(store, DIGEST, b"y" * 500)

        files, freed = await storagestats.purge_orphans(session, store, grace=timedelta(hours=24))

        assert (files, freed) == (0, 0)
        assert staged.exists()


def digest_of(body: bytes) -> str:
    """The name a body is stored under."""
    return hashlib.md5(body, usedforsecurity=False).hexdigest()


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def seed_lease(
    session: AsyncSession,
    digest: str,
    *,
    root: Path | None = None,
    size: int = 500,
    mtime_ns: int = 1,
    available_at: datetime | None = None,
    status: str = LEASE_CANDIDATE,
    deleted_at: datetime | None = None,
) -> StorageDeleteLease:
    """Write a lease row directly, the way an acquisition or decision did.

    With ``root`` the identity is snapshotted from the file on disk; without
    it the caller supplies the snapshot, which is what the crash-convergence
    tests need -- a lease for bytes that are no longer there.
    """
    if root is not None and storage.file_path(root, digest).is_file():
        described = storage.file_path(root, digest).stat()
        size, mtime_ns = described.st_size, described.st_mtime_ns
    lease = StorageDeleteLease(
        digest=digest,
        status=status,
        size=size,
        mtime_ns=mtime_ns,
        available_at=available_at or (_naive_now() - timedelta(hours=1)),
        deleted_at=deleted_at,
    )
    session.add(lease)
    await session.commit()
    return lease


def authorization_for(digest: str, size: int) -> dict[str, object]:
    """What the first upload request declares for these bytes."""
    return storage.parse_authorization(
        {
            "md5": digest,
            "filename": "f.txt",
            "filesize": str(size),
            "mtime": "1700000000000",
            "contentType": "text/plain",
        }
    )


async def md5_fields(session: AsyncSession, item_id: int) -> list[ItemField]:
    return list(
        await session.scalars(
            select(ItemField).where(ItemField.item_id == item_id, ItemField.field == "md5")
        )
    )


GRACE = timedelta(hours=24)


class TestTheLeaseAnOrphanIsDeletedUnder:
    """The decide half of the two-phase purge, driven through a seeded lease."""

    async def test_a_decision_leaves_a_deleted_lease_record(
        self, session: AsyncSession, store: Path
    ) -> None:
        """The terminal row is the reviewable evidence the old purge lacked."""
        await personal_library(session)
        put_file(store, OTHER, b"y" * 500)
        age(store, OTHER, hours=48)
        await seed_lease(session, OTHER, root=store)

        files, freed = await storagestats.purge_orphans(session, store, grace=GRACE)

        assert (files, freed) == (1, 500)
        lease = await session.get(StorageDeleteLease, OTHER)
        assert lease is not None
        assert lease.status == LEASE_DELETED
        assert lease.deleted_at is not None

    async def test_a_lease_that_is_not_due_yet_is_left_alone(
        self, session: AsyncSession, store: Path
    ) -> None:
        """available_at is the gate, independently of the file's own age."""
        await personal_library(session)
        put_file(store, OTHER, b"y" * 500)
        age(store, OTHER, hours=48)
        await seed_lease(session, OTHER, root=store, available_at=_naive_now() + timedelta(hours=1))

        files, freed = await storagestats.purge_orphans(session, store, grace=GRACE)

        assert (files, freed) == (0, 0)
        assert storage.file_path(store, OTHER).exists()
        lease = await session.get(StorageDeleteLease, OTHER)
        assert lease is not None
        assert lease.status == LEASE_CANDIDATE

        lease.available_at = _naive_now() - timedelta(minutes=1)
        await session.commit()
        files, _ = await storagestats.purge_orphans(session, store, grace=GRACE)
        assert files == 1

    async def test_an_upload_in_flight_keeps_the_candidate(
        self, session: AsyncSession, store: Path
    ) -> None:
        """The three-request upload owns the digest until it registers."""
        library = await personal_library(session)
        item = await make_item(session, library, item_type="attachment")
        body = b"z" * 300
        digest = digest_of(body)
        put_file(store, digest, body)
        age(store, digest, hours=48)
        await seed_lease(session, digest, root=store)
        session.add(
            StorageUpload(
                key=generate_api_key(),
                item_id=item.id,
                library_id=library.id,
                md5=digest,
                filename="f.txt",
                filesize=300,
                mtime=1700000000000,
            )
        )
        await session.commit()

        files, freed = await storagestats.purge_orphans(session, store, grace=GRACE)

        assert (files, freed) == (0, 0)
        assert storage.file_path(store, digest).exists()
        lease = await session.get(StorageDeleteLease, digest)
        assert lease is not None
        assert lease.status == LEASE_CANDIDATE
        assert lease.attempts == 0

    async def test_a_reference_after_the_upload_row_keeps_the_candidate(
        self, session: AsyncSession, store: Path
    ) -> None:
        """Registration deletes the upload row and writes the item field."""
        library = await personal_library(session)
        item = await make_item(session, library, item_type="attachment")
        body = b"z" * 300
        digest = digest_of(body)
        put_file(store, digest, body)
        age(store, digest, hours=48)
        await seed_lease(session, digest, root=store)
        upload = StorageUpload(
            key=generate_api_key(),
            item_id=item.id,
            library_id=library.id,
            md5=digest,
            filename="f.txt",
            filesize=300,
            mtime=1700000000000,
            received=True,
        )
        session.add(upload)
        await session.commit()

        # First pass: retained on the upload row.
        assert (await storagestats.purge_orphans(session, store, grace=GRACE)) == (0, 0)

        # Registration commits: the row goes, the reference appears.
        await session.delete(upload)
        await make_item(session, library, item_type="attachment", fields={"md5": digest})
        assert (await storagestats.purge_orphans(session, store, grace=GRACE)) == (0, 0)
        assert storage.file_path(store, digest).exists()

    async def test_an_unexpired_download_permission_keeps_the_candidate(
        self, session: AsyncSession, store: Path
    ) -> None:
        """The 302 hands out ownership that lasts until it expires."""
        library = await personal_library(session)
        item = await make_item(session, library, item_type="attachment")
        put_file(store, OTHER, b"y" * 500)
        age(store, OTHER, hours=48)
        await seed_lease(session, OTHER, root=store)
        session.add(
            StorageDownload(
                key=generate_api_key(),
                item_id=item.id,
                library_id=library.id,
                md5=OTHER,
                expires=_naive_now() + timedelta(minutes=5),
            )
        )
        await session.commit()

        assert (await storagestats.purge_orphans(session, store, grace=GRACE)) == (0, 0)
        assert storage.file_path(store, OTHER).exists()

        permission = await session.scalar(select(StorageDownload))
        assert permission is not None
        permission.expires = _naive_now() - timedelta(minutes=1)
        await session.commit()
        files, freed = await storagestats.purge_orphans(session, store, grace=GRACE)
        assert (files, freed) == (1, 500)

    async def test_a_trashed_reference_keeps_the_candidate(
        self, session: AsyncSession, store: Path
    ) -> None:
        """Bytes an item in the trash still claims are not orphans."""
        library = await personal_library(session)
        put_file(store, DIGEST, b"x" * 1000)
        age(store, DIGEST, hours=48)
        await seed_lease(session, DIGEST, root=store)
        await make_item(
            session, library, item_type="attachment", deleted=True, fields={"md5": DIGEST}
        )

        files, _ = await storagestats.purge_orphans(session, store, grace=GRACE)

        assert files == 0
        assert storage.file_path(store, DIGEST).exists()

    async def test_a_file_whose_identity_changed_releases_the_lease(
        self, session: AsyncSession, store: Path
    ) -> None:
        """A new incarnation at the same digest name earns a fresh grace."""
        await personal_library(session)
        put_file(store, OTHER, b"y" * 500)
        age(store, OTHER, hours=48)
        await seed_lease(session, OTHER, root=store)

        # A fresh atomic replace: same content-addressed name, new size/mtime.
        put_file(store, OTHER, b"y" * 501)

        files, freed = await storagestats.purge_orphans(session, store, grace=GRACE)

        assert (files, freed) == (0, 0)
        assert storage.file_path(store, OTHER).exists()
        assert await session.get(StorageDeleteLease, OTHER) is None

        # The new file is young, so the very next pass acquires nothing.
        assert (await storagestats.purge_orphans(session, store, grace=GRACE)) == (0, 0)

        # Old again and still unreferenced: a fresh candidate deletes it.
        age(store, OTHER, hours=48)
        files, freed = await storagestats.purge_orphans(session, store, grace=GRACE)
        assert (files, freed) == (1, 501)


class TestUnlinkThatFails:
    async def test_it_is_recorded_as_retryable_work(
        self, session: AsyncSession, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await personal_library(session)
        put_file(store, OTHER, b"y" * 500)
        age(store, OTHER, hours=48)
        await seed_lease(session, OTHER, root=store)

        def refuse(self: Path) -> None:  # pragma: no cover - exercised by the call below
            raise PermissionError("mount read-only")

        monkeypatch.setattr(Path, "unlink", refuse)

        files, freed = await storagestats.purge_orphans(session, store, grace=GRACE)

        assert (files, freed) == (0, 0)
        assert storage.file_path(store, OTHER).exists()
        lease = await session.get(StorageDeleteLease, OTHER)
        assert lease is not None
        assert lease.attempts == 1
        assert "PermissionError" in (lease.last_error or "")
        assert lease.retry_after is not None
        assert lease.retry_after > _naive_now()

        # Still inside the backoff: another press does not even try.
        files, _ = await storagestats.purge_orphans(session, store, grace=GRACE)
        assert files == 0
        retried = await session.get(StorageDeleteLease, OTHER)
        assert retried is not None
        assert retried.attempts == 1

        monkeypatch.undo()
        lease.retry_after = _naive_now() - timedelta(minutes=1)
        await session.commit()
        files, freed = await storagestats.purge_orphans(session, store, grace=GRACE)
        assert (files, freed) == (1, 500)


class TestCrashesAndRetries:
    async def test_bytes_gone_before_commit_converges_without_freed_bytes(
        self, session: AsyncSession, store: Path
    ) -> None:
        """unlink done, commit lost: the lease meets a missing file."""
        await personal_library(session)
        # The lease says the file exists; the disk disagrees.
        await seed_lease(session, OTHER, root=None, size=500, mtime_ns=1)

        files, freed = await storagestats.purge_orphans(session, store, grace=GRACE)

        assert (files, freed) == (0, 0)
        lease = await session.get(StorageDeleteLease, OTHER)
        assert lease is not None
        assert lease.status == LEASE_DELETED

        # The administrator presses again after the lost response: no bytes are
        # claimed a second time, and there is nothing left to decide.
        assert (await storagestats.purge_orphans(session, store, grace=GRACE)) == (0, 0)

    async def test_a_crashed_purge_never_blocks_a_new_upload(
        self, session: AsyncSession, store: Path
    ) -> None:
        """A stale candidate for missing bytes is reaped by authorize."""
        library = await personal_library(session)
        item = await make_item(session, library, item_type="attachment")
        body = b"q" * 400
        digest = digest_of(body)
        await seed_lease(session, digest, root=None, size=400, mtime_ns=1)

        declared = authorization_for(digest, len(body))
        result = await storage.authorize(session, library, item, declared, store)
        await session.commit()

        assert "uploadKey" in result
        assert await session.get(StorageDeleteLease, digest) is None

        # The ordinary three-request upload then completes registration.
        upload = await storage.get_upload(session, result["uploadKey"])
        storage.store_bytes(store, upload, body)
        upload.received = True
        await session.commit()
        version = await writes.bump_library_version(session, library)
        await storage.register(session, item, upload, version, store)
        await session.commit()

        assert storage.file_path(store, digest).is_file()
        assert await md5_fields(session, item.id)

    async def test_terminal_leases_are_pruned_after_one_window(
        self, session: AsyncSession, store: Path
    ) -> None:
        await personal_library(session)
        # Old enough to forget; the bytes are not on disk any more.
        await seed_lease(
            session,
            OTHER,
            root=None,
            size=500,
            status=LEASE_DELETED,
            deleted_at=_naive_now() - timedelta(hours=25),
            available_at=_naive_now() - timedelta(hours=49),
        )
        # Recent enough to keep for review.
        await seed_lease(
            session,
            DIGEST,
            root=None,
            size=1000,
            status=LEASE_DELETED,
            deleted_at=_naive_now(),
            available_at=_naive_now() - timedelta(hours=25),
        )

        await storagestats.purge_orphans(session, store, grace=GRACE)

        assert await session.get(StorageDeleteLease, OTHER) is None
        assert await session.get(StorageDeleteLease, DIGEST) is not None

    async def test_a_returned_file_under_a_terminal_lease_is_leased_again(
        self, session: AsyncSession, store: Path
    ) -> None:
        """A restore bringing bytes back must not let them escape forever."""
        await personal_library(session)
        put_file(store, OTHER, b"y" * 500)
        age(store, OTHER, hours=48)
        await seed_lease(
            session,
            OTHER,
            root=None,
            size=999,
            status=LEASE_DELETED,
            deleted_at=_naive_now(),
            available_at=_naive_now() - timedelta(hours=25),
        )

        files, freed = await storagestats.purge_orphans(session, store, grace=GRACE)

        assert (files, freed) == (1, 500)
        assert not storage.file_path(store, OTHER).exists()


class TestUploadsArbitrateWithTheLease:
    """The race this was built for, in its deterministic service-level form."""

    async def test_authorize_revokes_a_candidate_and_attaches(
        self, session: AsyncSession, store: Path
    ) -> None:
        """Reference re-registered after the sweep's reference read survives."""
        library = await personal_library(session)
        item = await make_item(session, library, item_type="attachment")
        body = b"w" * 250
        digest = digest_of(body)
        put_file(store, digest, body)
        age(store, digest, hours=48)
        await seed_lease(session, digest, root=store)

        result = await storage.authorize(
            session, library, item, authorization_for(digest, len(body)), store
        )
        await session.commit()

        assert result == {"exists": 1}
        assert await session.get(StorageDeleteLease, digest) is None
        fields = await md5_fields(session, item.id)
        assert [field.value for field in fields] == [digest]

        # The sweep that runs now finds nothing to take.
        files, freed = await storagestats.purge_orphans(session, store, grace=GRACE)
        assert (files, freed) == (0, 0)
        assert storage.file_path(store, digest).exists()

    async def test_authorize_falls_back_to_bytes_when_the_sweep_won(
        self, session: AsyncSession, store: Path
    ) -> None:
        """Decision committed first: the file is gone, so a normal upload opens."""
        library = await personal_library(session)
        item = await make_item(session, library, item_type="attachment")
        body = b"v" * 333
        digest = digest_of(body)
        # What a finished decision leaves behind; the bytes are already gone.
        await seed_lease(
            session,
            digest,
            root=None,
            size=len(body),
            status=LEASE_DELETED,
            deleted_at=_naive_now(),
            available_at=_naive_now() - timedelta(hours=25),
        )

        result = await storage.authorize(
            session, library, item, authorization_for(digest, len(body)), store
        )
        assert "uploadKey" in result
        upload = await storage.get_upload(session, result["uploadKey"])

        storage.store_bytes(store, upload, body)
        upload.received = True
        await session.commit()
        version = await writes.bump_library_version(session, library)
        await storage.register(session, item, upload, version, store)
        await session.commit()

        assert storage.file_path(store, digest).is_file()
        assert await md5_fields(session, item.id)
        # The terminal row sits out its review window harmlessly; what matters
        # is that it neither blocked the upload nor came back as a candidate.
        terminal = await session.get(StorageDeleteLease, digest)
        assert terminal is not None
        assert terminal.status == LEASE_DELETED


class TestRealConcurrentArbitration:
    """Two database connections interleaved at the exact decision point.

    SQLite ignores FOR UPDATE and lets WAL readers and one writer overlap, so
    the guarantee cannot be shown with sequential calls: the purge must hold
    a *writer* lock before it rechecks. Events pin the interleave deterministically
    -- the attach reaches its DELETE (its own write-first statement) while the
    purge decision is holding the lock, then the purge is released -- without
    any sleep or timing assumption.
    """

    async def test_an_attach_during_the_decision_converges_without_losing_bytes(
        self,
        app: FastAPI,
        session: AsyncSession,
        store: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Session A: an old, unreferenced orphan already leased for deletion.
        library = await personal_library(session)
        item = await make_item(session, library, item_type="attachment")
        body = b"c" * 222
        digest = digest_of(body)
        put_file(store, digest, body)
        age(store, digest, hours=48)
        await seed_lease(session, digest, root=store)

        # Preload what the authorize side needs on a second connection and
        # end ITS read transaction before the interleave, so its first
        # statement during the race is the DELETE (a write), not a read that
        # could only fail to upgrade later.
        async with app.state.database.session_factory() as other:
            other_library = await get_library(other, LibraryType.USER, 1)
            other_item = await other.get(Item, item.id)
            await other.commit()

            decision_reached = asyncio.Event()
            release_decision = asyncio.Event()
            real_owner_check = storagestats._locked_owner_exists

            async def gated_owner_check(session_, model, candidate, moment):
                decision_reached.set()
                await release_decision.wait()
                return await real_owner_check(session_, model, candidate, moment)

            revoke_started = asyncio.Event()
            real_revoke = storage.revoke_delete_lease

            async def tracked_revoke(session_, candidate):
                revoke_started.set()
                return await real_revoke(session_, candidate)

            monkeypatch.setattr(storagestats, "_locked_owner_exists", gated_owner_check)
            monkeypatch.setattr(storage, "revoke_delete_lease", tracked_revoke)

            purge = asyncio.create_task(
                storagestats.purge_orphans(session, store, grace=GRACE)
            )
            await decision_reached.wait()

            async def attach_and_commit() -> dict[str, object]:
                # authorize flushes but leaves committing to its caller, as
                # the route does -- the commit is what has to be arbitrated.
                authorized = await storage.authorize(
                    other,
                    other_library,
                    other_item,
                    authorization_for(digest, len(body)),
                    store,
                )
                await other.commit()
                return authorized

            attach = asyncio.create_task(attach_and_commit())
            await revoke_started.wait()

            # Discriminator: with the write-claim the purge owns the database
            # writer lock, so this cannot return until the purge finishes. A
            # decision that only held a read lock would let the attach commit
            # straight through this gate -- which is exactly the race.
            try:
                result = await asyncio.wait_for(asyncio.shield(attach), timeout=1)
            except TimeoutError:
                release_decision.set()
                files, freed = await purge
                result = await attach
            else:  # pragma: no cover - only reached when the arbitration regresses
                pytest.fail("attach committed through a decision that held no write lock")

            # Purge committed first: it rightfully removed the still-unowned
            # orphan; the attach MUST NOT claim bytes that are gone.
            assert (files, freed) == (1, len(body))
            assert "uploadKey" in result
            assert not storage.file_path(store, digest).is_file()

            # The converged authorize opened a normal three-request upload;
            # completing it lands the bytes and the reference.
            upload = await storage.get_upload(other, str(result["uploadKey"]))
            storage.store_bytes(store, upload, body)
            upload.received = True
            await other.commit()
            version = await writes.bump_library_version(other, other_library)
            await storage.register(other, other_item, upload, version, store)
            await other.commit()

        assert storage.file_path(store, digest).is_file()
        # Re-read through A's connection and sweep again: the new reference
        # holds and nothing is deleted a second time.
        referenced = list(
            await session.scalars(
                select(ItemField).where(ItemField.item_id == item.id, ItemField.field == "md5")
            )
        )
        assert [field.value for field in referenced] == [digest]
        assert (await storagestats.purge_orphans(session, store, grace=GRACE)) == (0, 0)
        assert storage.file_path(store, digest).is_file()


class TestWritesOutsideTheFileProtocol:
    """Generic item writes (copy, sync replay, restore) name md5s too."""

    async def test_a_generic_item_write_revokes_a_candidate(
        self, session: AsyncSession, store: Path
    ) -> None:
        library = await personal_library(session)
        body = b"d" * 150
        digest = digest_of(body)
        put_file(store, digest, body)
        age(store, digest, hours=48)
        await seed_lease(session, digest, root=store)

        written = await itemwrites.save_item(
            session,
            library,
            {
                "itemType": "attachment",
                "linkMode": "imported_file",
                "title": "Restored",
                "filename": "d.txt",
                "md5": digest,
                "mtime": "1700000000000",
            },
            version=1,
        )
        await session.commit()

        assert written is not None
        assert await session.get(StorageDeleteLease, digest) is None
        assert (await storagestats.purge_orphans(session, store, grace=GRACE)) == (0, 0)
        assert storage.file_path(store, digest).exists()


class TestAcquisitionIsBestEffort:
    async def test_a_file_gone_by_lease_time_is_skipped(self, session: AsyncSession, store: Path) -> None:
        """scan said it was there; stat at acquisition says it is not."""
        await personal_library(session)

        await storagestats._acquire_candidates(
            session, store, {OTHER: 500}, _naive_now(), GRACE
        )

        assert await session.get(StorageDeleteLease, OTHER) is None

    async def test_a_duplicate_acquisition_from_another_purge_is_rolled_back(
        self, session: AsyncSession, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two purges racing to INSERT the same candidates must not 500."""
        await personal_library(session)
        put_file(store, OTHER, b"y" * 500)
        age(store, OTHER, hours=48)

        real_commit = session.commit
        forced = False

        async def fail_once() -> None:
            nonlocal forced
            if not forced:
                forced = True
                raise IntegrityError("insert", {}, Exception("UNIQUE constraint failed"))
            await real_commit()

        monkeypatch.setattr(session, "commit", fail_once)

        await storagestats._acquire_candidates(
            session, store, {OTHER: 500}, _naive_now(), GRACE
        )

        assert await session.get(StorageDeleteLease, OTHER) is None
        monkeypatch.undo()
        # The losing purge's caller can simply run on; the next pass leases.
        files, _ = await storagestats.purge_orphans(session, store, grace=GRACE)
        assert files == 1
