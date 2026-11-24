"""Joiner, mover, and leaver processing for the thin HR feed."""

from __future__ import annotations

from iga_lab.errors import FeedError
from iga_lab.models import HrEvent, JmlResult, Person, Warehouse
from iga_lab.publish import Publisher
from iga_lab.roles import clear_assignments, sync_birthright


def apply_feed(
    warehouse: Warehouse,
    events: list[HrEvent],
    publisher: Publisher,
    clock: object,
) -> list[JmlResult]:
    """Apply HR events in order. There is no approval step."""
    seen: set[str] = set()
    results: list[JmlResult] = []
    for event in events:
        if event.event_id in seen:
            raise FeedError(f"Duplicate HR event id {event.event_id}.")
        seen.add(event.event_id)
        clock.advance(minutes=1)  # type: ignore[attr-defined]
        if event.event_type == "joiner":
            result = _joiner(warehouse, event, publisher, clock)
        elif event.event_type == "mover":
            result = _mover(warehouse, event, publisher, clock)
        elif event.event_type == "leaver":
            result = _leaver(warehouse, event, publisher, clock)
        else:
            raise FeedError(f"Unsupported HR event type {event.event_type}.")
        results.append(result)
    return results


def _joiner(warehouse: Warehouse, event: HrEvent, publisher: Publisher, clock: object) -> JmlResult:
    person_row = event.person or {}
    person_id = str(person_row.get("person_id") or "")
    if not person_id:
        raise FeedError(f"Joiner {event.event_id} needs a person_id.")
    if person_id in warehouse.people:
        raise FeedError(f"Joiner {event.event_id} person {person_id} already exists.")
    manager_id = person_row.get("manager_id")
    manager_text = None if manager_id in (None, "") else str(manager_id)
    if manager_text and manager_text not in warehouse.people:
        raise FeedError(f"Joiner {event.event_id} manager {manager_text} does not exist.")
    email = str(person_row.get("email") or "")
    if any(person.email == email for person in warehouse.people.values()):
        raise FeedError(f"Joiner {event.event_id} email {email} already exists.")
    person = Person(
        person_id=person_id,
        display_name=_need(person_row, "display_name", event.event_id),
        person_type=_need(person_row, "person_type", event.event_id),
        status="active",
        department=_need(person_row, "department", event.event_id),
        title=_need(person_row, "title", event.event_id),
        email=_need(person_row, "email", event.event_id),
        uid=_need(person_row, "uid", event.event_id),
        manager_id=manager_text,
        start_date=_need(person_row, "start_date", event.event_id),
        end_date=None,
    )
    if person.person_type not in {"employee", "contractor"}:
        raise FeedError(f"Joiner {event.event_id} has an unsupported person_type.")
    if "@" not in person.email:
        raise FeedError(f"Joiner {event.event_id} needs an email address.")
    if any(existing.uid == person.uid for existing in warehouse.people.values()):
        raise FeedError(f"Joiner {event.event_id} uid {person.uid} already exists.")
    warehouse.people[person.person_id] = person
    notes = sync_birthright(warehouse, clock.iso())  # type: ignore[attr-defined]
    own_notes = [note for note in notes if note.startswith(f"grant {person.person_id} ")]
    provisioning = publisher.publish()
    summary = (
        f"JOIN {person.person_id} {person.display_name} {person.person_type} {person.department}"
    )
    return JmlResult(
        event_id=event.event_id,
        event_type="joiner",
        person_id=person.person_id,
        effective_at=event.effective_at,
        summary=summary,
        notes=own_notes,
        provisioning=provisioning,
    )


def _mover(warehouse: Warehouse, event: HrEvent, publisher: Publisher, clock: object) -> JmlResult:
    if not event.person_id or event.person_id not in warehouse.people:
        raise FeedError(f"Mover {event.event_id} person {event.person_id} does not exist.")
    person = warehouse.people[event.person_id]
    if person.status != "active":
        raise FeedError(f"Mover {event.event_id} person {person.person_id} is not active.")
    changes = event.changes or {}
    if not changes:
        raise FeedError(f"Mover {event.event_id} has no changes.")
    previous_department = person.department
    if "department" in changes:
        person.department = str(changes["department"])
    if "title" in changes:
        person.title = str(changes["title"])
    if "email" in changes:
        email = str(changes["email"])
        if "@" not in email:
            raise FeedError(f"Mover {event.event_id} needs an email address.")
        person.email = email
    if "person_type" in changes:
        person_type = str(changes["person_type"])
        if person_type not in {"employee", "contractor"}:
            raise FeedError(f"Mover {event.event_id} has an unsupported person_type.")
        person.person_type = person_type
    if "manager_id" in changes:
        manager_id = changes["manager_id"]
        manager_text = None if manager_id in (None, "") else str(manager_id)
        if manager_text and manager_text not in warehouse.people:
            raise FeedError(f"Mover {event.event_id} manager {manager_text} does not exist.")
        if manager_text == person.person_id:
            raise FeedError(f"Mover {event.event_id} cannot set the person as their own manager.")
        person.manager_id = manager_text
    notes = sync_birthright(warehouse, clock.iso())  # type: ignore[attr-defined]
    own_notes = [note for note in notes if note.split()[1] == person.person_id]
    provisioning = publisher.publish()
    summary = (
        f"MOVE {person.person_id} {person.display_name} "
        f"{previous_department} -> {person.department}"
    )
    return JmlResult(
        event_id=event.event_id,
        event_type="mover",
        person_id=person.person_id,
        effective_at=event.effective_at,
        summary=summary,
        notes=own_notes,
        provisioning=provisioning,
    )


def _need(row: dict[str, object], field: str, event_id: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise FeedError(f"Joiner {event_id} is missing {field}.")
    return value


def _leaver(warehouse: Warehouse, event: HrEvent, publisher: Publisher, clock: object) -> JmlResult:
    del clock
    if not event.person_id or event.person_id not in warehouse.people:
        raise FeedError(f"Leaver {event.event_id} person {event.person_id} does not exist.")
    person = warehouse.people[event.person_id]
    if person.status == "terminated":
        raise FeedError(f"Leaver {event.event_id} person {person.person_id} is already terminated.")
    if not event.end_date:
        raise FeedError(f"Leaver {event.event_id} needs end_date.")
    person.status = "terminated"
    person.end_date = event.end_date
    notes = clear_assignments(warehouse, person.person_id)
    provisioning = publisher.publish()
    summary = f"LEAVE {person.person_id} {person.display_name} end {event.end_date}"
    return JmlResult(
        event_id=event.event_id,
        event_type="leaver",
        person_id=person.person_id,
        effective_at=event.effective_at,
        summary=summary,
        notes=notes,
        provisioning=provisioning,
    )
