from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations


@dataclass
class Model:
    predecessor: str = "active"
    successor: str = "absent"
    old_audio_quarantined: bool = False
    successor_holds: int = 0
    predecessor_settlements: int = 0
    successor_bindings: int = 0
    forwarded_old_audio: int = 0

    def apply(self, event: str) -> None:
        if event == "speech_started":
            if self.predecessor == "active":
                self.predecessor = "cancelling"
            self.old_audio_quarantined = True
        elif event == "new_commit":
            assert self.successor == "absent"
            self.successor = "pending"
            self.successor_holds += 1
        elif event == "new_created":
            assert self.successor == "pending"
            self.successor = "active"
            self.successor_bindings += 1
        elif event == "old_terminal":
            if self.predecessor != "terminal":
                self.predecessor = "terminal"
                self.predecessor_settlements += 1
        elif event == "duplicate_old_terminal":
            assert self.predecessor == "terminal"
        elif event == "late_old_audio":
            if not self.old_audio_quarantined and self.predecessor == "active":
                self.forwarded_old_audio += 1
        else:
            raise AssertionError(event)

        assert self.successor_holds <= 1
        assert self.successor_bindings <= 1
        assert self.predecessor_settlements <= 1
        live = int(self.predecessor != "terminal") + int(
            self.successor not in {"absent", "terminal"}
        )
        assert live <= 2


events = [
    "speech_started",
    "new_commit",
    "new_created",
    "old_terminal",
    "duplicate_old_terminal",
    "late_old_audio",
]

checked = 0
for ordering in permutations(events):
    pos = {event: ordering.index(event) for event in events}
    if not (
        pos["speech_started"] < pos["new_commit"] < pos["new_created"]
        and pos["old_terminal"] < pos["duplicate_old_terminal"]
        and pos["speech_started"] < pos["late_old_audio"]
    ):
        continue
    model = Model()
    for event in ordering:
        model.apply(event)
    assert model.successor_holds == 1
    assert model.successor_bindings == 1
    assert model.predecessor_settlements == 1
    assert model.forwarded_old_audio == 0
    checked += 1

no_active = Model(predecessor="terminal")
no_active.apply("speech_started")
assert no_active.predecessor_settlements == 0
assert no_active.successor_holds == 0

assert checked == 45, checked
print(f"SP-BARGE-OVERLAP-PASS permutations={checked} cancel_no_active=pass")
