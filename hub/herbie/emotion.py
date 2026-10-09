"""Driver mood from facial expression scores (driver-monitoring camera).

Privacy by design: the browser runs MediaPipe Face Landmarker locally and sends only a
handful of expression scores ("blendshapes", 0..1) here, never images. This module turns
them into a smoothed mood plus a fatigue estimate. A single frown changes nothing; a
mood has to dominate the last ~15 seconds before Herbie reacts, and each reaction has a
cooldown so Herbie doesn't nag.
"""
from __future__ import annotations

import time
from collections import Counter, deque

WINDOW = 10            # samples (~1.5 s apart → ~15 s)
STABLE_SHARE = 0.6     # share of the window a mood needs to count as "the" mood
REACT_COOLDOWN_S = 75  # real seconds between mood-driven remarks of the same kind
FEATURES = ("mouthSmileLeft", "mouthSmileRight", "mouthFrownLeft", "mouthFrownRight", "browDownLeft",
            "browDownRight", "browInnerUp", "eyeBlinkLeft", "eyeBlinkRight", "jawOpen", "mouthPressLeft",
            "mouthPressRight")


def classify(b: dict[str, float]) -> str:
    g = lambda k: float(b.get(k, 0.0) or 0.0)  # noqa: E731
    smile = (g("mouthSmileLeft") + g("mouthSmileRight")) / 2
    frown = (g("mouthFrownLeft") + g("mouthFrownRight")) / 2
    brow_down = (g("browDownLeft") + g("browDownRight")) / 2
    press = (g("mouthPressLeft") + g("mouthPressRight")) / 2
    eyes_closed = (g("eyeBlinkLeft") + g("eyeBlinkRight")) / 2
    if eyes_closed > 0.6 or g("jawOpen") > 0.7:  # long eye closure or a yawn
        return "tired"
    if smile > 0.4:
        return "happy"
    if g("browInnerUp") > 0.35 and (frown > 0.15 or smile < 0.05):
        return "sad"
    if brow_down > 0.35 or frown > 0.3 or press > 0.45:
        return "tense"
    return "neutral"


class MoodTracker:
    def __init__(self, clock=time.monotonic):
        self.samples: deque[str] = deque(maxlen=WINDOW)
        self.closed: deque[float] = deque(maxlen=WINDOW)
        self.stable = "unknown"
        self.last_reaction: dict[str, float] = {}
        self.clock = clock

    def reset(self) -> None:
        self.samples.clear()
        self.closed.clear()
        self.stable = "unknown"

    def update(self, blendshapes: dict[str, float]) -> tuple[str, float, str | None]:
        """Add one sample. Returns (stable mood, fatigue 0..1, mood to react to or None)."""
        mood = classify(blendshapes)
        self.samples.append(mood)
        self.closed.append((float(blendshapes.get("eyeBlinkLeft", 0)) + float(blendshapes.get("eyeBlinkRight", 0))) / 2)
        top, count = Counter(self.samples).most_common(1)[0]
        if len(self.samples) >= WINDOW // 2 and count / len(self.samples) >= STABLE_SHARE:
            changed = top != self.stable
            self.stable = top
        else:
            changed = False
        # PERCLOS-style fatigue: share of samples with eyes mostly closed, plus yawns
        fatigue = min(1.0, sum(1 for c in self.closed if c > 0.5) / max(len(self.closed), 1)
                      + 0.5 * sum(1 for m in self.samples if m == "tired") / max(len(self.samples), 1))
        react = None
        now = self.clock()
        if changed and self.stable not in ("neutral", "unknown"):
            if now - self.last_reaction.get(self.stable, -1e9) >= REACT_COOLDOWN_S:
                self.last_reaction[self.stable] = now
                react = self.stable
        return self.stable, round(fatigue, 2), react
