# R3/R6: pairing validity, governance-record currency, and the fallback decision.
# Input: {use_case_id, data_classes, region, now, exception_model?}
# Signature, health (circuit breaker) and capability checks are done in the gateway, because
# they need key material and live state; this policy covers the governance conditions.
package gov.route

import rego.v1

snapshot := data.snapshot

now_ns := time.parse_rfc3339_ns(input.now)

is_expired(ts) := true if {
	time.parse_rfc3339_ns(ts) < now_ns
} else := false

gov_expired(m) := true if {
	g := m.governance_record
	some ts in [g.agreement.expires_at, g.data_processing_terms.expires_at, g.third_party_risk_assessment.valid_until]
	is_expired(ts)
} else := false

model_dc_bad(m) := true if {
	some d in input.data_classes
	not d in m.governance_record.approved_data_classes
} else := false

pairing_dc_bad(e) := true if {
	some d in input.data_classes
	not d in e.data_classes
} else := false

model_region_bad(m) := true if {
	not input.region in m.governance_record.approved_regions
} else := false

pairing_region_bad(e) := true if {
	not input.region in e.regions
} else := false

entry_reasons(e, m) := {r |
	some check in [
		[is_expired(e.expires_at), "pairing_expired"],
		[gov_expired(m), "governance_record_expired"],
		[model_dc_bad(m), "data_class_not_approved"],
		[pairing_dc_bad(e), "data_class_not_approved"],
		[model_region_bad(m), "region_not_approved"],
		[pairing_region_bad(e), "region_not_approved"],
	]
	check[0] == true
	r := check[1]
}

candidates := [c |
	some e in snapshot.pairings[input.use_case_id]
	m := snapshot.models[e.model]
	rs := entry_reasons(e, m)
	c := {
		"model": e.model,
		"rank": e.rank,
		"eligible": count(rs) == 0,
		"reasons": sort(rs),
		"record_version": m.governance_record.version,
	}
]

default fallback := "hold"

fallback := "degrade" if snapshot.use_cases[input.use_case_id].interaction == "interactive"

# One-time exception from the approval service: the named model must be in the catalog,
# have a current governance record, and be approved for the request's data classes and region.
default exception := null

exception := {"model": input.exception_model, "eligible": count(rs) == 0, "reasons": sort(rs)} if {
	input.exception_model
	m := snapshot.models[input.exception_model]
	rs := {r |
		some check in [
			[gov_expired(m), "governance_record_expired"],
			[model_dc_bad(m), "data_class_not_approved"],
			[model_region_bad(m), "region_not_approved"],
		]
		check[0] == true
		r := check[1]
	}
}

exception := {"model": input.exception_model, "eligible": false, "reasons": ["model_not_in_catalog"]} if {
	input.exception_model
	not snapshot.models[input.exception_model]
}

decision := {"candidates": candidates, "fallback": fallback, "exception": exception}
