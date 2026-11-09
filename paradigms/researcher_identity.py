"""Conservative identity-preserving joins for public researcher profiles."""

from __future__ import annotations

import copy
import unicodedata
from difflib import SequenceMatcher

from .models import ResearcherProfile


_IDENTITY_KEYS = {"orcid", "openalex", "semantic_scholar"}
IDENTITY_POLICY = "professional-links-v1"


def normalized_person_name(value: str) -> str:
    """Preserve every writing system; two empty names must never match."""
    return "".join(c for c in unicodedata.normalize("NFKC", str(value)).casefold() if c.isalnum())


def person_name_similarity(left: str, right: str) -> float:
    a, b = normalized_person_name(left), normalized_person_name(right)
    if not a or not b:
        return 0.0
    # Fuzzy matching short/non-Latin names is unsafe for byline attachment.
    if a == b:
        return 1.0
    if min(len(a), len(b)) < 6 or not (a.isascii() and b.isascii()):
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def bind_current_work(profile, evidence):
    from .evidence_association import support_revision
    binding = {"source_fingerprint": evidence.fingerprint,
               "source_revision": support_revision(evidence), "source_url": evidence.url}
    profile.identity_policy_version = IDENTITY_POLICY
    if binding not in profile.identity_bindings:
        profile.identity_bindings.append(binding)


def profile_bound_to_work(profile, evidence):
    from .evidence_association import support_revision
    return any(binding.get("source_fingerprint") == evidence.fingerprint
               and binding.get("source_revision") == support_revision(evidence)
               and binding.get("source_url") == evidence.url for binding in profile.identity_bindings)


def record_identity_observation(profile, provider, status, *, url="", reason="", **details):
    """Metadata only. Keep a failed provider even when another one succeeds."""
    from runtime_clock import observation_now
    row = {"provider": provider, "status": status, "url": url, "reason": reason,
           "policy_version": IDENTITY_POLICY, "observed_at": observation_now().isoformat(), **details}
    if row not in profile.identity_observations:
        profile.identity_observations.append(row)


def verify_contact(profile, label, target, source_url, relation):
    from .models import safe_public_contact_target
    from sources.professional_identity import public_url, profile_label
    if (not safe_public_contact_target(label, target) or not public_url(source_url)
            or (label != "email" and (not public_url(target) or profile_label(target, label) != label))):
        return False
    if label in _IDENTITY_KEYS and label in profile.contact_provenance:
        previous = profile.contact_provenance[label].get("target", "")
        if _claim_value(label, previous) != _claim_value(label, target):
            record_identity_observation(profile, "identity_link", "unverified", url=target,
                                        reason="ConflictingAcademicIdentity", source_url=source_url)
            return False
    if label == "email":
        profile.public_email, profile.public_email_source = target, source_url
    else:
        # An alternative remains a lead until it can replace the same fact.
        current = profile.profile_urls.get(label)
        if current and current != target and label in profile.contact_provenance:
            return False
        profile.profile_urls[label] = target
    profile.contact_provenance[label] = {"target": target, "source_url": source_url, "relation": relation,
                                         "policy_version": IDENTITY_POLICY}
    if label == "orcid":
        profile.identifiers["orcid"] = target.rstrip("/").rsplit("/", 1)[-1]
    return True


def _claim_value(label: str, value: str) -> str:
    normalized = str(value).strip().rstrip("/").casefold()
    if label in _IDENTITY_KEYS:
        return normalized.rsplit("/", 1)[-1]
    return normalized


def _claims(profile: ResearcherProfile) -> dict[str, str]:
    claims = {
        label: _claim_value(label, value)
        for label, value in profile.identifiers.items()
        if label in _IDENTITY_KEYS and str(value).strip()
    }
    for label, value in profile.profile_urls.items():
        if label in _IDENTITY_KEYS and str(value).strip():
            claims.setdefault(label, _claim_value(label, value))
    if profile.identity_policy_version:
        # Raw author-ID mappings are lookup seeds, not proof that a historical
        # namesake may lend this work their identity and contacts.
        contacts = profile.public_contacts
        claims = {label: value for label, value in claims.items()
                  if label in contacts and _claim_value(label, contacts[label]) == value}
    return claims


def same_verified_researcher(
    left: ResearcherProfile, right: ResearcherProfile
) -> bool:
    """A matching name or institution is never an identity proof by itself."""

    left_claims, right_claims = _claims(left), _claims(right)
    shared = left_claims.keys() & right_claims.keys()
    if any(left_claims[key] != right_claims[key] for key in shared):
        return False
    # A lab mailbox, GitHub organization, matching name or affiliation may
    # corroborate a profile later, but none is a stable person ID by itself.
    return bool(shared)


def merge_researcher_profiles(
    existing: list[ResearcherProfile], incoming: list[ResearcherProfile]
) -> list[ResearcherProfile]:
    """Preserve distinct same-name people; fill gaps only for verified matches."""

    merged = [copy.deepcopy(profile) for profile in existing]
    for profile in incoming:
        match = next(
            (item for item in merged if same_verified_researcher(item, profile)),
            None,
        )
        if match is None:
            merged.append(copy.deepcopy(profile))
            continue
        old_contacts = match.public_contacts.copy() if not match.identity_policy_version else {}
        old_email_source = match.public_email_source
        if profile.identity_policy_version and not match.identity_policy_version and match.current_affiliation:
            match.affiliation_observations.append({"organization": match.current_affiliation,
                "kind": "historical_profile_unknown_date", "source_url": old_email_source})
            match.current_affiliation = profile.current_affiliation
        for field in (
            "role", "current_affiliation", "research_trajectory",
            "background_summary", "public_bio_excerpt", "key_person_reason",
            "identity_search_status", "contact_search_status", "attribution_analysis_status",
        ):
            if not getattr(match, field):
                if field == "current_affiliation" and match.identity_policy_version and not profile.identity_policy_version:
                    if profile.current_affiliation:
                        fact = {"organization": profile.current_affiliation, "kind": "historical_profile_unknown_date", "source_url": profile.public_email_source}
                        if fact not in match.affiliation_observations:
                            match.affiliation_observations.append(fact)
                    continue
                setattr(match, field, getattr(profile, field))
        if not match.public_email and profile.public_email_source:
            match.public_email = profile.public_email
            match.public_email_source = profile.public_email_source
        match.trajectory_consistency = max(
            match.trajectory_consistency, profile.trajectory_consistency
        )
        for label, value in profile.identifiers.items():
            match.identifiers.setdefault(label, value)
        for label, value in profile.profile_urls.items():
            match.profile_urls.setdefault(label, value)
        if match.identity_policy_version and not profile.identity_policy_version:
            # A verified shared academic ID may retain already-saved contacts.
            # Record that they are historical, never pretend to have queried
            # their source in this run. Unmatched legacy people stay separate.
            for label, value in profile.public_contacts.items():
                target = match.public_email if label == "email" else match.profile_urls.get(label)
                source = profile.public_email_source if label == "email" else value
                if target == value:
                    match.contact_provenance.setdefault(label, {"target": value, "source_url": source,
                        "relation": "historical_identity", "policy_version": match.identity_policy_version})
        for label, claim in profile.contact_provenance.items():
            target = match.public_email if label == "email" else match.profile_urls.get(label)
            if target == claim.get("target"):
                match.contact_provenance.setdefault(label, copy.deepcopy(claim))
        if profile.identity_policy_version:
            match.identity_policy_version = profile.identity_policy_version
            for label, value in old_contacts.items():
                target = match.public_email if label == "email" else match.profile_urls.get(label)
                if target == value:
                    match.contact_provenance.setdefault(label, {"target": value,
                        "source_url": old_email_source if label == "email" else value,
                        "relation": "historical_identity", "policy_version": match.identity_policy_version})
        for field in ("identity_bindings", "identity_observations", "profile_leads", "affiliation_observations"):
            target = getattr(match, field)
            target.extend(copy.deepcopy(value) for value in getattr(profile, field) if value not in target)
        for field in ("prior_affiliations", "contact_search_notes"):
            target = getattr(match, field)
            target.extend(value for value in getattr(profile, field) if value not in target)
        known_works = {
            (str(work.get("title", "")), str(work.get("url", "")))
            for work in match.representative_works
        }
        for work in profile.representative_works:
            identity = (str(work.get("title", "")), str(work.get("url", "")))
            if identity not in known_works:
                match.representative_works.append(copy.deepcopy(work))
                known_works.add(identity)
    return merged
