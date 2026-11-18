# R5: budget fail mode when the counter store is unreachable.
# Input: {limits: [{name, fail_mode}]}. Any limit with fail_mode "closed" refuses the request;
# if all are "open" the request proceeds and the gateway logs that it failed open.
package gov.budget

import rego.v1

closed_limits := sort([l.name | some l in input.limits; l.fail_mode == "closed"])

open_limits := sort([l.name | some l in input.limits; l.fail_mode != "closed"])

default allow := false

allow if count(closed_limits) == 0

default mode := "fail_closed"

mode := "fail_open" if allow

decision := {"allow": allow, "mode": mode, "closed_limits": closed_limits, "open_limits": open_limits}
