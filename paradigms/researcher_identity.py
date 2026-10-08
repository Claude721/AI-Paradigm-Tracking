"""Conservative identity-preserving joins for public researcher profiles."""

from __future__ import annotations

import copy

from .models import ResearcherProfile


_IDENTITY_KEYS = {"orcid", "openalex", "semantic_scholar"}


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
        for field in (
            "role", "current_affiliation", "research_trajectory",
            "background_summary", "public_bio_excerpt", "key_person_reason",
        ):
            if not getattr(match, field):
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
