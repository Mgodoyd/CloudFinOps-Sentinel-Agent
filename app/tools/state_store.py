"""Where the Memory Bank persists.

Two backends behind one interface:

* **JSON file** — zero setup, fine for local work.
* **Firestore** — required once the agent runs on Cloud Run, whose filesystem is
  ephemeral: a new revision would otherwise start with no memory of what it had
  already remediated, and the loop protection would silently stop working.

The store is deliberately dumb — load and save a dict. Keeping the merge logic
in MemoryBank means the backend can change without touching the agent.
"""

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

COLLECTION = "cloudfinops_sentinel"
DOCUMENT = "memory_bank"


class StateStore:
    """Interface: load() -> dict | None, save(dict) -> bool."""

    def load(self) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def save(self, state: Dict[str, Any]) -> bool:
        raise NotImplementedError

    @property
    def describe(self) -> str:
        raise NotImplementedError


class NullStore(StateStore):
    """No persistence at all — used by tests and throwaway instances."""

    def load(self) -> Optional[Dict[str, Any]]:
        return None

    def save(self, state: Dict[str, Any]) -> bool:
        return True

    @property
    def describe(self) -> str:
        return "in-memory (no persistence)"


class FileStore(StateStore):
    def __init__(self, path: str):
        self.path = path

    def load(self) -> Optional[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s (%s)", self.path, exc)
            return None

    def save(self, state: Dict[str, Any]) -> bool:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
            os.replace(tmp, self.path)  # atomic: never a half-written file
            return True
        except OSError as exc:
            logger.warning("Could not persist to %s: %s", self.path, exc)
            return False

    @property
    def describe(self) -> str:
        return f"file:{self.path}"


class FirestoreStore(StateStore):
    """Survives Cloud Run revisions, and is shared across instances."""

    def __init__(self, project_id: str, collection: str = COLLECTION, document: str = DOCUMENT):
        self.project_id = project_id
        self.collection = collection
        self.document = document
        self._client = None
        self._failed = False

    def _doc(self):
        if self._client is None:
            from google.cloud import firestore

            self._client = firestore.Client(project=self.project_id)
        return self._client.collection(self.collection).document(self.document)

    def load(self) -> Optional[Dict[str, Any]]:
        if self._failed:
            return None
        try:
            snapshot = self._doc().get()
            if not snapshot.exists:
                return None
            logger.info("Memory bank restored from Firestore")
            return snapshot.to_dict()
        except Exception as exc:
            # Degrade rather than refuse to start: a Sentinel that cannot read
            # its history is still more useful than one that will not boot.
            logger.warning("Firestore unavailable (%s); continuing without history", exc)
            self._failed = True
            return None

    def save(self, state: Dict[str, Any]) -> bool:
        if self._failed:
            return False
        try:
            self._doc().set(state)
            return True
        except Exception as exc:
            logger.warning("Could not persist to Firestore: %s", exc)
            return False

    @property
    def describe(self) -> str:
        return f"firestore:{self.collection}/{self.document}"


def build_store(backend: str, project_id: str, state_file: Optional[str]) -> StateStore:
    """Pick a backend. 'auto' uses Firestore on Cloud Run, a file locally."""
    backend = (backend or "auto").strip().lower()

    if backend == "none" or state_file is None and backend == "auto":
        return NullStore()
    if backend == "firestore":
        return FirestoreStore(project_id)
    if backend == "file":
        return FileStore(state_file or "data/memory_bank.json")

    # auto: Cloud Run sets K_SERVICE and has an ephemeral filesystem.
    if os.environ.get("K_SERVICE"):
        logger.info("Cloud Run detected; persisting the memory bank to Firestore")
        return FirestoreStore(project_id)
    return FileStore(state_file or "data/memory_bank.json")
