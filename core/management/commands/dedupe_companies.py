from __future__ import annotations

import os
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Company, CompanyRecruiter, JobPosting, JobRecruiterTarget
from core.services.normalization_service import canonical_compact_text
from core.utils import safe_str


def _pick_keep_company(companies: list[Company]) -> Company:
    # Prefer: readable non-noise name first, then identifiers, then most jobs, then lowest id.
    scored = []
    for c in companies:
        jobs = JobPosting.objects.filter(company_ref=c).count()
        name = safe_str(getattr(c, "normalized_name", "")).lower()
        score = (
            1 if "linkedin" not in name else 0,
            1 if " " in name else 0,
            1 if safe_str(c.active_domain) else 0,
            1 if safe_str(c.linkedin_company_slug) else 0,
            jobs,
            -int(c.id),
        )
        scored.append((score, c))
    scored.sort(reverse=True, key=lambda x: x[0])
    return scored[0][1]


@transaction.atomic
def _merge_company_into(target: Company, source: Company) -> dict:
    """
    Merge source Company into target Company by re-pointing FKs.
    Handles recruiter conflicts by merging on normalized_person_name.
    """
    stats = {"source_id": source.id, "target_id": target.id, "jobs_moved": 0, "recruiters_moved": 0, "targets_moved": 0, "recruiters_merged": 0, "targets_dropped_conflict": 0}

    # Move jobs
    stats["jobs_moved"] = JobPosting.objects.filter(company_ref=source).update(company_ref=target)

    # Move/merge recruiters
    source_recruiters = list(CompanyRecruiter.objects.filter(company=source).order_by("id"))
    for recruiter in source_recruiters:
        existing = CompanyRecruiter.objects.filter(company=target, normalized_person_name=recruiter.normalized_person_name).order_by("id").first()
        if not existing:
            recruiter.company = target
            recruiter.save(update_fields=["company", "updated_at"])
            stats["recruiters_moved"] += 1
            continue

        # Merge recruiter data into existing (prefer real email)
        existing_email = safe_str(existing.email).strip().lower()
        incoming_email = safe_str(recruiter.email).strip().lower()
        if (not existing_email or existing_email == "none") and incoming_email and incoming_email != "none":
            existing.email = incoming_email

        existing.email_sent = bool(existing.email_sent or recruiter.email_sent)
        if recruiter.email_sent_date and (not existing.email_sent_date or recruiter.email_sent_date > existing.email_sent_date):
            existing.email_sent_date = recruiter.email_sent_date
        existing.is_active = bool(existing.is_active or recruiter.is_active)

        existing.save()
        stats["recruiters_merged"] += 1

        # Move targets from recruiter -> existing recruiter, dropping conflicts
        targets = list(JobRecruiterTarget.objects.filter(company_recruiter=recruiter).order_by("id"))
        for t in targets:
            conflict = JobRecruiterTarget.objects.filter(job_posting=t.job_posting, company_recruiter=existing).exists()
            if conflict:
                # Keep the existing one; drop the duplicate target row.
                t.delete()
                stats["targets_dropped_conflict"] += 1
                continue
            t.company_recruiter = existing
            t.save(update_fields=["company_recruiter", "updated_at"])
            stats["targets_moved"] += 1

        recruiter.delete()

    # Prefer filling in missing target fields
    if safe_str(source.active_domain) and not safe_str(target.active_domain):
        target.active_domain = source.active_domain
    if safe_str(source.linkedin_company_slug) and not safe_str(target.linkedin_company_slug):
        target.linkedin_company_slug = source.linkedin_company_slug
    if safe_str(source.raw_name_latest) and not safe_str(target.raw_name_latest):
        target.raw_name_latest = source.raw_name_latest
    target.save()

    source.delete()
    return stats


class Command(BaseCommand):
    help = "Deduplicate Company rows by LinkedIn slug/domain and optional fuzzy matching. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Apply merges (otherwise dry-run).")
        parser.add_argument("--include-fuzzy", action="store_true", help="Also propose fuzzy merges on normalized_name.")
        parser.add_argument("--threshold", type=int, default=90, help="Fuzzy match threshold (default 90).")

    def handle(self, *args, **options):
        apply = bool(options.get("apply"))
        include_fuzzy = bool(options.get("include_fuzzy"))
        threshold = int(options.get("threshold") or 90)

        groups: list[list[Company]] = []

        # 0) Group by canonical compact key (spacing/punctuation variants)
        compact_map = defaultdict(list)  # compact_key -> [company_id]
        for row in Company.objects.values("id", "normalized_name"):
            name = safe_str(row.get("normalized_name")).strip().lower()
            key = canonical_compact_text(name)
            if not key or len(key) < 3:
                continue
            compact_map[key].append(int(row["id"]))

        for ids in compact_map.values():
            if len(ids) <= 1:
                continue
            groups.append(list(Company.objects.filter(id__in=ids)))

        # 1) Group by linkedin_company_slug
        slug_map = defaultdict(list)
        for c in Company.objects.exclude(linkedin_company_slug="").exclude(linkedin_company_slug__isnull=True):
            slug_map[safe_str(c.linkedin_company_slug).strip().lower()].append(c)
        groups.extend([items for items in slug_map.values() if len(items) > 1])

        # 2) Group by active_domain
        domain_map = defaultdict(list)
        for c in Company.objects.exclude(active_domain="").exclude(active_domain__isnull=True):
            domain_map[safe_str(c.active_domain).strip().lower()].append(c)
        groups.extend([items for items in domain_map.values() if len(items) > 1])

        # 3) Optional fuzzy proposals (dry-run recommended). Requires rapidfuzz.
        fuzzy_pairs = []
        if include_fuzzy:
            try:
                from rapidfuzz import fuzz  # type: ignore
            except Exception:
                self.stdout.write(self.style.WARNING("rapidfuzz is not installed; skipping fuzzy merges."))
                fuzz = None

            if fuzz is not None:
                # Load all companies once to avoid N+1 DB queries.
                companies = list(Company.objects.all().values("id", "normalized_name"))
                buckets = defaultdict(list)  # first_char -> list[(id, name)]
                for row in companies:
                    name = safe_str(row.get("normalized_name")).strip().lower()
                    if not name:
                        continue
                    buckets[name[0]].append((int(row["id"]), name))

                def _score(a: str, b: str) -> int:
                    s1 = int(fuzz.token_sort_ratio(a, b))
                    s2 = int(fuzz.token_set_ratio(a, b))
                    return max(s1, s2)

                for _first_char, items in buckets.items():
                    # Pairwise within bucket (usually small enough).
                    items = sorted(items, key=lambda x: x[1])
                    for i in range(len(items)):
                        src_id, src_name = items[i]
                        best = None
                        best_score = 0
                        for j in range(i + 1, len(items)):
                            cand_id, cand_name = items[j]
                            score = _score(src_name, cand_name)
                            if score > best_score:
                                best_score = score
                                best = (cand_id, cand_name)
                        if not best or best_score < threshold:
                            continue

                        match_id, match_name = best

                        # Guard against over-merging extreme length differences unless acronym merge.
                        len_ratio = min(len(src_name), len(match_name)) / max(len(src_name), len(match_name), 1)
                        if len_ratio < 0.5:
                            continue

                        # Require meaningful overlap (avoid cs vs cps energy, etc.)
                        drop = {"inc", "llc", "ltd", "corp", "corporation", "company", "co", "linkedin"}
                        src_tokens = {t for t in src_name.split() if t not in drop}
                        match_tokens = {t for t in match_name.split() if t not in drop}
                        shared = len(src_tokens.intersection(match_tokens))
                        if shared < 2:
                            continue

                        # Require first token match
                        if src_tokens and match_tokens:
                            if (src_name.split()[0] if src_name.split() else "") != (match_name.split()[0] if match_name.split() else ""):
                                continue

                        fuzzy_pairs.append((src_id, match_id, best_score))

        seen_merge_keys = set()
        planned_merges = []

        for items in groups:
            ids = sorted([c.id for c in items])
            key = tuple(ids)
            if key in seen_merge_keys:
                continue
            seen_merge_keys.add(key)
            keep = _pick_keep_company(items)
            for src in items:
                if src.id == keep.id:
                    continue
                planned_merges.append((keep.id, src.id, "slug/domain"))

        for src_id, match_id, score in fuzzy_pairs:
            a = Company.objects.filter(id=match_id).first()
            b = Company.objects.filter(id=src_id).first()
            if not a or not b or a.id == b.id:
                continue

            keep = _pick_keep_company([a, b])
            drop = b if keep.id == a.id else a
            planned_merges.append((keep.id, drop.id, f"fuzzy({score})"))

        if not planned_merges:
            self.stdout.write(self.style.SUCCESS("No company dedupe merges found."))
            return

        self.stdout.write(self.style.WARNING(f"Planned merges: {len(planned_merges)} (apply={apply})"))
        for keep_id, src_id, reason in planned_merges[:100]:
            keep = Company.objects.filter(id=keep_id).first()
            src = Company.objects.filter(id=src_id).first()
            if not keep or not src:
                continue
            self.stdout.write(f"- {reason}: keep #{keep.id} {keep.normalized_name!r} <- drop #{src.id} {src.normalized_name!r}")

        if not apply:
            self.stdout.write(self.style.WARNING("Dry-run only. Re-run with --apply to perform merges."))
            return

        # Apply merges
        merged = 0
        for keep_id, src_id, reason in planned_merges:
            keep = Company.objects.filter(id=keep_id).first()
            src = Company.objects.filter(id=src_id).first()
            if not keep or not src:
                continue
            if keep.id == src.id:
                continue
            stats = _merge_company_into(keep, src)
            merged += 1
            self.stdout.write(f"MERGED {reason}: {stats}")

        self.stdout.write(self.style.SUCCESS(f"Done. merges_applied={merged}"))
