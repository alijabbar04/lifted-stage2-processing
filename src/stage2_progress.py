"""Small, UI-independent model for honest phase progress and wait estimates."""
from dataclasses import dataclass, field
import time


@dataclass
class PhaseProgress:
    phase: str = "idle"
    state: str = "idle"
    completed: int = 0
    total: int = 0
    needs_review: int = 0
    errors: int = 0
    path: str = ""
    worker: str = ""
    report: str = ""
    started: float = 0.0
    step_started: float = 0.0
    finished: float = 0.0
    clock: object = field(default=time.monotonic, repr=False)

    def observe(self, event):
        now = self.clock()
        phase = str(event.get("phase", self.phase))
        state = str(event.get("state", "running"))
        fresh = phase != self.phase or state == "started"
        if fresh:
            self.phase, self.completed, self.total = phase, 0, 0
            self.needs_review, self.errors = 0, 0
            self.started, self.step_started, self.finished = now, now, 0.0
            self.path, self.worker, self.report = "", "", ""
        if state != self.state or event.get("path", self.path) != self.path:
            self.step_started = now
        self.state = state
        self.total = max(0, int(event.get("total", self.total) or 0))
        self.completed = min(self.total, max(self.completed,
            int(event.get("completed", self.completed) or 0)))
        self.needs_review = max(0, int(event.get("needs_review", self.needs_review) or 0))
        self.errors = max(0, int(event.get("errors", self.errors) or 0))
        for key in ("path", "worker", "report"):
            if key in event:
                setattr(self, key, str(event[key] or ""))
        if state in ("complete", "stopped", "failed", "skipped"):
            self.finished = now
        return self

    @property
    def percent(self):
        return 100 * self.completed / self.total if self.total else 0

    def wait_seconds(self):
        if self.state not in ("checking", "adjudicating", "running", "writing_report"):
            return 0
        return max(0, int(self.clock() - self.step_started))

    def eta_seconds(self):
        """Only estimate measured audit throughput, never invent batch timing."""
        if (self.phase != "audit" or self.completed < 3 or not self.total
                or self.completed >= self.total
                or self.state not in ("checking", "adjudicating", "document_done")
                or self.wait_seconds() >= 90):
            return None
        elapsed = self.clock() - self.started
        if elapsed < 15:
            return None
        return max(0, int(elapsed / self.completed * (self.total - self.completed)))

    def caption(self):
        if self.phase == "audit":
            count = f"{self.completed:,} of {self.total:,} documents checked"
            if self.errors:
                count = (f"{self.completed:,} of {self.total:,} audit attempts finished"
                         f" · {self.errors:,} check failures")
            if self.state == "complete":
                return count + " · review report ready"
            if self.state in ("stopped", "failed", "skipped"):
                return count + " · audit incomplete"
            if self.state == "writing_report":
                return count + " · saving report"
            return count + f" · {self.percent:.0f}%"
        if self.total:
            unit = {"preparing":"worker folders prepared", "batch":"requests prepared",
                    "recovery":"documents prepared for recovery"}.get(self.phase, "worker folders checked")
            return f"{self.completed:,} of {self.total:,} {unit}"
        return "Choose a care-home folder to begin"


def concise_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} sec"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} sec"
    return f"{seconds // 3600} hr {(seconds % 3600) // 60} min"
