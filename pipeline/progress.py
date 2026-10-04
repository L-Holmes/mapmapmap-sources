"""The pipeline's progress lines: about how long a step has left."""
import time


def left(start, done, total):
    """About how long is left, going by how long the first `done` of `total` took since `start`."""
    if done >= total:
        return "done"
    if done <= 0:
        return "working out how long it will take"
    s = (time.time() - start) / done * (total - done)
    return f"about {s / 60:.0f} min left" if s >= 90 else f"about {max(s, 1):.0f} s left"
