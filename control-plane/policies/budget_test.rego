package gov.budget_test

import rego.v1

import data.gov.budget

test_all_open_allows if {
	d := budget.decision with input as {"limits": [{"name": "money:UC-01", "fail_mode": "open"}, {"name": "money:org", "fail_mode": "open"}]}
	d.allow
	d.mode == "fail_open"
}

test_any_closed_refuses if {
	d := budget.decision with input as {"limits": [{"name": "money:UC-17", "fail_mode": "closed"}, {"name": "money:org", "fail_mode": "open"}]}
	not d.allow
	d.mode == "fail_closed"
	d.closed_limits == ["money:UC-17"]
}
