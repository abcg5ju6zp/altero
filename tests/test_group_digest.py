"""Turning settled activity into one notification and one message.

The sweep is what makes this a digest rather than a stream. A client syncing a
library uploads in batches, so a member who asked to hear about new items would
otherwise be told ten times about one sync. Activity is therefore left alone
until the library has been quiet for a while, and then everything waiting is
rendered together.

Two rules decide who hears about it: the member asked for that kind, and the
member is not the person who did it. Both are checked here rather than at the
point of writing, so a group's size costs nothing on the sync path.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from altero.models import (
    ActivityKind,
    GroupActivity,
    GroupDigest,
    GroupDigestRecipient,
    Library,
    Notification,
)
from altero.services import groupactivity, groupdigest, groupprefs
from altero.services.mail import Message
from tests.factories import add_group_member, make_group, make_user

#: Long enough that nothing is due unless a test says so.
QUIET = timedelta(minutes=15)


class Outbox:
    """A mailer that keeps what it was given."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> bool:
        self.sent.append(message)
        return True


@pytest.fixture
def outbox() -> Outbox:
    return Outbox()


@pytest.fixture
async def group(session: AsyncSession) -> Library:
    """A group whose owner writes and whose member subscribes to everything."""
    await make_user(session, user_id=1, username="alice", display_name="Alice")
    await make_user(session, user_id=2, username="bob", display_name="Bob", email="bob@example.org")
    library = await make_group(session, group_id=100, owner_id=1, members={2: "member"})
    for kind in ActivityKind:
        await groupprefs.set_kind(session, library, user_id=2, kind=kind, wanted=True)
    await session.commit()
    return library


async def age(session: AsyncSession, minutes: int) -> None:
    """Backdate every waiting row, so the sweep considers it settled."""
    when = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes)
    await session.execute(update(GroupActivity).values(created=when))
    await session.commit()


async def notices(session: AsyncSession, user_id: int) -> list[Notification]:
    result = await session.scalars(
        select(Notification).where(Notification.user_id == user_id).order_by(Notification.id)
    )
    return list(result)


async def record(
    session: AsyncSession,
    library: Library,
    *,
    kind: ActivityKind = ActivityKind.ITEMS_CHANGED,
    count: int = 1,
    actor_id: int | None = 1,
) -> None:
    await groupactivity.record(session, library, actor_id=actor_id, kind=kind, count=count)
    await session.commit()


class TestWhatIsDue:
    async def test_activity_that_has_not_settled_is_left_alone(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        # Somebody is mid-sync. Sending now would mean sending again in a
        # minute, which is the thing the quiet period exists to prevent.
        await record(session, group)

        sent = await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert sent == 0
        assert outbox.sent == []
        assert await notices(session, 2) == []

    async def test_activity_that_has_settled_is_delivered(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        await record(session, group)
        await age(session, minutes=30)

        sent = await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert sent == 1
        assert len(outbox.sent) == 1
        assert len(await notices(session, 2)) == 1

    async def test_a_fresh_write_holds_back_the_whole_burst(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        # The burst is judged by its newest row, not its oldest: a sync still
        # running is one event that has not finished happening.
        await record(session, group)
        await age(session, minutes=30)
        await record(session, group)

        assert await groupdigest.sweep(session, outbox.send, quiet_period=QUIET) == 0
        assert outbox.sent == []


class TestWhoHearsAboutIt:
    async def test_the_person_who_did_it_is_not_told(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        await groupprefs.set_kind(
            session, group, user_id=1, kind=ActivityKind.ITEMS_CHANGED, wanted=True
        )
        await session.commit()
        await record(session, group, actor_id=1)
        await age(session, minutes=30)

        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert await notices(session, 1) == []
        assert len(await notices(session, 2)) == 1

    async def test_a_member_who_asked_for_nothing_hears_nothing(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        for kind in ActivityKind:
            await groupprefs.set_kind(session, group, user_id=2, kind=kind, wanted=False)
        await session.commit()
        await record(session, group)
        await age(session, minutes=30)

        assert await groupdigest.sweep(session, outbox.send, quiet_period=QUIET) == 0
        assert await notices(session, 2) == []

    async def test_only_the_subscribed_kinds_are_reported(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        await groupprefs.set_kind(
            session, group, user_id=2, kind=ActivityKind.ITEMS_DELETED, wanted=False
        )
        await session.commit()
        await record(session, group, kind=ActivityKind.ITEMS_CHANGED, count=2)
        await record(session, group, kind=ActivityKind.ITEMS_DELETED, count=5)
        await age(session, minutes=30)

        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        body = outbox.sent[0].body
        assert "2" in body
        assert "5" not in body

    async def test_each_person_hears_about_the_others_and_not_themselves(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        # Alice adds two, Bob adds three, both subscribe. Alice should be told
        # about three and Bob about two -- excluding the actor is per row, not
        # a decision about the whole burst.
        await groupprefs.set_kind(
            session, group, user_id=1, kind=ActivityKind.ITEMS_CHANGED, wanted=True
        )
        await session.commit()
        await record(session, group, actor_id=1, count=2)
        await record(session, group, actor_id=2, count=3)
        await age(session, minutes=30)

        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        (for_alice,) = await notices(session, 1)
        (for_bob,) = await notices(session, 2)
        assert "3" in for_alice.body
        assert "2" not in for_alice.body
        assert "2" in for_bob.body
        assert "3" not in for_bob.body

    async def test_a_write_by_nobody_is_reported_to_everyone(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        await record(session, group, actor_id=None)
        await age(session, minutes=30)

        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert len(await notices(session, 2)) == 1


class TestWhatItSays:
    async def test_one_burst_becomes_one_message(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        # Ten batches of a sync are one thing that happened.
        for _ in range(10):
            await record(session, group, count=50)
        await age(session, minutes=30)

        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert len(outbox.sent) == 1
        assert "500" in outbox.sent[0].body

    async def test_the_group_is_named(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        await record(session, group)
        await age(session, minutes=30)

        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert group.name in outbox.sent[0].subject

    async def test_the_notification_repeats_what_was_sent(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        # Mail is not enough on its own: no relay may be configured, and an
        # address may be unconfirmed. The panel is the other channel.
        await record(session, group, count=3)
        await age(session, minutes=30)

        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        (notice,) = await notices(session, 2)
        assert notice.kind == "group_activity"
        assert group.name in notice.subject
        assert "3" in notice.body

    async def test_a_member_without_an_address_still_gets_the_notification(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        await make_user(session, user_id=3, username="carol")

        await add_group_member(session, library_id=group.id, user_id=3)
        await groupprefs.set_kind(
            session, group, user_id=3, kind=ActivityKind.ITEMS_CHANGED, wanted=True
        )
        await session.commit()
        await record(session, group)
        await age(session, minutes=30)

        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert len(await notices(session, 3)) == 1
        assert [message.to for message in outbox.sent] == ["bob@example.org"]


class TestNotSendingTwice:
    async def test_delivered_activity_is_not_sent_again(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        await record(session, group)
        await age(session, minutes=30)

        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)
        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert len(outbox.sent) == 1
        assert len(await notices(session, 2)) == 1

    async def test_delivered_rows_are_kept_and_stamped(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        # Kept rather than deleted: what accumulates is the record of who
        # changed what and when, which an activity log would be built from.
        await record(session, group)
        await age(session, minutes=30)

        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        rows = list(await session.scalars(select(GroupActivity)))
        assert len(rows) == 1
        assert rows[0].flushed is not None

    async def test_activity_after_a_sweep_is_delivered_separately(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        await record(session, group)
        await age(session, minutes=30)
        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        await record(session, group)
        await age(session, minutes=30)
        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert len(outbox.sent) == 2


class TestFailures:
    async def test_a_relay_that_refuses_still_leaves_the_notification(
        self, session: AsyncSession, group: Library
    ) -> None:
        # Nothing in the mail path raises, and a dead relay must not mean the
        # activity is delivered again on the next sweep.
        async def refuse(message: Message) -> bool:
            return False

        await record(session, group)
        await age(session, minutes=30)

        await groupdigest.sweep(session, refuse, quiet_period=QUIET)

        assert len(await notices(session, 2)) == 1
        rows = list(await session.scalars(select(GroupActivity)))
        assert rows[0].flushed is not None

    async def test_a_mailer_that_raises_does_not_lose_the_sweep(
        self, session: AsyncSession, group: Library
    ) -> None:
        async def explode(message: Message) -> bool:
            raise RuntimeError("relay on fire")

        await record(session, group)
        await age(session, minutes=30)

        # The digest is about something that already happened, so a failure to
        # announce it must not take anything else down with it.
        await groupdigest.sweep(session, explode, quiet_period=QUIET)

        assert len(await notices(session, 2)) == 1


#: Stand-in for the process being killed between recipients. A BaseException,
#: like SIGTERM, so the digest code cannot be said to have caught it.
class ProcessKilled(BaseException):
    pass


class Refusing:
    """A relay that refuses everything, counting how often it was tried."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, message: Message) -> bool:
        self.attempts += 1
        return False


class DiesAt:
    """Dies -- as a process can -- when asked to send to one address."""

    def __init__(self, address: str) -> None:
        self.address = address
        self.sent: list[Message] = []

    async def send(self, message: Message) -> bool:
        if message.to == self.address:
            raise ProcessKilled
        self.sent.append(message)
        return True


async def digests(session: AsyncSession) -> list[GroupDigest]:
    return list(await session.scalars(select(GroupDigest).order_by(GroupDigest.id)))


async def recipient_rows(session: AsyncSession) -> list[GroupDigestRecipient]:
    return list(
        await session.scalars(select(GroupDigestRecipient).order_by(GroupDigestRecipient.id))
    )


async def make_member(
    session: AsyncSession,
    group: Library,
    *,
    user_id: int,
    username: str,
    email: str | None,
) -> None:
    """Create a member subscribed to everything the test group records."""
    await make_user(session, user_id=user_id, username=username, email=email)
    await add_group_member(session, library_id=group.id, user_id=user_id)
    for kind in ActivityKind:
        await groupprefs.set_kind(session, group, user_id=user_id, kind=kind, wanted=True)
    await session.commit()


async def age_email_attempt(session: AsyncSession, minutes: int = 5) -> None:
    """Backdate a pending email attempt, as waiting out the retry lease does."""
    when = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes)
    await session.execute(update(GroupDigestRecipient).values(last_attempt_at=when))
    await session.commit()


class TestBatchIdentity:
    async def test_claimed_rows_and_recipients_belong_to_one_digest(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        await record(session, group, count=2)
        await age(session, minutes=30)

        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        (batch,) = await digests(session)
        (recipient,) = await recipient_rows(session)
        rows = list(await session.scalars(select(GroupActivity)))
        assert rows[0].digest_id == batch.id
        assert rows[0].flushed is not None
        assert recipient.digest_id == batch.id
        assert recipient.user_id == 2
        assert recipient.email == "bob@example.org"
        assert batch.completed_at is not None

    async def test_losing_the_claim_leaves_one_digest_and_no_double_recipients(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        # The loser of a candidate race must leave no trace: no second batch,
        # no second recipients, and a normal sweep afterwards delivers once.
        await record(session, group)
        await age(session, minutes=30)

        winner = await groupdigest._establish(session, group.id)
        loser = await groupdigest._establish(session, group.id)

        assert winner is not None
        assert loser is None
        assert len(await digests(session)) == 1
        assert len(await recipient_rows(session)) == 1

        told = await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert told == 1
        assert len(outbox.sent) == 1
        assert len(await digests(session)) == 1
        assert len(await recipient_rows(session)) == 1


class TestPreferenceSnapshot:
    async def test_changes_after_establishment_do_not_alter_this_batch(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        # Carol is not subscribed when the batch settles; Bob is.
        await make_member(session, group, user_id=3, username="carol", email="carol@example.org")
        for kind in ActivityKind:
            await groupprefs.set_kind(session, group, user_id=3, kind=kind, wanted=False)
        await session.commit()
        await record(session, group)
        await age(session, minutes=30)

        # The batch is established, and only then do preferences move: Bob
        # unsubscribes and Carol subscribes.
        batch = await groupdigest._establish(session, group.id)
        assert batch is not None
        for kind in ActivityKind:
            await groupprefs.set_kind(session, group, user_id=2, kind=kind, wanted=False)
            await groupprefs.set_kind(session, group, user_id=3, kind=kind, wanted=True)
        await session.commit()

        told = await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert told == 1
        assert len(await notices(session, 2)) == 1
        assert await notices(session, 3) == []
        assert [message.to for message in outbox.sent] == ["bob@example.org"]

    async def test_changes_after_establishment_do_apply_to_the_next_batch(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        await make_member(session, group, user_id=3, username="carol", email="carol@example.org")
        for kind in ActivityKind:
            await groupprefs.set_kind(session, group, user_id=3, kind=kind, wanted=False)
        await session.commit()
        await record(session, group)
        await age(session, minutes=30)
        batch = await groupdigest._establish(session, group.id)
        assert batch is not None
        for kind in ActivityKind:
            await groupprefs.set_kind(session, group, user_id=2, kind=kind, wanted=False)
            await groupprefs.set_kind(session, group, user_id=3, kind=kind, wanted=True)
        await session.commit()
        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)
        outbox.sent.clear()

        # A later burst is established under the new preferences.
        await record(session, group)
        await age(session, minutes=30)
        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert len(await notices(session, 2)) == 1
        assert len(await notices(session, 3)) == 1
        assert [message.to for message in outbox.sent] == ["carol@example.org"]


class TestResumingAfterAnExit:
    async def test_a_process_killed_mid_fan_out_is_resumed_without_duplicates(
        self, session: AsyncSession, group: Library, app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Four recipients in id order: bob, carol, dave, erin. The process
        # dies while sending carol's mail. Bob is fully done; carol's notice
        # is durable but her mail is pending; dave and erin are untouched.
        await make_member(session, group, user_id=3, username="carol", email="carol@example.org")
        await make_member(session, group, user_id=4, username="dave", email="dave@example.org")
        await make_member(session, group, user_id=5, username="erin", email="erin@example.org")
        await record(session, group)
        await age(session, minutes=30)

        with pytest.raises(ProcessKilled):
            await groupdigest.sweep(session, DiesAt("carol@example.org").send, quiet_period=QUIET)

        assert len(await notices(session, 2)) == 1
        assert len(await notices(session, 3)) == 1
        assert await notices(session, 4) == []
        assert await notices(session, 5) == []

        # A restart: new process, new session, and the pending attempt is old.
        monkeypatch.setattr(groupdigest, "EMAIL_ATTEMPT_LEASE", timedelta(0))
        outbox = Outbox()
        async with app.state.database.session_factory() as restarted:
            told = await groupdigest.sweep(restarted, outbox.send, quiet_period=QUIET)

        # Carol is not told again; dave and erin are.
        assert told == 2
        for user_id in (2, 3, 4, 5):
            assert len(await notices(session, user_id)) == 1
        assert sorted(message.to for message in outbox.sent) == [
            "carol@example.org",
            "dave@example.org",
            "erin@example.org",
        ]
        (batch,) = await digests(session)
        assert batch.completed_at is not None

    async def test_a_pending_email_after_restart_keeps_the_single_notification(
        self,
        session: AsyncSession,
        group: Library,
        outbox: Outbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        refusing = Refusing()
        await record(session, group)
        await age(session, minutes=30)
        await groupdigest.sweep(session, refusing.send, quiet_period=QUIET)

        (recipient,) = await recipient_rows(session)
        assert recipient.email_state == "pending"
        assert recipient.notification_id is not None
        assert recipient.email_attempts == 1
        assert (await digests(session))[0].completed_at is None

        # Restart finishes the email without raising another notification.
        monkeypatch.setattr(groupdigest, "EMAIL_ATTEMPT_LEASE", timedelta(0))
        told = await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert told == 0
        assert len(await notices(session, 2)) == 1
        assert [message.to for message in outbox.sent] == ["bob@example.org"]
        (recipient,) = await recipient_rows(session)
        assert recipient.email_state == "sent"
        assert (await digests(session))[0].completed_at is not None


class TestEmailRetries:
    async def test_one_refusing_address_does_not_stop_the_rest(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        # The original loss: one mailbox refusing ended the batch, so everyone
        # later in the fan-out never heard anything.
        await make_member(session, group, user_id=3, username="carol", email="carol@example.org")
        await record(session, group)
        await age(session, minutes=30)

        async def send(message: Message) -> bool:
            if message.to == "bob@example.org":
                return False
            outbox.sent.append(message)
            return True

        told = await groupdigest.sweep(session, send, quiet_period=QUIET)

        assert told == 2
        assert len(await notices(session, 2)) == 1
        assert len(await notices(session, 3)) == 1
        assert [message.to for message in outbox.sent] == ["carol@example.org"]
        by_user = {row.user_id: row for row in await recipient_rows(session)}
        assert by_user[2].email_state == "pending"
        assert by_user[3].email_state == "sent"

    async def test_a_refusal_is_not_retried_while_its_attempt_is_fresh(
        self, session: AsyncSession, group: Library
    ) -> None:
        # The attempt lease: a sweep tick beside a long SMTP call must not send
        # a second copy, and a rejecting relay is not hammered every tick.
        refusing = Refusing()
        await record(session, group)
        await age(session, minutes=30)

        await groupdigest.sweep(session, refusing.send, quiet_period=QUIET)
        await groupdigest.sweep(session, refusing.send, quiet_period=QUIET)

        assert refusing.attempts == 1

    async def test_a_refusal_is_retried_after_the_lease_and_succeeds_once(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        refusing = Refusing()
        await record(session, group)
        await age(session, minutes=30)
        await groupdigest.sweep(session, refusing.send, quiet_period=QUIET)
        assert refusing.attempts == 1
        assert outbox.sent == []

        await age_email_attempt(session)
        told = await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert told == 0
        assert len(outbox.sent) == 1
        assert len(await notices(session, 2)) == 1
        (recipient,) = await recipient_rows(session)
        assert recipient.email_state == "sent"
        assert recipient.email_attempts == 2

    async def test_a_mailer_that_raises_is_retried_after_restart(
        self,
        session: AsyncSession,
        group: Library,
        outbox: Outbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        attempts = 0

        async def flaky(message: Message) -> bool:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("relay on fire")
            outbox.sent.append(message)
            return True

        await record(session, group)
        await age(session, minutes=30)
        await groupdigest.sweep(session, flaky, quiet_period=QUIET)
        assert len(await notices(session, 2)) == 1
        assert outbox.sent == []

        await age_email_attempt(session)
        monkeypatch.setattr(groupdigest, "EMAIL_ATTEMPT_LEASE", timedelta(0))
        await groupdigest.sweep(session, flaky, quiet_period=QUIET)

        assert len(outbox.sent) == 1
        assert len(await notices(session, 2)) == 1
        (recipient,) = await recipient_rows(session)
        assert recipient.email_state == "sent"


class TestCommitFailure:
    async def test_a_notification_commit_failure_is_retried_without_duplicates(
        self,
        session: AsyncSession,
        group: Library,
        outbox: Outbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Establishment is the first commit; bob's notification the second.
        # Fail that second commit: nothing about the notification is durable,
        # so the next tick must make exactly one rather than zero or two.
        await record(session, group)
        await age(session, minutes=30)

        real_commit = session.commit
        calls = 0

        async def flaky_commit() -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OperationalError(None, None, RuntimeError("disk full"))
            await real_commit()

        monkeypatch.setattr(session, "commit", flaky_commit)
        told = await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)
        assert told == 0
        assert await notices(session, 2) == []
        (recipient,) = await recipient_rows(session)
        assert recipient.notification_id is None

        monkeypatch.undo()
        told = await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert told == 1
        assert len(await notices(session, 2)) == 1
        assert len(outbox.sent) == 1
        (recipient,) = await recipient_rows(session)
        assert recipient.notification_id is not None
        assert recipient.email_state == "sent"


class TestEmpty:
    async def test_no_subscribers_completes_an_empty_digest(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        for kind in ActivityKind:
            await groupprefs.set_kind(session, group, user_id=2, kind=kind, wanted=False)
        await session.commit()
        await record(session, group)
        await age(session, minutes=30)

        assert await groupdigest.sweep(session, outbox.send, quiet_period=QUIET) == 0
        assert await notices(session, 2) == []
        assert await recipient_rows(session) == []
        (batch,) = await digests(session)
        assert batch.completed_at is not None

        # Nothing left to resume: a later sweep changes nothing.
        assert await groupdigest.sweep(session, outbox.send, quiet_period=QUIET) == 0
        assert await notices(session, 2) == []

    async def test_no_email_address_is_a_terminal_state_not_a_pending_one(
        self, session: AsyncSession, group: Library, outbox: Outbox
    ) -> None:
        await make_member(session, group, user_id=3, username="carol", email=None)
        await record(session, group)
        await age(session, minutes=30)

        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)
        await groupdigest.sweep(session, outbox.send, quiet_period=QUIET)

        assert len(await notices(session, 3)) == 1
        assert [message.to for message in outbox.sent] == ["bob@example.org"]
        rows = {row.user_id: row for row in await recipient_rows(session)}
        assert rows[3].email_state == "none"
        assert rows[2].email_state == "sent"
