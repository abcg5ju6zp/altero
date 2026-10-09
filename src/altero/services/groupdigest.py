"""Turning settled group activity into one notification and one message.

This is what makes the feature a digest rather than a stream. A client syncing
a library uploads in batches of fifty, so a member who asked to hear about new
items would be told ten times about one sync. Activity is therefore left where
:mod:`altero.services.groupactivity` put it until the library has been quiet
for a while, and then everything waiting is rendered together.

The fan-out to recipients happens here rather than on the write path. A group
with fifty members costs one row when somebody writes and fifty notifications
once, later, off anybody's request.

Delivery is durable per recipient, not one mark-then-fan-out stretch. When a
burst settles the sweep *establishes* a :class:`~altero.models.GroupDigest` in
the same transaction that claims the activity rows: that stable identity owns
the rows, one :class:`~altero.models.GroupDigestRecipient` per member (holding
the subscription snapshot and rendered text), the in-app notification each
gets, and every email attempt. Each recipient then progresses independently:

* the in-app notification is completed only once it has been committed, and a
  conditional update plus a unique constraint mean a second sweep cannot make
  a second one;
* a refused or interrupted email leaves the recipient *retryable* without ever
  undoing the notification; a later sweep -- after a restart, or simply the
  next tick -- sends it, once, and does nothing to finished recipients.

The three failures a sweep can meet therefore have three recoveries:

* **another sweep won the batch** -- the claim matches no waiting rows, the
  half-made digest is rolled back, and the library is skipped;
* **a database commit fails** -- the transaction rolls back, so a batch whose
  establishment failed is still fully claimable and a notification whose
  commit failed is still recorded as not done; the next sweep retries from the
  last durable state, with nothing half-written;
* **email is refused, raises, or the process vanishes mid-send** -- the
  notification stays (it committed first) and the email stays pending; it is
  attempted again on a later sweep.

Nothing here raises. A digest is *about* something that has already happened,
so one bad relay or database blip must neither redeliver to the people already
done nor take down whatever else the sweep was doing.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from altero.models import (
    ActivityKind,
    DigestEmailState,
    GroupActivity,
    GroupDigest,
    GroupDigestRecipient,
    Library,
    Notification,
    User,
)
from altero.services import groupprefs
from altero.services.mail import Message

logger = logging.getLogger("altero.groupdigest")

#: How long a library must be quiet before what happened in it is sent.
DEFAULT_QUIET_PERIOD = timedelta(minutes=15)

#: How long an in-progress email attempt owns the address. Longer than one
#: SMTP attempt can take (:data:`altero.services.mail.TIMEOUT_SECONDS`), so a
#: sweep tick running beside a long send does not send too; short enough that
#: an attempt whose process died is picked up again promptly.
EMAIL_ATTEMPT_LEASE = timedelta(seconds=30)

#: Sends one message and says whether a relay took it.
Notify = Callable[[Message], Awaitable[bool]]

#: What each kind is called in a digest. One line per kind, in this order, so
#: two digests about the same group read the same way.
_LINES: dict[ActivityKind, tuple[str, str]] = {
    ActivityKind.ITEMS_CHANGED: (
        "{count} item was added or changed",
        "{count} items were added or changed",
    ),
    ActivityKind.ITEMS_DELETED: ("{count} item was deleted", "{count} items were deleted"),
    ActivityKind.MEMBERS_CHANGED: ("{count} membership changed", "{count} memberships changed"),
    ActivityKind.COLLECTIONS_CHANGED: (
        "{count} collection was added or changed",
        "{count} collections were added or changed",
    ),
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _describe(counts: dict[ActivityKind, int]) -> list[str]:
    """Return one line per kind that happened, in a fixed order."""
    lines = []
    for kind, (singular, plural) in _LINES.items():
        count = counts.get(kind, 0)
        if count:
            lines.append((singular if count == 1 else plural).format(count=count))
    return lines


async def _settled_libraries(session: AsyncSession, quiet_period: timedelta) -> list[int]:
    """Return the libraries whose waiting activity has stopped arriving.

    Judged by the *newest* waiting row rather than the oldest: a sync still
    running is one event that has not finished happening, and reporting its
    first half would mean reporting the second half separately.
    """
    cutoff = _now() - quiet_period
    result = await session.execute(
        select(GroupActivity.library_id)
        .where(GroupActivity.flushed.is_(None))
        .group_by(GroupActivity.library_id)
        .having(func.max(GroupActivity.created) <= cutoff)
    )
    return [library_id for (library_id,) in result.all()]


def _counts_for(
    rows: Sequence[GroupActivity], kinds: frozenset[ActivityKind], recipient_id: int
) -> dict[ActivityKind, int]:
    """Total each kind the recipient asked for and did not do themselves.

    Excluded per row rather than per digest: when two people have been working
    in a group, each should hear what the other did, and neither should be told
    about their own afternoon.
    """
    counts: dict[ActivityKind, int] = {}
    for row in rows:
        if row.actor_id == recipient_id:
            continue
        kind = ActivityKind(row.kind)
        if kind in kinds:
            counts[kind] = counts.get(kind, 0) + row.count
    return counts


async def _snapshot_recipients(
    session: AsyncSession,
    digest: GroupDigest,
    library: Library,
    rows: Sequence[GroupActivity],
) -> None:
    """Create one recipient row per member this burst is going to.

    Everything written here is a snapshot of establishment time. The member
    list and preferences are read now -- never again -- so leaving or changing
    a preference after this point cannot alter a batch already in flight; it
    affects the next one. The rendered text is stored too, so a retry renders
    identically and needs neither the (possibly renamed) group nor the
    activity rows (which retention may age out).
    """
    # Resolved per kind and merged, so one member subscribed to two kinds is
    # written to once.
    candidates: dict[int, User] = {}
    for kind in ActivityKind:
        for user in await groupprefs.subscribers(session, library, kind):
            candidates[user.id] = user

    for user in sorted(candidates.values(), key=lambda candidate: candidate.id):
        kinds = await groupprefs.subscribed_kinds(session, library, user_id=user.id)
        counts = _counts_for(rows, kinds, user.id)
        if not counts:
            # Either nothing of a kind they wanted, or all of it was their own
            # work. Telling somebody what they just did is the fastest way to
            # teach them to ignore the notification.
            continue

        subject = f"New activity in “{library.name}”"
        body = "\n".join(_describe(counts))
        session.add(
            GroupDigestRecipient(
                digest_id=digest.id,
                user_id=user.id,
                email=user.email,
                subject=subject,
                body=body,
                email_body=(
                    f"Since you were last told, in the group library “{library.name}”:\n\n"
                    f"{body}\n\n"
                    "You are receiving this because you asked to be notified about "
                    "this group. Change that in the group's settings."
                ),
                notify_items_changed=ActivityKind.ITEMS_CHANGED in kinds,
                notify_items_deleted=ActivityKind.ITEMS_DELETED in kinds,
                notify_members_changed=ActivityKind.MEMBERS_CHANGED in kinds,
                notify_collections_changed=ActivityKind.COLLECTIONS_CHANGED in kinds,
                email_state=(
                    DigestEmailState.NONE.value
                    if not user.email
                    else DigestEmailState.PENDING.value
                ),
            )
        )


async def _establish(session: AsyncSession, library_id: int) -> GroupDigest | None:
    """Claim a settled library's waiting rows and build its digest.

    Returns the established digest, or ``None`` when the claim matched
    nothing -- which means another sweep won this library and there is nothing
    for this one to do. The claim is the stamp, set together with the new
    digest id in one ``UPDATE``: two sweeps racing cannot both match
    ``flushed IS NULL``.

    Everything here is one transaction. If it fails to commit, the rows are
    still unflushed and the next sweep establishes the batch again; nothing is
    left half-made.
    """
    digest = GroupDigest(library_id=library_id)
    session.add(digest)
    try:
        await session.flush()

        claimed = await session.execute(
            update(GroupActivity)
            .where(GroupActivity.library_id == library_id, GroupActivity.flushed.is_(None))
            .values(flushed=_now(), digest_id=digest.id)
            .returning(GroupActivity.id)
        )
        ids = [row_id for (row_id,) in claimed.all()]
        if not ids:
            # Another sweep took the rows between the settle query and here.
            # The digest created above goes back with the rollback.
            await session.rollback()
            return None

        rows = list(await session.scalars(select(GroupActivity).where(GroupActivity.id.in_(ids))))
        library = await session.get(Library, library_id)
        if library is not None:  # pragma: no branch - deletion races the claim
            await _snapshot_recipients(session, digest, library, rows)
        await session.commit()
    except SQLAlchemyError:
        # Commit or statement failure: nothing is durable, so the batch is
        # still waiting exactly as it was. The next sweep tries again.
        logger.exception("Could not establish the digest for library %s", library_id)
        await session.rollback()
        return None

    return digest


async def _raise_notification(
    session: AsyncSession, recipient: GroupDigestRecipient
) -> tuple[bool, bool]:
    """Make one recipient's in-app notification durable.

    Returns ``(told, keep_going)``: whether a new notification was created on
    this call, and whether the sweep should continue with this digest.

    The insert and the claim are one transaction. The conditional update only
    matches while ``notification_id`` is null, so two sweeps that reach the
    same recipient cannot both commit one; the loser rolls its notification
    back. A database failure rolls back too: the recipient stays undone and is
    retried rather than recorded twice.
    """
    notification = Notification(
        user_id=recipient.user_id,
        kind="group_activity",
        subject=recipient.subject,
        body=recipient.body,
    )
    session.add(notification)
    try:
        await session.flush()
        claimed = await session.execute(
            update(GroupDigestRecipient)
            .where(
                GroupDigestRecipient.id == recipient.id,
                GroupDigestRecipient.notification_id.is_(None),
            )
            .values(notification_id=notification.id)
        )
        if (getattr(claimed, "rowcount", 0) or 0) == 0:
            # Another sweep persisted it first. Nothing to add here; let that
            # sweep finish the digest rather than working beside it.
            await session.rollback()
            return False, False
        await session.commit()
    except SQLAlchemyError:
        logger.exception("Could not persist the digest notification for recipient %s", recipient.id)
        await session.rollback()
        return False, False

    return True, True


async def _send_email(
    session: AsyncSession, recipient: GroupDigestRecipient, notify: Notify
) -> bool:
    """Attempt one recipient's pending email. Returns whether to keep going.

    The attempt is claimed before the send, in its own transaction, and only
    while no fresher attempt exists -- the lease that stops two overlapping
    sweeps both sending. The send's outcome then gets exactly one of two
    durable results: acceptance advances the row to ``sent``; a refusal, an
    exception or a process that vanishes leaves it ``pending`` with the
    attempt recorded, and the notification already persisted is never touched.
    A later sweep retries.
    """
    now = _now()
    claimed = await session.execute(
        update(GroupDigestRecipient)
        .where(
            GroupDigestRecipient.id == recipient.id,
            GroupDigestRecipient.email_state == DigestEmailState.PENDING.value,
            or_(
                GroupDigestRecipient.last_attempt_at.is_(None),
                GroupDigestRecipient.last_attempt_at <= now - EMAIL_ATTEMPT_LEASE,
            ),
        )
        .values(
            last_attempt_at=now,
            email_attempts=GroupDigestRecipient.email_attempts + 1,
        )
    )
    if (getattr(claimed, "rowcount", 0) or 0) == 0:
        # A fresher attempt -- another sweep's -- owns the address.
        return False
    try:
        await session.commit()
    except SQLAlchemyError:
        logger.exception("Could not claim the email attempt for recipient %s", recipient.id)
        await session.rollback()
        return False

    try:
        message = Message(
            to=recipient.email or "",
            subject=recipient.subject,
            body=recipient.email_body,
        )
        accepted = await notify(message)
    except Exception:
        # The message is about something that already happened. A relay that
        # raises is treated like one that refuses: logged, left pending.
        logger.exception("Could not send the group digest to %s", recipient.email)
        return True

    if not accepted:
        logger.warning(
            "The relay refused the group digest for %s; it will be retried", recipient.email
        )
        return True

    try:
        await session.execute(
            update(GroupDigestRecipient)
            .where(
                GroupDigestRecipient.id == recipient.id,
                GroupDigestRecipient.email_state == DigestEmailState.PENDING.value,
            )
            .values(email_state=DigestEmailState.SENT.value)
        )
        await session.commit()
    except SQLAlchemyError:
        # The send's outcome is now unknown: the relay may have the message.
        # Leaving it pending risks a duplicate; marking it sent risks losing
        # it. Losing a digest is the failure this design exists to prevent, so
        # the row stays retryable.
        logger.exception("Could not record the accepted email for recipient %s", recipient.id)
        await session.rollback()
        return False

    return True


async def _deliver(session: AsyncSession, digest_id: int, notify: Notify) -> int:
    """Work through one digest's unfinished recipients; return how many told.

    Safe to call against a digest another sweep is delivering and against one
    left half-finished by a process that exited: recipients already persisted
    are skipped and never duplicated.
    """
    told = 0
    lease_cutoff = _now() - EMAIL_ATTEMPT_LEASE
    recipients = list(
        await session.scalars(
            select(GroupDigestRecipient)
            .where(
                GroupDigestRecipient.digest_id == digest_id,
                or_(
                    GroupDigestRecipient.notification_id.is_(None),
                    and_(
                        GroupDigestRecipient.email_state == DigestEmailState.PENDING.value,
                        or_(
                            GroupDigestRecipient.last_attempt_at.is_(None),
                            GroupDigestRecipient.last_attempt_at <= lease_cutoff,
                        ),
                    ),
                ),
            )
            .order_by(GroupDigestRecipient.id)
        )
    )

    for recipient in recipients:
        if recipient.notification_id is None:
            made, keep_going = await _raise_notification(session, recipient)
            told += int(made)
            if not keep_going:
                return told

        if (
            recipient.email_state == DigestEmailState.PENDING.value
            and recipient.email
            and not await _send_email(session, recipient, notify)
        ):
            return told

    await _close_if_finished(session, digest_id)
    return told


async def _close_if_finished(session: AsyncSession, digest_id: int) -> None:
    """Mark the digest complete once no recipient has work left.

    A pending email -- even one waiting out the attempt lease -- keeps it open,
    which is what a restart looks for.
    """
    outstanding = await session.scalar(
        select(func.count())
        .select_from(GroupDigestRecipient)
        .where(
            GroupDigestRecipient.digest_id == digest_id,
            or_(
                GroupDigestRecipient.notification_id.is_(None),
                GroupDigestRecipient.email_state == DigestEmailState.PENDING.value,
            ),
        )
    )
    if outstanding:
        return

    try:
        await session.execute(
            update(GroupDigest)
            .where(GroupDigest.id == digest_id, GroupDigest.completed_at.is_(None))
            .values(completed_at=_now())
        )
        await session.commit()
    except SQLAlchemyError:
        # Harmless if lost: the digest is still provably complete and a later
        # sweep repeats this no-op.
        await session.rollback()


async def _continue_open_digests(session: AsyncSession, notify: Notify) -> int:
    """Finish digests an earlier sweep or process did not.

    Runs before new batches are established, so a restart resumes even when no
    fresh activity exists.
    """
    digest_ids = list(
        await session.scalars(
            select(GroupDigest.id)
            .where(GroupDigest.completed_at.is_(None))
            .order_by(GroupDigest.id)
        )
    )

    told = 0
    for digest_id in digest_ids:
        told += await _deliver(session, digest_id, notify)
    return told


async def sweep(
    session: AsyncSession,
    notify: Notify,
    *,
    quiet_period: timedelta = DEFAULT_QUIET_PERIOD,
) -> int:
    """Deliver every digest that is due, and return how many were told.

    Safe to call at any time and from anywhere: open digests are resumed
    first, a library with nothing settled is skipped, and the claim means two
    sweeps running at once cannot both establish or deliver the same digest.
    """
    total = await _continue_open_digests(session, notify)

    for library_id in await _settled_libraries(session, quiet_period):
        digest = await _establish(session, library_id)
        if digest is None:
            # Lost the race to another sweep, or establishment failed and
            # rolled back; the library stays claimable for a later tick.
            continue

        total += await _deliver(session, digest.id, notify)

    return total
