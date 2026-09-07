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
    operation: str = ""
    documents: int = 0
    accepted: int = 0
    prepared: int = 0
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
            self.operation, self.documents, self.accepted = "", 0, 0
            self.prepared = 0
        # Measure inactivity of the current operation, not total phase duration.
        # Repeated UI polls with identical facts must not reset this clock.
        advanced = int(event.get("completed", self.completed) or 0) > self.completed
        if (state != self.state or event.get("path", self.path) != self.path
                or event.get("operation", self.operation) != self.operation or advanced):
            self.step_started = now
        self.state = state
        self.total = max(0, int(event.get("total", self.total) or 0))
        self.completed = min(self.total, max(self.completed,
            int(event.get("completed", self.completed) or 0)))
        self.needs_review = max(0, int(event.get("needs_review", self.needs_review) or 0))
        self.errors = max(0, int(event.get("errors", self.errors) or 0))
        self.documents = max(self.documents, int(event.get("documents", self.documents) or 0))
        self.accepted = max(self.accepted, int(event.get("accepted", self.accepted) or 0))
        self.prepared = max(self.prepared, int(event.get("prepared", self.prepared) or 0))
        for key in ("path", "worker", "report", "operation"):
            if key in event:
                setattr(self, key, str(event[key] or ""))
        if state in ("complete", "stopped", "failed", "skipped", "limited", "submitted", "attention"):
            self.finished = now
        return self

    @property
    def percent(self):
        return 100 * self.completed / self.total if self.total else 0

    def wait_seconds(self):
        if self.state not in ("checking", "adjudicating", "running", "writing_report",
                              "scanning", "orienting", "rendering", "submitting"):
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
        if self.phase == "followup_plan":
            return (f"{self.completed:,} of {self.total:,} candidate checks finished"
                    f" · {self.prepared:,} requests sized, not submitted")
        if self.phase == "followup_upload":
            return (f"{self.completed:,} of {self.total:,} remaining requests prepared"
                    f" · {self.accepted:,} accepted overall")
        if self.total:
            unit = {"preparing":"worker-folder preparation passes finished", "batch":"requests prepared",
                    "scanning":"worker-folder scan passes finished",
                    "recovery":"documents prepared for recovery"}.get(self.phase, "worker folders checked")
            count = f"{self.completed:,} of {self.total:,} {unit}"
            if self.phase == "scanning":
                count += f" · {self.documents:,} documents encountered"
                if self.state == "limited":
                    count += " · file limit reached; remaining folders not fully scanned"
            elif self.phase == "batch":
                count += f" · {self.accepted:,} accepted by provider"
            return count
        if self.phase == "scanning":
            return "Scanning documents; total not yet known"
        if self.phase in ("preparing", "batch"):
            return "Preparing the next operation; total not yet known"
        return "Choose a care-home folder to begin"


def concise_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} sec"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} sec"
    return f"{seconds // 3600} hr {(seconds % 3600) // 60} min"
