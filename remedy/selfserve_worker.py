"""Bounded spawn-process helpers for untrusted self-serve input."""
import json
import multiprocessing
import threading
from email.parser import BytesParser
from email.policy import default
from urllib.parse import parse_qs

from .selfserve import InputError, check, parse_json

COMPUTE_TIMEOUT = 3
MAX_COMPUTE = 4
MAX_FORM_FIELDS = 200


class BusyError(RuntimeError):
    pass


class ComputeTimeout(RuntimeError):
    pass


_slots = threading.BoundedSemaphore(MAX_COMPUTE)
_context = multiprocessing.get_context("spawn")


def parse_fields(content_type, raw):
    if not isinstance(raw, bytes):
        raise InputError("The request body is invalid.")
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type == "application/x-www-form-urlencoded":
        try:
            parsed = parse_qs(raw.decode("utf-8", "strict"), keep_blank_values=True,
                              max_num_fields=MAX_FORM_FIELDS, errors="strict")
        except (UnicodeDecodeError, ValueError):
            raise InputError("The submitted form is invalid.") from None
        return {key: values[-1] if values else "" for key, values in parsed.items()}
    if media_type == "multipart/form-data":
        try:
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + content_type.encode("ascii", "strict") +
                b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
            )
        except (UnicodeEncodeError, ValueError):
            raise InputError("The uploaded form is invalid.") from None
        if not message.is_multipart():
            raise InputError("The uploaded form is invalid.")
        fields = {}
        for count, part in enumerate(message.iter_parts(), 1):
            if count > MAX_FORM_FIELDS:
                raise InputError("The submitted form has too many fields.")
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            try:
                fields[name] = payload.decode(part.get_content_charset() or "utf-8", "strict")
            except (LookupError, UnicodeDecodeError):
                raise InputError("Upload fields must be UTF-8 text.") from None
        return fields
    raise InputError("Use a standard form or multipart upload.")


def evaluate_raw(raw, today):
    case = parse_json(json.dumps(raw))
    return case, check(case, today)


def evaluate_many(raws, today):
    return [evaluate_raw(raw, today)[1] for raw in raws]


def _run(connection, function, args):
    try:
        connection.send((True, function(*args)))
    except BaseException as exc:
        try:
            connection.send((False, (type(exc).__module__, type(exc).__name__, str(exc))))
        except BaseException:
            pass
    finally:
        connection.close()


def bounded_call(function, *args, timeout=COMPUTE_TIMEOUT):
    if timeout <= 0 or not _slots.acquire(False):
        raise BusyError("processing capacity reached")
    parent, child = _context.Pipe(False)
    process = _context.Process(target=_run, args=(child, function, args), daemon=True)
    try:
        process.start()
        child.close()
        if not parent.poll(timeout):
            process.terminate()
            process.join(1)
            raise ComputeTimeout("isolated computation timed out")
        ok, payload = parent.recv()
        process.join(1)
        if ok:
            return payload
        module, name, message = payload
        if name == "InputError":
            raise InputError(message)
        if name in {"ValueError", "TypeError", "AttributeError", "ArithmeticError", "JSONDecodeError"}:
            raise ValueError(message)
        raise RuntimeError(f"worker failed: {module}.{name}")
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(1)
        _slots.release()
