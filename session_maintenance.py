"""Maintain the APP session and restore businesses, without sending an SMS."""
import threading
import time


class SessionMaintenance:
    def __init__(self, logins, check, *, interval=600, retry_after=60, now=None):
        self.logins, self.check = logins, check
        self.interval, self.retry_after = interval, retry_after
        self.now = now or time.monotonic
        self.due = {}
        self.revisions = {}
        self.stopping = threading.Event()
        self.thread = None

    def tick(self):
        with self.logins.lock:
            records = list(self.logins.records.values())
        for saved in records:
            if self.stopping.is_set():
                return
            revision = saved.credential_revision
            changed = self.revisions.get(saved.id) != revision
            if saved.status == 'login_required' or not changed and self.now() < self.due.get(saved.id, 0):
                continue
            try:
                ok = self.check(saved)
            except Exception:
                # A failing account cannot terminate recovery for other accounts.
                ok = False
            self.due[saved.id] = self.now() + (self.interval if ok else self.retry_after)
            self.revisions[saved.id] = saved.credential_revision
        current_ids = {saved.id for saved in records}
        self.due = {key:value for key,value in self.due.items() if key in current_ids}
        self.revisions = {key:value for key,value in self.revisions.items() if key in current_ids}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name='shared-session-maintenance', daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stopping.is_set():
            self.tick()
            self.stopping.wait(5)

    def stop(self):
        self.stopping.set()
