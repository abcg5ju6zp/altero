"""Durable delivery progress for one settled burst of group activity.

A digest is established once, when the sweep claims the waiting activity rows,
and keeps an identity after that. Everything to do with the burst hangs off
that identity: the activity rows (``group_activity.digest_id``), one
:class:`GroupDigestRecipient` per person the burst is going to, the in-app
notification each receives, and every attempt to hand that person the email.

The point of keeping the progress is that delivery stops being one
all-or-nothing stretch between two marks. The old order -- mark everything
flushed, then fan out -- meant a process that died halfway, or one mailbox that
refused, quietly ended delivery for everybody left in the batch. Each
recipient row now says exactly where that person's delivery got to, so a later
sweep continues from it and a finished recipient is never told twice.

Preferences are copied onto the recipient row when the batch is established.
A member changing their mind afterwards affects the *next* burst, not one that
is already in flight.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from altero.db import Base


class DigestEmailState(StrEnum):
    """Where one recipient's email copy has got to.

    Stored as the string for the same reason :class:`~altero.models.activity.
    ActivityKind` is: read straight off the row by the sweep and by anybody
    inspecting a stuck queue.
    """

    #: The member had no address when the batch was established. Nothing to
    #: send, ever, for this digest -- an address added later is for next time.
    NONE = "none"
    #: An address exists and the relay has not accepted the message yet. This
    #: is the retryable state: refusal, an error and a process killed mid-send
    #: all leave the row here, and the in-app notification is untouched.
    PENDING = "pending"
    #: The relay took the message. Terminal, like ``none``.
    SENT = "sent"


class GroupDigest(Base):
    """One settled batch of activity for one library, with its delivery state.

    Created in the same transaction that stamps the activity rows, so a batch
    and its identity cannot come apart. Rows are kept after completion rather
    than deleted: together with the recipient rows they are the record of who
    was told what, and of any mail a relay never took.
    """

    __tablename__ = "group_digests"
    __table_args__ = (
        # The sweep's resume query: everything not yet finished.
        Index("ix_group_digests_open", "completed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id"), index=True)
    created: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    #: Set once every recipient has both a persisted notification and a
    #: terminal email state. Null is what a restarting sweep looks for.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class GroupDigestRecipient(Base):
    """One member's place in one digest.

    The rendered text and the four preference flags are snapshots taken at
    establishment, so retries render identically and later edits -- group
    renamed, preference flipped, even the activity row aged out by retention
    -- cannot change what this digest says.

    The two channels progress independently. ``notification_id`` is the
    in-app channel: set once, and the uniqueness below is the hard guarantee
    that a second sweep cannot make a second notification. The email channel
    is :attr:`email_state`: it advances to ``sent`` only when a relay accepts,
    and any other outcome stays ``pending`` without rolling the notification
    back.
    """

    __tablename__ = "group_digest_recipients"
    __table_args__ = (
        UniqueConstraint("digest_id", "user_id", name="uq_group_digest_recipient"),
        UniqueConstraint("notification_id", name="uq_group_digest_recipient_notice"),
        Index("ix_group_digest_recipients_digest", "digest_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    digest_id: Mapped[int] = mapped_column(
        ForeignKey("group_digests.id", ondelete="CASCADE")
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    #: The address as it was when the batch was established. Null then means
    #: ``email_state`` starts at ``none``.
    email: Mapped[str | None] = mapped_column(String(320), default=None)
    subject: Mapped[str] = mapped_column(String(255))
    #: What the in-app notification shows.
    body: Mapped[str] = mapped_column(String, default="")
    #: The full email, including the wrapper the notification does not carry.
    #: Stored rendered so a retry needs neither the group name nor the
    #: activity rows, which retention may already have removed.
    email_body: Mapped[str] = mapped_column(String, default="")

    # --- Subscription preference snapshot, one flag per activity kind. ---
    notify_items_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_items_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_members_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_collections_changed: Mapped[bool] = mapped_column(Boolean, default=False)

    #: The notification once it has been made durable. Its presence -- not an
    #: in-memory decision, not an email answer -- is what completes the
    #: recipient's in-app delivery.
    notification_id: Mapped[int | None] = mapped_column(
        ForeignKey("notifications.id", ondelete="SET NULL"), default=None
    )
    email_state: Mapped[str] = mapped_column(
        String(16), default=DigestEmailState.PENDING.value
    )
    #: How often a relay has been tried. Kept so a permanently refusing
    #: address is visible as one rather than as silence.
    email_attempts: Mapped[int] = mapped_column(Integer, default=0)
    #: When the current or last attempt started. Doubles as the attempt lease:
    #: another sweep does not touch a pending row whose attempt is fresh, so a
    #: long SMTP call and a sweep tick beside it cannot send twice.
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
