from __future__ import annotations
import hashlib


def stable_event_id(source: str, source_record_id: str, algo: str = "sha256") -> str:
    """
    Create a stable deterministic event ID from (source, source_record_id).

    - algo: "sha256" (default) or "sha1"
    - Returns: hex string
    """
    if algo not in ("sha1", "sha256"):
        raise ValueError("algo must be 'sha1' or 'sha256'")

    hasher = hashlib.sha1() if algo == "sha1" else hashlib.sha256()

    # Normalize & join the input for deterministic hashing
    key = f"{source}:{source_record_id}".encode("utf-8")

    hasher.update(key)
    return hasher.hexdigest()


