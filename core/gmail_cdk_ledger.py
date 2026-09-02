from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from core import app_state_db


class GmailCdkQuotaError(RuntimeError):
    """CDK không còn slot khả dụng."""


@dataclass(frozen=True)
class GmailCdkSlot:
    email: str
    status: str
    job_id: str
    cdk_key: str = ""
    phase: str = "original"
    domain: str = ""


class GmailCdkLedger:
    """Ledger JSON giữ reservation và quota CDK giữa các worker."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def cdk_key(cdk: str) -> str:
        canonical = str(cdk or "").strip().upper()
        if not canonical:
            raise GmailCdkQuotaError("CDK không được để trống")
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _email_key(email: str) -> str:
        return str(email or "").strip().lower()

    def _load(self) -> dict:
        if app_state_db.is_app_state_path(self.path):
            data = app_state_db.get_named_document("gmail_cdk_ledger", None)
            if isinstance(data, dict) and isinstance(data.get("cards"), dict):
                return data
            return {"version": 2, "cards": {}}
        if not self.path.exists():
            return {"version": 2, "cards": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GmailCdkQuotaError("Không thể đọc ledger Gmail CDK") from exc
        if not isinstance(data, dict) or not isinstance(data.get("cards"), dict):
            raise GmailCdkQuotaError("Ledger Gmail CDK không hợp lệ")
        return data

    def _save(self, data: dict) -> None:
        if app_state_db.is_app_state_path(self.path):
            app_state_db.set_named_document("gmail_cdk_ledger", data)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def reserve(
        self,
        cdk: str,
        variants: list[str],
        job_id: str,
        *,
        remote_remaining: int,
        configured_limit: int,
    ) -> GmailCdkSlot:
        owner = str(job_id or "").strip()
        if not owner:
            raise GmailCdkQuotaError("Reservation cần job ID")
        limit = max(0, min(6, int(remote_remaining), int(configured_limit), len(variants)))
        with self._lock:
            data = self._load()
            cdk_key = self.cdk_key(cdk)
            card = data["cards"].setdefault(cdk_key, {"slots": {}})
            slots = card.setdefault("slots", {})
            for email in variants[:limit]:
                key = self._email_key(email)
                if key in slots:
                    continue
                slots[key] = {"email": email, "status": "reserved", "job_id": owner}
                self._save(data)
                return GmailCdkSlot(email=email, status="reserved", job_id=owner, cdk_key=cdk_key)
        raise GmailCdkQuotaError("CDK đã hết quota tài khoản")

    def reserve_plan(self, cdk: str, plan, job_id: str) -> GmailCdkSlot:
        owner = str(job_id or "").strip()
        if not owner:
            raise GmailCdkQuotaError("Reservation cần job ID")
        limit = max(0, min(6, int(plan.configured_limit)))
        domains = tuple(plan.routed_domains)
        with self._lock:
            data = self._load()
            data["version"] = 2
            cdk_key = self.cdk_key(cdk)
            card = data["cards"].setdefault(
                cdk_key,
                {"slots": {}, "allocation_phase": "original", "routing_domains": list(domains)},
            )
            bound_domains = tuple(card.get("routing_domains") or ())
            if domains and bound_domains and domains != bound_domains:
                raise GmailCdkQuotaError("Domain routing Gmail đã thay đổi")
            if domains and not bound_domains:
                card["routing_domains"] = list(domains)
            phase = str(card.get("allocation_phase") or "original")
            slots = card.setdefault("slots", {})
            used_phase = sum(
                1 for row in slots.values()
                if row.get("status") in {"reserved", "consumed"}
                and row.get("phase", "original") == phase
            )
            if phase == "original" and used_phase >= limit and domains:
                phase = "routed"
                card["allocation_phase"] = phase
                used_phase = sum(
                    1 for row in slots.values()
                    if row.get("status") in {"reserved", "consumed"}
                    and row.get("phase") == phase
                )
            if used_phase >= limit:
                raise GmailCdkQuotaError("CDK đã hết quota tài khoản")
            for candidate in plan.candidates:
                if candidate.phase != phase:
                    continue
                key = self._email_key(candidate.email)
                if key in slots:
                    continue
                slots[key] = {
                    "email": candidate.email,
                    "status": "reserved",
                    "job_id": owner,
                    "phase": candidate.phase,
                    "domain": candidate.domain,
                }
                self._save(data)
                return GmailCdkSlot(
                    candidate.email, "reserved", owner, cdk_key,
                    candidate.phase, candidate.domain,
                )
        raise GmailCdkQuotaError("CDK đã hết quota tài khoản")

    def find(self, email: str) -> GmailCdkSlot | None:
        key = self._email_key(email)
        with self._lock:
            data = self._load()
            for cdk_key, card in data["cards"].items():
                row = card.get("slots", {}).get(key)
                if row:
                    return GmailCdkSlot(
                        row["email"], row["status"], row["job_id"], cdk_key,
                        row.get("phase", "original"), row.get("domain", ""),
                    )
        return None

    def _transition(self, email: str, job_id: str, target: str | None) -> bool:
        key = self._email_key(email)
        owner = str(job_id or "").strip()
        with self._lock:
            data = self._load()
            for card in data["cards"].values():
                slots = card.get("slots", {})
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

    def release(self, email: str, job_id: str) -> bool:
        return self._transition(email, job_id, None)

    def reconcile(self, *, account_exists, job_is_active) -> int:
        """Khôi phục reservation sau crash dựa trên account và trạng thái job bền vững."""
        changed = 0
        with self._lock:
            data = self._load()
            for card in data["cards"].values():
                slots = card.get("slots", {})
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
