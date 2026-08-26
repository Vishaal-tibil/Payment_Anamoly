from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from .models import CanonicalEvent, Individual, Merchant


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _extract_account_type(event: CanonicalEvent) -> str | None:
    if isinstance(event.risk_flags, dict):
        for key in ("account_type", "entity_account_type"):
            if key in event.risk_flags:
                return event.risk_flags[key]
    return None


def _enrich_merchant(merchant: Merchant, event: CanonicalEvent) -> None:
    """Fills gaps on an existing merchant from a later row, never overwrites.

    A merchant transacts across multiple rails; if it was first resolved
    from a row on a rail with no entity-name field (e.g. Card, Cheque),
    a later row on a rail that does have one should still fill legal_name
    in rather than leaving it permanently null -- same enrichment-only
    philosophy as canonical_events' own merge rule.
    """
    if merchant.legal_name is None and event.payer_name:
        merchant.legal_name = event.payer_name
    if merchant.merchant_account is None and event.payer_account_ref:
        merchant.merchant_account = {"account_ref": event.payer_account_ref}
    if merchant.processor_name is None and event.processor_name:
        merchant.processor_name = event.processor_name
    if merchant.onboarded_by is None and event.onboarded_by:
        merchant.onboarded_by = event.onboarded_by


def _get_or_create_merchant(db: Session, event: CanonicalEvent) -> tuple[Merchant, bool]:
    existing = (
        db.query(Merchant)
        .filter_by(source_merchant_id=event.source_merchant_id, tenant_bank_id=event.tenant_bank_id)
        .one_or_none()
    )
    if existing is not None:
        _enrich_merchant(existing, event)
        return existing, False

    now = _utcnow()
    merchant = Merchant(
        merchant_id=f"MER-{str(uuid4())[:8].upper()}",
        source_merchant_id=event.source_merchant_id,
        tenant_bank_id=event.tenant_bank_id,
        # payer_name is where the entity's own name lives, by the
        # entity->payer_name convention every tenant's mapping config
        # follows. No payee_name fallback: on rails where the source
        # genuinely never exposes the entity's name (e.g. Card has no
        # cardholder name, Cheque has no drawer name -- only the
        # counterparty's), payee_name names a *different* party, and
        # substituting it here would mislabel the resolved merchant with
        # its counterparty's name instead of honestly leaving it null.
        legal_name=event.payer_name,
        merchant_account={"account_ref": event.payer_account_ref} if event.payer_account_ref else None,
        processor_name=event.processor_name,
        onboarded_by=event.onboarded_by,
        created_at=now,
        updated_at=now,
    )
    db.add(merchant)
    db.flush()
    return merchant, True


def _enrich_individual(individual: Individual, event: CanonicalEvent) -> None:
    """Same gap-filling enrichment as _enrich_merchant, for individuals."""
    if individual.full_name is None and event.payer_name:
        individual.full_name = event.payer_name
    if individual.account_ref is None and event.payer_account_ref:
        individual.account_ref = event.payer_account_ref
    if individual.account_type is None:
        account_type = _extract_account_type(event)
        if account_type:
            individual.account_type = account_type
    if individual.onboarded_by is None and event.onboarded_by:
        individual.onboarded_by = event.onboarded_by


def _get_or_create_individual(db: Session, event: CanonicalEvent) -> tuple[Individual, bool]:
    existing = (
        db.query(Individual)
        .filter_by(source_individual_id=event.source_individual_id, tenant_bank_id=event.tenant_bank_id)
        .one_or_none()
    )
    if existing is not None:
        _enrich_individual(existing, event)
        return existing, False

    now = _utcnow()
    individual = Individual(
        individual_id=f"IND-{str(uuid4())[:8].upper()}",
        source_individual_id=event.source_individual_id,
        tenant_bank_id=event.tenant_bank_id,
        # Same reasoning as merchant legal_name above -- payer_name only, no
        # payee_name fallback.
        full_name=event.payer_name,
        account_ref=event.payer_account_ref,
        account_type=_extract_account_type(event),
        onboarded_by=event.onboarded_by,
        created_at=now,
        updated_at=now,
    )
    db.add(individual)
    db.flush()
    return individual, True


def resolve_parties(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Resolves source_merchant_id/source_individual_id on canonical_events
    rows into canonical merchant_id/individual_id.

    Idempotent: a row already carrying a resolved id is never touched
    again, so re-running produces no new merchants/individuals and no
    changed ids. Tenant-isolated: lookup is always scoped to
    (source_id, tenant_bank_id), so the same source id under two tenants
    resolves to two distinct canonical ids.
    """
    resolved_merchants = 0
    resolved_individuals = 0
    created_new_merchants = 0
    created_new_individuals = 0
    errors: list[dict[str, Any]] = []

    merchant_query = db.query(CanonicalEvent).filter(
        CanonicalEvent.source_merchant_id.isnot(None),
        CanonicalEvent.merchant_id.is_(None),
    )
    if tenant_bank_id:
        merchant_query = merchant_query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)

    for event in merchant_query.all():
        try:
            merchant, created = _get_or_create_merchant(db, event)
            event.merchant_id = merchant.merchant_id
            resolved_merchants += 1
            if created:
                created_new_merchants += 1

            # Mutual exclusivity: a row must never carry both ids. If the
            # source somehow gave us both, merchant wins and individual_id
            # is forced null -- logged here since resolve_parties runs
            # independently of any single ingestion_log entry.
            if event.source_individual_id:
                event.individual_id = None
                errors.append({
                    "type": "mutual_exclusivity_warning",
                    "tenant_bank_id": event.tenant_bank_id,
                    "rail_type": event.rail_type,
                    "transaction_id": event.transaction_id,
                    "detail": (
                        "Both source_merchant_id and source_individual_id were populated; "
                        "resolved as merchant, individual_id forced null."
                    ),
                })
        except Exception as exc:
            errors.append({
                "type": "row_error",
                "tenant_bank_id": event.tenant_bank_id,
                "rail_type": event.rail_type,
                "transaction_id": event.transaction_id,
                "error": str(exc),
            })

    # autoflush is off (see database.py), so the merchant_id updates above
    # are only visible in-memory until flushed -- without this, the
    # merchant_id IS NULL filter below would still match a row the
    # mutual-exclusivity branch just resolved as a merchant.
    db.flush()

    # merchant_id IS NULL guard excludes rows the merchant pass above just
    # resolved (mutual-exclusivity case), so they don't also get resolved
    # as an individual.
    individual_query = db.query(CanonicalEvent).filter(
        CanonicalEvent.source_individual_id.isnot(None),
        CanonicalEvent.individual_id.is_(None),
        CanonicalEvent.merchant_id.is_(None),
    )
    if tenant_bank_id:
        individual_query = individual_query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)

    for event in individual_query.all():
        try:
            individual, created = _get_or_create_individual(db, event)
            event.individual_id = individual.individual_id
            resolved_individuals += 1
            if created:
                created_new_individuals += 1
        except Exception as exc:
            errors.append({
                "type": "row_error",
                "tenant_bank_id": event.tenant_bank_id,
                "rail_type": event.rail_type,
                "transaction_id": event.transaction_id,
                "error": str(exc),
            })

    skipped_query = db.query(CanonicalEvent).filter(
        CanonicalEvent.source_merchant_id.is_(None),
        CanonicalEvent.source_individual_id.is_(None),
    )
    if tenant_bank_id:
        skipped_query = skipped_query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)
    skipped_already_resolved = skipped_query.count()

    db.commit()

    return {
        "resolved_merchants": resolved_merchants,
        "resolved_individuals": resolved_individuals,
        "created_new_merchants": created_new_merchants,
        "created_new_individuals": created_new_individuals,
        "skipped_already_resolved": skipped_already_resolved,
        "errors": errors,
    }
