"""Conservative review labels; never certify physical relief-vehicle passage."""
from __future__ import annotations

# hgv is reviewed separately: motorcar permission does not grant truck permission.
RESTRICTED = {"no", "private", "permit", "customers", "agricultural", "forestry"}
PURPOSE = {"destination", "delivery", "permissive"}
LIMITS = ("maxweight", "maxaxleload", "maxheight", "maxwidth", "width", "maxwidth:physical")


def vehicle_policy(tags, node_tags=()):
    reasons = []
    controlling = next((k for k in ("motor_vehicle", "vehicle", "access") if k in tags), None)
    value = tags.get(controlling)
    prohibited = value in RESTRICTED or tags.get("motorcar") in RESTRICTED or tags.get("hgv") in RESTRICTED
    if prohibited:
        reasons.append("explicit_vehicle_or_permission_restriction")
    if value in PURPOSE or any(tags.get(k) in PURPOSE for k in ("motorcar", "hgv")):
        reasons.append("purpose_or_revocable_permission_requires_confirmation")
    # Both access hierarchy and unresolved alternatives remain visible in the source JSON.
    if any(":conditional" in k for k in tags):
        reasons.append("conditional_rule_or_exception_not_resolved")
    if any(k.startswith("oneway:") for k in tags):
        reasons.append("vehicle_specific_direction_rule_not_resolved")
    if any(k in tags for k in LIMITS):
        reasons.append("vehicle_dimensions_and_load_not_compared")
    if tags.get("surface") or tags.get("smoothness") or tags.get("tracktype"):
        reasons.append("surface_condition_not_field_verified")
    if tags.get("bridge") not in (None, "no"):
        reasons.append("bridge_capacity_not_confirmed")
    if any(t.get("barrier") not in (None, "no") or t.get("locked") == "yes"
           or any(t.get(k) in RESTRICTED for k in ("access", "vehicle", "motor_vehicle", "hgv"))
           for t in node_tags):
        reasons.append("source_node_barrier_or_access_restriction")
    if tags.get("highway") in {"track", "footway", "steps", "cycleway", "path", "pedestrian", "bridleway"}:
        prohibited = True
        reasons.append("separate_track_scenario_or_non_motor_highway")
    if tags.get("smoothness") == "impassable":
        prohibited = True
        reasons.append("explicit_impassable_condition")
    reasons.append("physical_access_and_relief_vehicle_suitability_unverified")
    return dict(candidate_included=not prohibited,
        status="excluded_pending_permission_or_exception_review" if prohibited else "candidate_requires_review",
        controlling_access_key=controlling, controlling_access_value=value,
        review_required=True, review_reasons=reasons, vehicle_passage_confirmed=False)
