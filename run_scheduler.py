import threading


def workspace_conflict(left, right):
    from pathlib import Path
    left, right = Path(left).resolve(), Path(right).resolve()
    return left == right or left in right.parents or right in left.parents


class RunScheduler:
    def __init__(self):
        self.condition = threading.Condition()
        self.waiting = []
        self.running = {}
        self.closed = False

    def acquire(self, job, limit, cancelled):
        with self.condition:
            self.waiting.append(job)
            try:
                while True:
                    if self.closed or cancelled():
                        return False
                    previous = self.waiting[:self.waiting.index(job)]
                    conflicts = lambda other: (other["task_id"] == job["task_id"] or
                                                workspace_conflict(other["workspace"], job["workspace"]))
                    if (len(self.running) < limit and not any(conflicts(other) for other in self.running.values())
                            and not any(conflicts(other) for other in previous)):
                        self.running[job["id"]] = job
                        return True
                    self.condition.wait(timeout=0.2)
            finally:
                self.waiting.remove(job)

    def release(self, run_id):
        with self.condition:
            self.running.pop(run_id, None)
            self.condition.notify_all()

    def notify(self):
        with self.condition:
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
