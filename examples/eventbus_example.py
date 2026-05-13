"""
EventBus example — in-process pub/sub between two Modules.

This mirrors the worked example in ``CONTEXT.md``: a ``CreateUser`` Command
runs a user-creation Module; on success, that Module **publishes** a
``UserCreated`` Event; a separate ``AuditModule`` **subscribes** to
``UserCreated`` and records it for the audit log.

The two Modules know nothing about each other. The publisher does not
import the audit module; the audit module does not import the user
module. They only share the ``UserCreated`` Event class, which sits in a
neutral location.

Run with::

    python -m examples.eventbus_example
"""

from dataclasses import dataclass, field

from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    Event,
    Module,
    ModuleHost,
    handles,
    module,
    subscribes,
)


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------


@dataclass
class CreateUserInput(CommandRequest):
    email: str = ""
    display_name: str = ""


@dataclass
class CreateUserOutput(CommandResponse):
    user_id: str = ""


class CreateUserCommand(Command[CreateUserInput, CreateUserOutput]):
    """Command that creates a user and publishes a ``UserCreated`` event."""

    name = "users.create"


@dataclass
class UserCreated(Event):
    """In-process fan-out event broadcast after a user is created."""

    user_id: str = ""
    email: str = ""
    display_name: str = ""
    name: str = "users.user_created"


# ---------------------------------------------------------------------------
# Publisher: handles CreateUser, publishes UserCreated on success
# ---------------------------------------------------------------------------


@module(name="UserService", description="Creates users", version="1.0.0")
class UserServiceModule(Module):
    """
    The Module that wins ``CreateUserCommand``. After it persists the user,
    it explicitly publishes ``UserCreated`` via the host's EventBus. The
    framework never auto-publishes — fan-out is the handler's call.
    """

    def __init__(self, host: ModuleHost) -> None:
        super().__init__()
        # Modules normally have no host back-reference; for the publish step
        # the example wires one in deliberately. Production code would
        # typically inject ``host.event_bus`` (or any ``EventBus``) instead
        # of the whole host, to keep the surface small.
        self._host = host
        self._next_id = 1

    @handles(CreateUserCommand)
    def create_user(self, command: CreateUserCommand) -> CreateUserOutput:
        req = command.request
        assert req is not None  # narrow Optional for mypy

        # Pretend-persist.
        user_id = f"u-{self._next_id:04d}"
        self._next_id += 1
        print(f"[UserService] persisted user {user_id} ({req.email})")

        # Publish in-process. Subscribers run synchronously inline here;
        # exceptions in any one subscriber are isolated by the EventBus.
        self._host.publish(
            UserCreated(
                user_id=user_id,
                email=req.email,
                display_name=req.display_name,
            )
        )

        return CreateUserOutput(user_id=user_id)


# ---------------------------------------------------------------------------
# Subscribers: each listens for UserCreated independently
# ---------------------------------------------------------------------------


@module(name="AuditLog", description="Records user-creation events")
class AuditLogModule(Module):
    """Listens for ``UserCreated`` and appends to an in-memory audit log."""

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[tuple[str, str]] = []

    @subscribes(UserCreated)
    def on_user_created(self, event: UserCreated) -> None:
        self.entries.append((event.user_id, event.email))
        print(f"[AuditLog] recorded {event.user_id} -> {event.email}")


@module(name="WelcomeMailer", description="Sends welcome emails on user creation")
class WelcomeMailerModule(Module):
    """A second, independent subscriber for the same event class."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    @subscribes(UserCreated)
    def on_user_created(self, event: UserCreated) -> None:
        # Multiple Modules can subscribe to the same Event — that is the
        # whole point of pub/sub fan-out.
        self.sent.append(event.email)
        print(f"[WelcomeMailer] sent welcome email to {event.email}")


# ---------------------------------------------------------------------------
# Wire-up
# ---------------------------------------------------------------------------


def main() -> None:
    host = ModuleHost()

    # Subscribers register first so they catch the event published from
    # inside the next dispatch.
    audit = AuditLogModule()
    mailer = WelcomeMailerModule()
    host.register(audit)
    host.register(mailer)

    # Publisher (knows nothing about audit/mailer).
    host.register(UserServiceModule(host=host))

    response = host.dispatch(
        CreateUserCommand(
            request=CreateUserInput(email="alice@example.com", display_name="Alice"),
        )
    )

    print()
    print(f"Created user_id = {response.user_id}")
    print(f"Audit log       = {audit.entries}")
    print(f"Welcome emails  = {mailer.sent}")

    host.shutdown()


if __name__ == "__main__":
    main()
