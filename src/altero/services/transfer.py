"""Moving a whole library out of one instance and into another.

The requirement that shapes all of this is exactness. A library is not a bag of
objects, it is a set of objects at particular versions, and clients remember
those versions: a restore that renumbers them looks like it worked and then
locks out every client that had synced against the original, in both directions
at once. So the archive carries versions -- the library's and every object's --
along with the timestamps the client supplied, the deletion log that lets a
client tell a removed object from one it never fetched, and the attachment bytes
without which every attachment points at nothing.

Accounts and credentials are deliberately not in it. An archive is a library,
not a user: it can be restored under a different account on a different server,
and it cannot leak an API key by being copied around.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from altero import __version__
from altero.errors import (
    AttachmentChangedError,
    AttachmentMissingError,
    ExportConflictError,
    ExportPublishError,
    ExportWriteError,
    InvalidInputError,
    NotFoundError,
)
from altero.models import (
    Collection,
    CollectionItem,
    CollectionRelation,
    CollectionShare,
    DeletedObject,
    FullText,
    Item,
    ItemCreator,
    ItemField,
    ItemRelation,
    ItemTag,
    Library,
    LibraryType,
    SavedSearch,
    SearchCondition,
    Setting,
    StorageDownload,
    StorageUpload,
    Tag,
)
from altero.services import storage

logger = logging.getLogger("altero.transfer")

#: Bumped when the archive layout changes in a way an older altero cannot read.
FORMAT_VERSION = 1

MANIFEST = "manifest.json"
FILE_PREFIX = "files/"

#: PostgreSQL SQLSTATEs that mean "the snapshot could not be maintained": a
#: serialization failure or a deadlock. Either one aborts the export so the
#: caller can start again, rather than the query returning a mixed answer.
_SERIALIZATION_SQLSTATES = {"40001", "40P01"}

#: An unpublished export is assembled in one of these directories, in the
#: destination's own directory: same filesystem, so publishing is one
#: ``os.replace``, and a restart can find and reclaim it. A confirmed archive
#: is a plain file with no such prefix and is never touched by reclamation.
WORKSPACE_PREFIX = ".altero-export-"
WORKSPACE_RECORD = "export.json"
ARCHIVE_PART = "archive.part"

#: The seven data documents, in archive write order.
DOCUMENT_NAMES = (
    "items.json",
    "collections.json",
    "searches.json",
    "tags.json",
    "settings.json",
    "fulltext.json",
    "deleted.json",
)

#: Block size when streaming an attachment into the archive.
COPY_CHUNK = 1 << 20

#: A workspace younger than this may belong to an export still running in this
#: or another process, so recovery leaves it alone. An older one means the
#: owning process died before the archive was published.
RECOVERY_GRACE = timedelta(hours=1)

#: An archived file is named by its digest, and that name is joined onto the
#: file store's root to decide where it lands. Anything that is not a digest is
#: refused: ``files/../../..`` in a hand-made archive would otherwise write
#: wherever the server can. It matters more now than it did when only an
#: administrator at a shell could restore one -- the browser can too.
_DIGEST = re.compile(r"[0-9a-fA-F]{32}")


def _moment(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def _resolve(session: AsyncSession, library_type: LibraryType, owner_id: int) -> Library:
    library = await session.scalar(
        select(Library).where(Library.type == library_type, Library.owner_id == owner_id)
    )
    if library is None:
        raise NotFoundError(f"No {library_type.value} library with id {owner_id}")
    return library


@dataclass(slots=True)
class Snapshot:
    """Everything about a library as of one export boundary."""

    library: Library
    items: list[Item]
    collections: list[Collection]
    searches: list[SavedSearch]
    tags: list[Tag]
    #: ``(collection key, item key)`` rows.
    membership: list[tuple[str, str]]
    #: ``(tag key, item key)`` rows.
    item_tags: list[tuple[str, str]]
    settings: list[Setting]
    fulltext: list[FullText]
    deletions: list[DeletedObject]


async def _begin_snapshot(session: AsyncSession) -> Any:
    """Establish the stable-snapshot transaction this export reads through.

    Must be called before the session's first statement.

    PostgreSQL asks for REPEATABLE READ before the transaction starts: every
    statement then sees the database as of the first, and a concurrent write
    commits harmlessly alongside it.

    SQLite needs scoped handling. Its driver's legacy transaction mode opens a
    transaction for DML only, so SELECTs share no transaction and have no
    stable snapshot. An explicit ``BEGIN`` -- sent as SQL while SQLAlchemy's
    logical transaction is still empty -- puts the connection in one real
    transaction, and the export then holds that WAL snapshot for its whole
    life. :func:`_end_snapshot` closes it with an explicit ``ROLLBACK``; the
    connection is then exactly where it started, so the write path -- which
    relies on the driver's deferred-begin behaviour -- is untouched.
    """
    bind = getattr(session, "bind", None)
    if bind is not None and bind.dialect.name == "postgresql":
        await session.connection(
            execution_options={"isolation_level": "REPEATABLE READ"}
        )
        return None

    # session.connection() autobegins SQLAlchemy's logical transaction; in
    # legacy mode this emits no SQL before the first statement, so the driver
    # is still outside a transaction and accepts our explicit BEGIN.
    connection = await session.connection()
    await connection.exec_driver_sql("BEGIN")
    return connection


async def _end_snapshot(session: AsyncSession, connection: Any) -> None:
    """Close the export's snapshot transaction on SQLite.

    Driver-level ROLLBACK rather than ``session.rollback()``: ending only the
    transaction does not expire every ORM object the session already holds,
    and SQLAlchemy's logical transaction stays in the same open state it has
    after any ordinary read. Nothing was written, so nothing else to undo.
    """
    if connection is None:
        # PostgreSQL: read-only, nothing was changed by the isolation setting.
        return
    await connection.exec_driver_sql("ROLLBACK")


def _snapshot_conflict(library: Library) -> ExportConflictError:
    message = (
        f"Library {library.type.value}/{library.owner_id} could not keep one "
        "snapshot while exporting. Start the export again."
    )
    return ExportConflictError(
        message, library_type=library.type.value, owner_id=library.owner_id
    )


def _is_serialization_failure(error: BaseException) -> bool:
    orig = getattr(error, "orig", None)
    return getattr(orig, "sqlstate", None) in _SERIALIZATION_SQLSTATES


async def _collect_snapshot(session: AsyncSession, library: Library) -> Snapshot:
    """Read every object and relationship, all from the one snapshot."""
    try:
        items = list(
            await session.scalars(
                select(Item).where(Item.library_id == library.id).order_by(Item.id)
            )
        )
        keys_by_id = {item.id: item.key for item in items}

        collections = list(
            await session.scalars(
                select(Collection)
                .where(Collection.library_id == library.id)
                .order_by(Collection.id)
            )
        )
        collection_keys = {collection.id: collection.key for collection in collections}

        searches = list(
            await session.scalars(
                select(SavedSearch)
                .where(SavedSearch.library_id == library.id)
                .order_by(SavedSearch.id)
            )
        )
        tags = list(
            await session.scalars(
                select(Tag).where(Tag.library_id == library.id).order_by(Tag.id)
            )
        )
        tag_keys = {tag.id: tag.key for tag in tags}

        membership = [
            (collection_keys[row.collection_id], keys_by_id[row.item_id])
            for row in await session.scalars(
                select(CollectionItem).where(
                    CollectionItem.collection_id.in_(collection_keys)
                )
            )
        ]
        item_tags = [
            (tag_keys[row.tag_id], keys_by_id[row.item_id])
            for row in await session.scalars(
                select(ItemTag).where(ItemTag.tag_id.in_(tag_keys))
            )
        ]

        settings = list(
            await session.scalars(
                select(Setting).where(Setting.library_id == library.id)
            )
        )
        fulltext = list(
            await session.scalars(
                select(FullText).where(FullText.library_id == library.id)
            )
        )
        deletions = list(
            await session.scalars(
                select(DeletedObject).where(DeletedObject.library_id == library.id)
            )
        )

        # The canary: within a stable snapshot the version is unchanged by
        # construction. A difference means the reads were not one snapshot, so
        # mixed content is refused rather than packaged.
        version = await session.scalar(
            select(Library.version).where(Library.id == library.id)
        )
        if version != library.version:
            raise _snapshot_conflict(library)
    except DBAPIError as error:
        if not _is_serialization_failure(error):
            raise
        raise _snapshot_conflict(library) from error

    return Snapshot(
        library=library,
        items=items,
        collections=collections,
        searches=searches,
        tags=tags,
        membership=membership,
        item_tags=item_tags,
        settings=settings,
        fulltext=fulltext,
        deletions=deletions,
    )


def _build_documents(snap: Snapshot) -> dict[str, list[dict[str, Any]]]:
    """Turn the snapshot rows into the archive's seven JSON documents."""
    keys_by_id = {item.id: item.key for item in snap.items}
    collection_keys = {c.id: c.key for c in snap.collections}

    return {
        "items.json": [
            {
                "key": item.key,
                "version": item.version,
                "itemType": item.item_type,
                "parent": keys_by_id.get(item.parent_id) if item.parent_id else None,
                "deleted": item.deleted,
                "inPublications": item.in_publications,
                "sortTitle": item.sort_title,
                "sortCreator": item.sort_creator,
                "sortDate": item.sort_date,
                "dateAdded": _moment(item.date_added),
                "dateModified": _moment(item.date_modified),
                "serverDateModified": _moment(item.server_date_modified),
                "fields": [{"field": f.field, "value": f.value} for f in item.fields],
                "creators": [
                    {
                        "position": c.position,
                        "creatorType": c.creator_type,
                        "firstName": c.first_name,
                        "lastName": c.last_name,
                        "name": c.name,
                    }
                    for c in item.creators
                ],
                "relations": [
                    {"predicate": r.predicate, "object": r.object}
                    for r in item.relations
                ],
            }
            for item in snap.items
        ],
        "collections.json": [
            {
                "key": collection.key,
                "version": collection.version,
                "name": collection.name,
                "parent": collection_keys.get(collection.parent_id)
                if collection.parent_id
                else None,
                "deleted": collection.deleted,
                "dateAdded": _moment(collection.date_added),
                "dateModified": _moment(collection.date_modified),
                "serverDateModified": _moment(collection.server_date_modified),
                "items": sorted(
                    item for name, item in snap.membership if name == collection.key
                ),
                "relations": [
                    {"predicate": r.predicate, "object": r.object}
                    for r in collection.relations
                ],
            }
            for collection in snap.collections
        ],
        "searches.json": [
            {
                "key": search.key,
                "version": search.version,
                "name": search.name,
                "deleted": search.deleted,
                "dateAdded": _moment(search.date_added),
                "dateModified": _moment(search.date_modified),
                "serverDateModified": _moment(search.server_date_modified),
                "conditions": [
                    {
                        "position": c.position,
                        "condition": c.condition,
                        "operator": c.operator,
                        "value": c.value,
                    }
                    for c in search.conditions
                ],
            }
            for search in snap.searches
        ],
        "tags.json": [
            {
                "key": tag.key,
                "name": tag.name,
                "type": tag.type,
                "version": tag.version,
                "dateAdded": _moment(tag.date_added),
                "dateModified": _moment(tag.date_modified),
                "serverDateModified": _moment(tag.server_date_modified),
                "items": sorted(
                    item for key, item in snap.item_tags if key == tag.key
                ),
            }
            for tag in snap.tags
        ],
        "settings.json": [
            {"name": setting.name, "value": setting.value, "version": setting.version}
            for setting in snap.settings
        ],
        "fulltext.json": [
            {
                "item": keys_by_id[text.item_id],
                "content": text.content,
                "version": text.version,
                "indexedChars": text.indexed_chars,
                "totalChars": text.total_chars,
                "indexedPages": text.indexed_pages,
                "totalPages": text.total_pages,
            }
            for text in snap.fulltext
        ],
        "deleted.json": [
            {
                "objectType": record.object_type,
                "key": record.key,
                "version": record.version,
                "deleted": _moment(record.deleted),
            }
            for record in snap.deletions
        ],
    }


def _referenced_digests(snap: Snapshot) -> list[str]:
    """All attachment digests the snapshot's items claim, sorted and de-duplicated."""
    return sorted(
        {
            value
            for item in snap.items
            for field, value in ((f.field, f.value) for f in item.fields)
            if field == "md5" and value
        }
    )


@dataclass(slots=True)
class ArchivedFile:
    """One attachment as it went into the archive and as its source was recorded."""

    digest: str
    source: Path
    #: Bytes streamed into the archive.
    size: int
    #: Source modification time captured at read time.
    mtime_ns: int
    #: SHA-256 of the archived bytes, recorded in the manifest.
    sha256: str


def _open_workspace(destination: Path) -> Path:
    """Create one unique, empty export workspace in the destination's directory.

    Beside the destination so the final rename cannot cross a device. The
    random suffix keeps concurrent exports of the same library from seeing
    each other's workspaces.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(100):
        workspace = destination.parent / f"{WORKSPACE_PREFIX}{token_hex(12)}"
        try:
            workspace.mkdir()
        except FileExistsError:
            continue
        return workspace
    # Practically impossible: would need 100 token collisions in one instant.
    raise ExportWriteError(
        f"Could not create an export workspace in {destination.parent}"
    )


def _write_record(workspace: Path, library: Library, state: str) -> None:
    """Write the sidecar that lets a restart tell what this export was."""
    record = {
        "startedAt": datetime.now(UTC).isoformat(),
        "library": {"type": library.type.value, "id": library.owner_id},
        "boundaryVersion": library.version,
        "state": state,
        "pid": os.getpid(),
    }
    (workspace / WORKSPACE_RECORD).write_text(
        json.dumps(record, indent=2), encoding="utf-8"
    )


def _discard_workspace(workspace: Path) -> None:
    """Remove an export workspace, whatever stage it reached."""
    shutil.rmtree(workspace, ignore_errors=True)


def _workspace_started_at(workspace: Path) -> float:
    """When the export that owns this workspace began, as epoch seconds.

    The sidecar's ``startedAt`` is the record; a missing or unreadable one
    falls back to the directory's own modification time.
    """
    record_path = workspace / WORKSPACE_RECORD
    if record_path.is_file():
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            return datetime.fromisoformat(record["startedAt"]).timestamp()
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return workspace.stat().st_mtime


def recover_workspaces(root: Path, *, grace: timedelta) -> int:
    """Reclaim unpublished export workspaces a stopped process left in ``root``.

    Only directories named like an export workspace are considered; a confirmed
    archive -- a plain file, including a published download left in the export
    directory -- is never touched. Workspaces younger than ``grace`` are
    spared, because an export in another process may still be writing them.
    Returns how many workspaces were removed.
    """
    if not root.is_dir():
        return 0

    cutoff = time.time() - grace.total_seconds()
    reclaimed = 0
    for workspace in root.glob(f"{WORKSPACE_PREFIX}*"):
        if not workspace.is_dir():
            continue
        try:
            started = _workspace_started_at(workspace)
        except OSError:
            # Cannot even stat it: treat as abandoned.
            started = 0.0
        if started > cutoff:
            continue
        try:
            shutil.rmtree(workspace)
        except OSError:
            logger.warning(
                "Could not remove abandoned export workspace %s", workspace
            )
            continue
        reclaimed += 1
    return reclaimed


def _sync_directory(directory: Path) -> None:
    """Force a directory entry to the device where the platform allows it."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _missing_attachment(library: Library, digest: str, when: str) -> AttachmentMissingError:
    return AttachmentMissingError(
        f"Attachment {digest} of library {library.type.value}/"
        f"{library.owner_id} disappeared {when}",
        digest=digest,
        library_type=library.type.value,
        owner_id=library.owner_id,
    )


def _stream_attachment(
    bundle: zipfile.ZipFile, *, library: Library, digest: str, source: Path
) -> ArchivedFile:
    """Copy one attachment into the archive, streaming and fingerprinting it."""
    try:
        described = source.stat()
    except FileNotFoundError as error:
        # Was present at the boundary (it was filtered in), gone before read.
        raise _missing_attachment(library, digest, "before it could be read") from error

    hasher = hashlib.sha256()
    size = 0
    try:
        with (
            source.open("rb") as handle,
            bundle.open(f"{FILE_PREFIX}{digest}", "w", force_zip64=True) as sink,
        ):
            while chunk := handle.read(COPY_CHUNK):
                hasher.update(chunk)
                size += len(chunk)
                sink.write(chunk)
    except FileNotFoundError as error:
        raise _missing_attachment(library, digest, "while being read") from error

    return ArchivedFile(
        digest=digest,
        source=source,
        size=size,
        mtime_ns=described.st_mtime_ns,
        sha256=hasher.hexdigest(),
    )


def _verify_sources(library: Library, files: list[ArchivedFile]) -> None:
    """Fail unless every boundary attachment is still the file that was archived.

    The file store is not transactional, so this is the attachment side of the
    export boundary: disappeared is one failure, replaced (different size or
    modification time) is another.
    """
    for archived in files:
        try:
            described = archived.source.stat()
        except FileNotFoundError as error:
            raise _missing_attachment(
                library, archived.digest, "between the boundary and the final check"
            ) from error
        if described.st_size != archived.size:
            raise AttachmentChangedError(
                f"Attachment {archived.digest} of library {library.type.value}/"
                f"{library.owner_id} was replaced during the export "
                "(size changed)",
                digest=archived.digest,
                library_type=library.type.value,
                owner_id=library.owner_id,
            )
        if described.st_mtime_ns != archived.mtime_ns:
            raise AttachmentChangedError(
                f"Attachment {archived.digest} of library {library.type.value}/"
                f"{library.owner_id} was replaced during the export "
                "(modification time changed)",
                digest=archived.digest,
                library_type=library.type.value,
                owner_id=library.owner_id,
            )


def _validate_archive(
    part: Path,
    *,
    manifest: dict[str, Any],
    documents: dict[str, list[dict[str, Any]]],
    files: list[ArchivedFile],
) -> None:
    """Check the closed archive before it is published."""
    try:
        with zipfile.ZipFile(part) as bundle:
            bad = bundle.testzip()
            names = set(bundle.namelist())
    except (zipfile.BadZipFile, OSError) as error:
        raise InvalidInputError(
            f"The assembled archive is unreadable: {error}"
        ) from error

    if bad is not None:
        raise InvalidInputError(
            f"The assembled archive fails its checksum at {bad}"
        )

    expected_members = {MANIFEST, *DOCUMENT_NAMES} | {
        f"{FILE_PREFIX}{archived.digest}" for archived in files
    }
    if names != expected_members:
        difference = sorted(names.symmetric_difference(expected_members))
        raise InvalidInputError(
            f"The assembled archive has the wrong members: {difference}"
        )

    counts = manifest.get("counts", {})
    expected_counts = {
        "items": len(documents["items.json"]),
        "collections": len(documents["collections.json"]),
        "searches": len(documents["searches.json"]),
        "tags": len(documents["tags.json"]),
        "settings": len(documents["settings.json"]),
        "fulltext": len(documents["fulltext.json"]),
        "deleted": len(documents["deleted.json"]),
        "files": len(files),
    }
    mismatched = {
        key: (counts.get(key), value)
        for key, value in expected_counts.items()
        if counts.get(key) != value
    }
    if mismatched:
        raise InvalidInputError(
            f"The manifest counts do not match the archive contents: {mismatched}"
        )


async def export_library(
    session: AsyncSession,
    *,
    library_type: LibraryType,
    owner_id: int,
    storage_root: Path,
    destination: Path,
) -> Path:
    """Write the whole of one library to ``destination`` and return the path."""
    snapshot_connection = await _begin_snapshot(session)
    try:
        library = await _resolve(session, library_type, owner_id)
        snap = await _collect_snapshot(session, library)

        documents = _build_documents(snap)

        # Attachments are stored once per digest, so the archive carries them the
        # same way: two items sharing a file do not carry it twice. A digest with
        # no backing file at the boundary is the ordinary state for a library read
        # out of WebDAV, so it is simply not in the archive rather than an error.
        digests = _referenced_digests(snap)
        present = [(digest, storage.file_path(storage_root, digest)) for digest in digests]
        present = [(digest, path) for digest, path in present if path.is_file()]

        manifest = {
            "format": FORMAT_VERSION,
            "altero": __version__,
            "library": {
                "type": library.type.value,
                "id": library.owner_id,
                "version": library.version,
            },
            "name": library.name,
            "exportedAt": datetime.now(UTC).isoformat(),
            "counts": {
                "items": len(snap.items),
                "collections": len(snap.collections),
                "searches": len(snap.searches),
                "tags": len(snap.tags),
                "settings": len(snap.settings),
                "fulltext": len(snap.fulltext),
                "deleted": len(snap.deletions),
                "files": len(present),
            },
            # Replaced below with the fingerprint records after the bytes stream.
            "files": [],
        }

        workspace = _open_workspace(destination)
        part = workspace / ARCHIVE_PART
        archived_files: list[ArchivedFile] = []
        try:
            _write_record(workspace, library, "writing")
            try:
                with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as bundle:
                    for name in DOCUMENT_NAMES:
                        bundle.writestr(name, json.dumps(documents[name], indent=2))
                    for digest, path in present:
                        archived_files.append(
                            _stream_attachment(
                                bundle, library=library, digest=digest, source=path
                            )
                        )
                    # The manifest is the last member: an interrupted write leaves
                    # an archive the manifest reader naturally rejects.
                    manifest["files"] = [
                        {
                            "digest": archived.digest,
                            "size": archived.size,
                            "sha256": archived.sha256,
                        }
                        for archived in archived_files
                    ]
                    bundle.writestr(MANIFEST, json.dumps(manifest, indent=2))
            except OSError as error:
                # Abort errors (AttachmentMissing) are AlteroErrors, not OSErrors;
                # a plain device/disk failure writing the part is reported as such.
                raise ExportWriteError(
                    f"Writing the archive for library {library.type.value}/"
                    f"{library.owner_id} failed: {error}",
                    cause=error,
                ) from error

            _write_record(workspace, library, "validating")
            _verify_sources(library, archived_files)
            _validate_archive(
                part, manifest=manifest, documents=documents, files=archived_files
            )

            try:
                os.replace(part, destination)
            except OSError as error:
                raise ExportPublishError(
                    f"Publishing the archive for library {library.type.value}/"
                    f"{library.owner_id} to {destination} failed: {error}",
                    cause=error,
                ) from error
            _sync_directory(destination.parent)
            logger.info(
                "Exported library %s/%d at version %d to %s",
                library.type.value,
                library.owner_id,
                library.version,
                destination,
            )
        finally:
            _discard_workspace(workspace)
    finally:
        await _end_snapshot(session, snapshot_connection)

    return destination


def read_manifest(archive: Path) -> dict[str, Any]:
    """Return what an archive says it holds, without restoring any of it.

    Separate from the restore so a caller can say what is about to happen --
    which library, how many items, how many files -- before anything is
    written. The checks live here too, so an archive this altero cannot read is
    refused by the reading rather than partway through the writing.
    """
    if not archive.is_file():
        raise NotFoundError(f"No archive at {archive}")

    try:
        with zipfile.ZipFile(archive) as bundle:
            manifest: dict[str, Any] = json.loads(bundle.read(MANIFEST))
            names = bundle.namelist()
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidInputError("That file is not an altero library archive") from error

    if manifest.get("format") != FORMAT_VERSION:
        raise InvalidInputError(
            f"Unsupported archive format {manifest.get('format')}; "
            f"this altero reads {FORMAT_VERSION}"
        )
    if not isinstance(manifest.get("library"), dict):
        raise InvalidInputError("That archive does not say which library it holds")

    for entry in names:
        if entry.startswith(FILE_PREFIX) and not _DIGEST.fullmatch(entry[len(FILE_PREFIX) :]):
            raise InvalidInputError(f"That archive holds a file with an impossible name: {entry}")

    return manifest


async def is_empty(session: AsyncSession, library: Library) -> bool:
    for model in (Item, Collection, SavedSearch, Tag, Setting, DeletedObject):
        if await session.scalar(select(model).where(model.library_id == library.id).limit(1)):
            return False
    return True


async def clear_library(session: AsyncSession, library: Library) -> None:
    """Remove everything belonging to a library, link tables first.

    Public because deleting a group needs exactly this, plus the rows that only
    a group has -- see :func:`altero.services.groups.delete_group`.
    """
    items = select(Item.id).where(Item.library_id == library.id)
    collections = select(Collection.id).where(Collection.library_id == library.id)

    await session.execute(delete(ItemTag).where(ItemTag.item_id.in_(items)))
    await session.execute(
        delete(CollectionItem).where(CollectionItem.collection_id.in_(collections))
    )
    # Before the collections themselves: a share names one by id, and a row left
    # pointing at a collection that no longer exists is a foreign key that fails
    # on the next backend that checks.
    await session.execute(delete(CollectionShare).where(CollectionShare.library_id == library.id))
    await session.execute(
        delete(CollectionRelation).where(CollectionRelation.collection_id.in_(collections))
    )
    await session.execute(delete(FullText).where(FullText.library_id == library.id))
    await session.execute(delete(ItemField).where(ItemField.item_id.in_(items)))
    await session.execute(delete(ItemCreator).where(ItemCreator.item_id.in_(items)))
    await session.execute(delete(ItemRelation).where(ItemRelation.item_id.in_(items)))
    # Before the items themselves, for the same reason as the shares above: an
    # upload that was authorized and never finished, and a download that was
    # permitted and never fetched, both name an item by id.
    await session.execute(delete(StorageUpload).where(StorageUpload.library_id == library.id))
    await session.execute(delete(StorageDownload).where(StorageDownload.library_id == library.id))
    # Children before parents, or the self-reference blocks the delete.
    await session.execute(
        delete(Item).where(Item.library_id == library.id, Item.parent_id.isnot(None))
    )
    await session.execute(delete(Item).where(Item.library_id == library.id))
    await session.execute(
        delete(SearchCondition).where(
            SearchCondition.search_id.in_(
                select(SavedSearch.id).where(SavedSearch.library_id == library.id)
            )
        )
    )
    await session.execute(delete(SavedSearch).where(SavedSearch.library_id == library.id))
    await session.execute(
        delete(Collection).where(
            Collection.library_id == library.id, Collection.parent_id.isnot(None)
        )
    )
    await session.execute(delete(Collection).where(Collection.library_id == library.id))
    await session.execute(delete(Tag).where(Tag.library_id == library.id))
    await session.execute(delete(Setting).where(Setting.library_id == library.id))
    await session.execute(delete(DeletedObject).where(DeletedObject.library_id == library.id))
    await session.flush()


def _check_file_records(manifest: dict[str, Any], names: list[str]) -> None:
    """If a manifest lists its files, require the record to match the archive.

    Archives produced with the file record name every file they carry; a
    mismatch means the archive was tampered with or damaged after the
    manifest was written, so the restore refuses before any row lands. An
    archive without the record predates it and is trusted the old way.
    """
    declared = manifest.get("files")
    if declared is None:
        return
    if not isinstance(declared, list) or not all(
        isinstance(entry, dict) for entry in declared
    ):
        raise InvalidInputError("The manifest's file records are malformed")

    declared_digests = sorted(str(entry.get("digest", "")) for entry in declared)
    actual = sorted(
        name[len(FILE_PREFIX) :]
        for name in names
        if name.startswith(FILE_PREFIX)
    )
    if declared_digests != actual:
        raise InvalidInputError(
            "The manifest's file records do not match the files in the archive"
        )


async def import_library(
    session: AsyncSession,
    *,
    archive: Path,
    storage_root: Path,
    replace: bool = False,
    into: Library | None = None,
) -> Library:
    """Restore an archive into the library it names, and return it.

    ``into`` overrides that: the archive is restored into the library the
    caller chose, whatever the manifest says. Keys and versions are per library,
    so nothing in the contents depends on which one it lands in -- and the
    browser needs it, because there the target is decided by who is signed in
    rather than by the file they uploaded. Without it, an archive naming
    somebody else's library would be restored over *that* library.

    The version counter comes from the archive either way. Restoring over a
    library that has since gone further therefore moves it backwards, and a
    client that synced past that point will not notice what changed underneath
    it; see "After recreating the database" in docs/administration.md.
    """
    manifest = read_manifest(archive)

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        documents: dict[str, Any] = {
            name: json.loads(bundle.read(name))
            for name in (
                "items.json",
                "collections.json",
                "searches.json",
                "tags.json",
                "settings.json",
                "fulltext.json",
                "deleted.json",
            )
        }
        # A new archive names its files; check that record before writing.
        # An older archive has no such record and keeps the old behaviour.
        _check_file_records(manifest, names)

        described = manifest["library"]
        library = (
            into
            if into is not None
            else await _resolve(session, LibraryType(described["type"]), described["id"])
        )

        if not await is_empty(session, library):
            if not replace:
                raise InvalidInputError(
                    f"Library {library.type.value}/{library.owner_id} is not empty; "
                    "restoring would merge two libraries rather than replace one"
                )
            await clear_library(session, library)

        # Items first and parents second: a child cannot name a row that does
        # not exist yet, and the archive stores parents by key.
        item_ids: dict[str, int] = {}
        for record in documents["items.json"]:
            item = Item(
                library_id=library.id,
                key=record["key"],
                version=record["version"],
                item_type=record["itemType"],
                deleted=record["deleted"],
                # Older archives predate the flag, so absence means false.
                in_publications=record.get("inPublications", False),
                sort_title=record["sortTitle"],
                sort_creator=record["sortCreator"],
                sort_date=record["sortDate"],
                date_added=_parse(record["dateAdded"]),
                date_modified=_parse(record["dateModified"]),
                server_date_modified=_parse(record["serverDateModified"]),
            )
            item.fields = [ItemField(field=f["field"], value=f["value"]) for f in record["fields"]]
            item.creators = [
                ItemCreator(
                    position=c["position"],
                    creator_type=c["creatorType"],
                    first_name=c["firstName"],
                    last_name=c["lastName"],
                    name=c["name"],
                )
                for c in record["creators"]
            ]
            item.relations = [
                ItemRelation(predicate=r["predicate"], object=r["object"])
                for r in record["relations"]
            ]
            session.add(item)
            await session.flush()
            item_ids[item.key] = item.id

        for record in documents["items.json"]:
            if record["parent"]:
                child = await session.get(Item, item_ids[record["key"]])
                if child is not None:
                    child.parent_id = item_ids[record["parent"]]

        collection_ids: dict[str, int] = {}
        for record in documents["collections.json"]:
            collection = Collection(
                library_id=library.id,
                key=record["key"],
                version=record["version"],
                name=record["name"],
                deleted=record["deleted"],
                date_added=_parse(record["dateAdded"]),
                date_modified=_parse(record["dateModified"]),
                server_date_modified=_parse(record["serverDateModified"]),
            )
            # Older archives predate collection relations.
            collection.relations = [
                CollectionRelation(predicate=r["predicate"], object=r["object"])
                for r in record.get("relations", [])
            ]
            session.add(collection)
            await session.flush()
            collection_ids[collection.key] = collection.id

        for record in documents["collections.json"]:
            stored = await session.get(Collection, collection_ids[record["key"]])
            if stored is None:  # pragma: no cover - just inserted
                continue
            if record["parent"]:
                stored.parent_id = collection_ids[record["parent"]]
            for item_key in record["items"]:
                session.add(CollectionItem(collection_id=stored.id, item_id=item_ids[item_key]))

        for record in documents["searches.json"]:
            search = SavedSearch(
                library_id=library.id,
                key=record["key"],
                version=record["version"],
                name=record["name"],
                deleted=record["deleted"],
                date_added=_parse(record["dateAdded"]),
                date_modified=_parse(record["dateModified"]),
                server_date_modified=_parse(record["serverDateModified"]),
            )
            search.conditions = [
                SearchCondition(
                    position=c["position"],
                    condition=c["condition"],
                    operator=c["operator"],
                    value=c["value"],
                )
                for c in record["conditions"]
            ]
            session.add(search)

        for record in documents["tags.json"]:
            tag = Tag(
                library_id=library.id,
                key=record["key"],
                name=record["name"],
                type=record["type"],
                version=record["version"],
                date_added=_parse(record["dateAdded"]),
                date_modified=_parse(record["dateModified"]),
                server_date_modified=_parse(record["serverDateModified"]),
            )
            session.add(tag)
            await session.flush()
            for item_key in record["items"]:
                session.add(ItemTag(tag_id=tag.id, item_id=item_ids[item_key]))

        for record in documents["settings.json"]:
            session.add(
                Setting(
                    library_id=library.id,
                    name=record["name"],
                    value=record["value"],
                    version=record["version"],
                )
            )

        for record in documents["fulltext.json"]:
            session.add(
                FullText(
                    library_id=library.id,
                    item_id=item_ids[record["item"]],
                    content=record["content"],
                    version=record["version"],
                    indexed_chars=record["indexedChars"],
                    total_chars=record["totalChars"],
                    indexed_pages=record["indexedPages"],
                    total_pages=record["totalPages"],
                )
            )

        for record in documents["deleted.json"]:
            session.add(
                DeletedObject(
                    library_id=library.id,
                    object_type=record["objectType"],
                    key=record["key"],
                    version=record["version"],
                    deleted=_parse(record["deleted"]),
                )
            )

        # Last, and the point of the exercise: the version clients remember.
        library.version = described["version"]
        # The name belongs to the library rather than to what is in it. Taking
        # the archive's is right when a library is restored over itself, since
        # that is its own name coming back, and wrong when the caller chose the
        # target: a group's name also lives on its `Group` row, which no archive
        # carries, so adopting one would leave the two disagreeing.
        if into is None:
            library.name = manifest.get("name", library.name)

        for entry in bundle.namelist():
            if not entry.startswith(FILE_PREFIX):
                continue
            # A digest, and nothing that could climb out of the store: checked
            # by read_manifest before a single row was written.
            digest = entry[len(FILE_PREFIX) :]
            # Whole or not at all, as an upload is. The bytes are not checked
            # against the name: an archive carries a snapshot under the digest
            # of the file inside it, and a library read out of zotero.org
            # keeps files whose digest disagrees with what the item claims, so
            # a mismatch here is the ordinary case rather than damage.
            storage.store_file(storage_root, digest, bundle.read(entry))

    await session.commit()
    return library
