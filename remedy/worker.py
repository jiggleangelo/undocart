"""Killable HTTP computation; socket deadlines cannot stop Python CPU work."""
import multiprocessing
import pickle
from threading import BoundedSemaphore

COMPUTE_TIMEOUT = 2.0
MAX_RESULT_BYTES = 65536
_SLOTS = BoundedSemaphore(4)


class BusyError(Exception):
    pass


class ComputeTimeout(Exception):
    pass


def _child(connection, function, args):
    try:
        try:
            payload = pickle.dumps((True, function(*args)), protocol=5)
            if len(payload) > MAX_RESULT_BYTES:
                raise ValueError("computation result is too large")
        except Exception:
            payload = pickle.dumps((False, None), protocol=5)
        connection.send_bytes(payload)
    finally:
        connection.close()


def bounded_call(function, *args, timeout=COMPUTE_TIMEOUT):
    if not _SLOTS.acquire(blocking=False):
        raise BusyError("computation capacity reached")
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(writer, function, args), daemon=True)
    started = False
    try:
        process.start()
        started = True
        writer.close()
        if not reader.poll(timeout):
            raise ComputeTimeout("computation deadline exceeded")
        try:
            success, value = pickle.loads(reader.recv_bytes(MAX_RESULT_BYTES))
        except (EOFError, OSError) as exc:
            raise ValueError("computation failed") from exc
        if not success:
            raise ValueError("invalid case or artefact")
        return value
    finally:
        if started:
            process.join(0.05)
            if process.is_alive():
                process.kill()
            process.join()
            process.close()
        reader.close()
        writer.close()
        _SLOTS.release()


def parse_case(text):
    import json
    from .models import Case
    return Case.from_dict(json.loads(text))


def evaluate(raw, sources, today):
    from .artefacts import generate
    from .classify import classify
    from .models import Case
    case = Case.from_dict(raw)
    decision = classify(case, sources, today)
    return case, decision, generate(case, decision)


def evaluate_json(text, sources, today):
    import json
    return evaluate(json.loads(text), sources, today)
