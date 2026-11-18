package gov.route_test

import rego.v1

import data.gov.route

current := {
	"version": "GR-OK",
	"agreement": {"expires_at": "2030-01-01T00:00:00Z"},
	"data_processing_terms": {"expires_at": "2030-01-01T00:00:00Z"},
	"third_party_risk_assessment": {"valid_until": "2030-01-01T00:00:00Z"},
	"approved_data_classes": ["internal", "confidential"],
	"approved_regions": ["eu-west"],
}

expired_agreement := object.union(current, {"version": "GR-EXP", "agreement": {"expires_at": "2026-06-30T23:59:59Z"}})

entry(model, rank) := {"model": model, "rank": rank, "data_classes": ["internal", "confidential"],
	"regions": ["eu-west"], "expires_at": "2030-12-31T23:59:59Z"}

snap := {
	"use_cases": {"UC-40": {"interaction": "batch"}, "UC-22": {"interaction": "interactive"}},
	"models": {
		"model-a": {"governance_record": current},
		"model-c": {"governance_record": current},
		"model-d": {"governance_record": expired_agreement},
	},
	"pairings": {
		"UC-40": [entry("model-d", 1), entry("model-a", 2)],
		"UC-22": [entry("model-a", 1)],
	},
}

inp(uc) := {"use_case_id": uc, "data_classes": ["internal"], "region": "eu-west", "now": "2026-11-18T00:00:00Z"}

test_expired_agreement_ineligible if {
	d := route.decision with input as inp("UC-40") with data.snapshot as snap
	some c in d.candidates
	c.model == "model-d"
	not c.eligible
	c.reasons == ["governance_record_expired"]
	some a in d.candidates
	a.model == "model-a"
	a.eligible
}

test_region_not_approved if {
	d := route.decision with input as object.union(inp("UC-22"), {"region": "us-east"}) with data.snapshot as snap
	some c in d.candidates
	c.reasons == ["region_not_approved"]
}

test_data_class_not_approved if {
	d := route.decision with input as object.union(inp("UC-22"), {"data_classes": ["restricted"]}) with data.snapshot as snap
	some c in d.candidates
	c.reasons == ["data_class_not_approved"]
}

test_pairing_expired if {
	s := object.union(snap, {"pairings": {"UC-22": [object.union(entry("model-a", 1), {"expires_at": "2026-01-01T00:00:00Z"})]}})
	d := route.decision with input as inp("UC-22") with data.snapshot as s
	d.candidates[0].reasons == ["pairing_expired"]
}

test_fallback_by_interaction if {
	route.fallback == "hold" with input as inp("UC-40") with data.snapshot as snap
	route.fallback == "degrade" with input as inp("UC-22") with data.snapshot as snap
}

test_exception_requires_current_record if {
	ok := route.decision with input as object.union(inp("UC-40"), {"exception_model": "model-c"}) with data.snapshot as snap
	ok.exception.eligible
	bad := route.decision with input as object.union(inp("UC-40"), {"exception_model": "model-d"}) with data.snapshot as snap
	not bad.exception.eligible
	unknown := route.decision with input as object.union(inp("UC-40"), {"exception_model": "model-x"}) with data.snapshot as snap
	unknown.exception.reasons == ["model_not_in_catalog"]
}
