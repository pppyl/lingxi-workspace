"""Carry a business feature's permission into HTTP sessions and pool workers."""
from concurrent.futures import ThreadPoolExecutor as BaseExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context

current_feature = ContextVar('workspace_feature', default=None)


class FeatureLease:
    def __init__(self, manager, feature_id):
        self.manager, self.feature_id = manager, feature_id
        self.generation = manager.generations.get(feature_id, 0)

    def allowed(self):
        return (self.manager.enabled(self.feature_id)
                and self.manager.generations.get(self.feature_id, 0) == self.generation)


@contextmanager
def feature_scope(lease):
    token = current_feature.set(lease)
    try:
        yield
    finally:
        current_feature.reset(token)


def feature_running():
    lease = current_feature.get()
    return lease is None or lease.allowed()


class ThreadPoolExecutor(BaseExecutor):
    def submit(self, function, /, *args, **kwargs):
        return super().submit(copy_context().run, function, *args, **kwargs)
