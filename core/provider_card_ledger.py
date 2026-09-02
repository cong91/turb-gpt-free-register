from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from core import app_state_db


class ProviderCardQuotaError(RuntimeError):
    """Card không còn slot khả dụng hoặc ledger không hợp lệ."""


@dataclass(frozen=True)
class ProviderCardSlot:
    email: str
    status: str
    job_id: str
    card_key: str = ""


class ProviderCardLedger:
    """Ledger JSON dùng reservation card cho provider không thuộc Gmail."""

    def __init__(self, path: str | Path, provider_name: str = "provider"):
        self.path = Path(path)
        self.provider_name = str(provider_name or "provider").strip() or "provider"
        self._lock = threading.RLock()

    def card_key(self, card: str) -> str:
        canonical = str(card or "").strip().upper()
        if not canonical:
            raise ProviderCardQuotaError("Card không được để trống")
        return "sha256:" + hashlib.sha256(
            f"{self.provider_name}:{canonical}".encode()
        ).hexdigest()

    @staticmethod
    def _email_key(email: str) -> str:
        return str(email or "").strip().lower()

    def _load(self) -> dict:
        if app_state_db.is_app_state_path(self.path):
            key = f"provider_card_ledger:{self.provider_name}"
            data = app_state_db.get_named_document(key, None)
            if isinstance(data, dict) and isinstance(data.get("cards"), dict):
                return data
            return {"version": 1, "cards": {}}
        if not self.path.exists():
            return {"version": 1, "cards": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderCardQuotaError("Không thể đọc ledger card") from exc
        if not isinstance(data, dict) or not isinstance(data.get("cards"), dict):
            raise ProviderCardQuotaError("Ledger card không hợp lệ")
        return data

    def _save(self, data: dict) -> None:
        if app_state_db.is_app_state_path(self.path):
            app_state_db.set_named_document(
                f"provider_card_ledger:{self.provider_name}", data
            )
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def reserve(
        self,
        card: str,
        variants: list[str],
        job_id: str,
        *,
        remote_remaining: int,
        configured_limit: int,
        max_capacity: int = 6,
    ) -> ProviderCardSlot:
        owner = str(job_id or "").strip()
        if not owner:
            raise ProviderCardQuotaError("Reservation cần job ID")
        limit = max(0, min(int(max_capacity), int(remote_remaining), int(configured_limit)))
        with self._lock:
            data = self._load()
            card_key = self.card_key(card)
            card_data = data["cards"].setdefault(card_key, {"slots": {}})
            if card_data.get("blocked"):
                raise ProviderCardQuotaError("Card đã bị chặn bởi provider")
            slots = card_data.setdefault("slots", {})
            if len(slots) >= limit:
                raise ProviderCardQuotaError("Card đã hết quota tài khoản")
            used_emails = set()
            for existing_card in data["cards"].values():
                used_emails.update(existing_card.get("slots", {}).keys())
            for email in variants:
                key = self._email_key(email)
                if key in used_emails:
                    continue
                slots[key] = {"email": email, "status": "reserved", "job_id": owner}
                self._save(data)
                return ProviderCardSlot(email=email, status="reserved", job_id=owner, card_key=card_key)
        raise ProviderCardQuotaError("Card đã hết quota tài khoản")

    def block_card(self, email: str, job_id: str, reason: str) -> bool:
        key = self._email_key(email)
        owner = str(job_id or "").strip()
        with self._lock:
            data = self._load()
            for card_data in data["cards"].values():
                slots = card_data.get("slots", {})
                row = slots.get(key)
                if not row or row.get("job_id") != owner:
                    continue
                card_data["blocked"] = True
                card_data["blocked_reason"] = str(reason or "provider_rejected")[:120]
                self._save(data)
                return True
        return False

    def has_card(self, card: str) -> bool:
        with self._lock:
            data = self._load()
            return self.card_key(card) in data["cards"]

    def find(self, email: str) -> ProviderCardSlot | None:
        key = self._email_key(email)
        with self._lock:
            data = self._load()
            for card_key, card_data in data["cards"].items():
                row = card_data.get("slots", {}).get(key)
                if row:
                    return ProviderCardSlot(row["email"], row["status"], row["job_id"], card_key)
        return None

    def _transition(self, email: str, job_id: str, target: str | None) -> bool:
        key = self._email_key(email)
        owner = str(job_id or "").strip()
        with self._lock:
            data = self._load()
            for card_data in data["cards"].values():
                slots = card_data.get("slots", {})
                row = slots.get(key)
                if not row or row.get("job_id") != owner or row.get("status") != "reserved":
                    continue
                if target is None:
                    del slots[key]
                else:
                    row["status"] = target
                self._save(data)
                return True
        return False

    def consume(self, email: str, job_id: str) -> bool:
        return self._transition(email, job_id, "consumed")

    def mark(self, email: str, job_id: str, status: str) -> bool:
        value = str(status or "").strip().lower()
        if value in {"", "available", "reserved"}:
            raise ProviderCardQuotaError("Trạng thái card không hợp lệ")
        return self._transition(email, job_id, value)

    def release(self, email: str, job_id: str) -> bool:
        return self._transition(email, job_id, None)

    def reconcile(self, *, account_exists, job_is_active) -> int:
        changed = 0
        with self._lock:
            data = self._load()
            for card_data in data["cards"].values():
                slots = card_data.get("slots", {})
                for key, row in list(slots.items()):
                    if row.get("status") != "reserved":
                        continue
                    if account_exists(row.get("email", "")):
                        row["status"] = "consumed"
                        changed += 1
                    elif not job_is_active(row.get("job_id", "")):
                        del slots[key]
                        changed += 1
            if changed:
                self._save(data)
        return changed
