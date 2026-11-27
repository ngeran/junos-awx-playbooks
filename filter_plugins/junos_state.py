# Filter plugins used by step2_upgrade.yml to collect and compare switch state.
#
#   junos_state(results, names)  -> normalised state of one switch
#   junos_diff(before, after)    -> list of comparison rows
#
# Only text output of Junos CLI commands is parsed. Nothing here talks to
# a device; the playbooks pass in what `juniper.device.command` returned.
from __future__ import annotations

import re

from ansible.errors import AnsibleFilterError

_INTERFACE_PREFIX = r"^(ge|xe|et|fe|ae|em|vtep|fxp|me\d|irb|lo\d|vme|lt|pc|cbc)"
_ALARM_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+(.*\S)\s*$")
_ROUTE_LINE = re.compile(r"^[\s*?\-+]+\s*(\d{1,3}(?:\.\d{1,3}){3}/\d+|[0-9A-Fa-f:]+/\d+)(?:\s+(.*))?$")
_ROUTE_NEXT = re.compile(r"^[\s*?\-+]+\s*([A-Z])\s+\d+\b")
_BGP_PEER = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s*$")
_OSPF_NBR = re.compile(r"^\s*\d{1,3}(?:\.\d{1,3}){3}\s+\S+\s+\d+\s+(\w+)")
_LDP_NBR = re.compile(r"^\s*\d{1,3}(?:\.\d{1,3}){3}\s+\S+\s+(Oper|None)")
_RSVP_NBR = re.compile(r"^\s*\d{1,3}(?:\.\d{1,3}){3}\s+\S+\s+\S+\s+\S+")
_NOT_CONFIGURED_TEXT = re.compile(
    r"(not (configured|running|valid|needed by configuration|supported)|warning: .*not running|"
    r"^No .* (configured|found))", re.IGNORECASE)
_ENV_LINE = re.compile(r"^\s*(.*?)\s{2,}(OK|Failed|Absent|Offline|Online|Testing|Check|Error|Fault)(?:\s{2,}.*)?\s*$")
_CORE_DUMP = re.compile(r"\.(core|tgz|tar\.gz|txt)\s|/var/.*(?:core|jkernel|jrouter)")
_NTP_ASSOC = re.compile(r"^\s*([*^~\s])([\*\-]?)\s*(\d{1,3}(?:\.\d{1,3}){3})\s+\S+\s+(\S+)\s+(\d+)\s+")
_OSPF_IF = re.compile(r"^\s*(\S+\.\d+)\s+(\S+)\s+(\d{1,3}(?:\.\d{1,3}){3})\s+\S+\s+\S+\s+(\d+)\s*$")
_LDP_LABEL = re.compile(r"^\s*\d+\s+[0-9A-Fa-f:.]+/\d+")
_RSVP_ROW = re.compile(r"^\s*\d{1,3}(?:\.\d{1,3}){3}(?:/\d+)?\s+\S+\s+.*\b(Up|Down)\b")
_LLDP_NBR = re.compile(r"^\s*(\S+)\s+(\S+)\s+\S+\s+\S+\s+(\S.*?)\s*$")
_LACP_ROW = re.compile(r"^\s*(\S+)\s+(\d+)\s+(Success|Failure)")


def _stdout(result) -> str:
    return (result or {}).get("stdout") or ""


def _failed(result) -> bool:
    return bool((result or {}).get("failed"))


def _lines(result) -> list[str]:
    return [line.rstrip() for line in _stdout(result).splitlines()]


def _is_configured(result) -> bool:
    """A `... | display set` command prints nothing when the stanza is absent."""
    return bool(_stdout(result).strip()) and not _failed(result)


def _is_absent(result) -> bool:
    """True when the command shows nothing, failed, or says the feature is off."""
    text = _stdout(result).strip()
    return _failed(result) or not text or bool(_NOT_CONFIGURED_TEXT.search(text))


def _absent_text(result) -> str:
    """Short reason for an absent feature, taken from the device."""
    text = " ".join(_stdout(result).split()) or ((result or {}).get("msg") or "")
    match = re.search(r"message:\s*(.*?)\)?\s*$", text)
    if match:
        return match.group(1)
    for needle, short in (("not running", "not running"), ("not configured", "not configured"),
                          ("not needed", "not configured")):
        if needle in text.lower():
            return short
    return text[:60]


def _count_matches(lines, pattern):
    return sum(1 for line in lines if pattern.search(line))


def _parse_interfaces(lines) -> dict:
    """Physical and logical interfaces without a unit number: name -> admin/link."""
    state = {}
    for line in lines:
        parts = line.split()
        if len(parts) >= 3 and "." not in parts[0] and re.match(_INTERFACE_PREFIX, parts[0]):
            state[parts[0]] = "%s/%s" % (parts[1], parts[2])
    return state


def _parse_descriptions(lines) -> dict:
    state = {}
    for line in lines:
        if not line.strip() or line.startswith("Interface "):
            continue
        parts = line.split(None, 3)
        if len(parts) >= 3:
            state[parts[0]] = parts[3].strip() if len(parts) == 4 else ""
    return state


def _parse_alarms(lines) -> list:
    # The timestamp is dropped on purpose: it always changes after a reboot.
    alarms = set()
    for line in lines:
        match = _ALARM_LINE.match(line)
        if match:
            parts = line.split(None, 4)
            alarms.add("%s: %s" % (parts[3], parts[4].strip()))
    return sorted(alarms)


def _parse_routes(lines) -> list:
    """'table prefix protocol' strings. Handles a prefix that wraps onto its own line."""
    routes = set()
    table = None
    pending = None
    for line in lines:
        header = re.match(r"^((?:[A-Za-z0-9_.\-]+\.)*inet(?:6)?\.0):", line)
        if header:
            table = header.group(1)
            pending = None
            continue
        if line.startswith(("+ =", "A V ", "Limit/Threshold")):
            continue
        match = _ROUTE_LINE.match(line)
        if match:
            pending = match.group(1)
            protocol = re.match(r"\s*([A-Z])\b", match.group(2) or "")
            if protocol and table:
                routes.add("%s %s %s" % (table, pending, protocol.group(1)))
                pending = None
            continue
        nxt = _ROUTE_NEXT.match(line)
        if nxt and pending and table:
            routes.add("%s %s %s" % (table, pending, nxt.group(1)))
            pending = None
    return sorted(routes)


def _protocol_state(name, config_result, command_result) -> dict:
    if not _is_configured(config_result):
        return {"configured": False, "text": "not configured"}
    lines = _lines(command_result)
    text = _stdout(command_result)
    if _failed(command_result) or _NOT_CONFIGURED_TEXT.search(text):
        reason = ((command_result or {}).get("msg") or "").strip()
        reason = re.sub(r"^Unable to execute the command:.*?Error:\s*", "", reason)
        message = re.search(r"message:\s*(.*?)\)?\s*$", reason)
        if message:
            reason = message.group(1)
        return {"configured": True, "available": False,
                "text": "configured, no live state", "reason": reason[:120]}

    if name == "bgp":
        peers = [_BGP_PEER.match(line) for line in lines]
        peers = [p for p in peers if p]
        up = sum(1 for p in peers if p.group(8).startswith("Est"))
        return {"configured": True, "available": True, "count": up, "total": len(peers),
                "text": "%d of %d peers Established" % (up, len(peers))}
    if name == "ospf":
        states = [m.group(1) for m in (_OSPF_NBR.search(line) for line in lines) if m]
        full = sum(1 for state in states if state.lower() == "full")
        return {"configured": True, "available": True, "count": full, "total": len(states),
                "text": "%d of %d neighbors Full" % (full, len(states))}
    if name == "ldp":
        oper = _count_matches(lines, _LDP_NBR)
        return {"configured": True, "available": True, "count": oper, "total": _count_matches(lines, _RSVP_NBR) or oper,
                "text": "%d neighbors Operational" % oper}
    if name == "rsvp":
        count = _count_matches(lines, _RSVP_NBR)
        return {"configured": True, "available": True, "count": count, "text": "%d neighbors" % count}
    if name == "mpls":
        # There is no single stable text layout for LSPs across Junos versions,
        # so report the number of non-header lines and keep the raw text.
        body = [line for line in lines if line.strip() and not re.search(r"(?i)^(Ingress|Egress|Transit|Total)\b", line)]
        return {"configured": True, "available": True, "count": len(body),
                "text": "%d LSP lines (see note)" % len(body), "raw": "\n".join(lines[:15])}
    return {"configured": True, "available": True, "text": "unknown protocol %s" % name}


def _parse_environment(lines) -> dict:
    sensors = {}
    for line in lines:
        match = _ENV_LINE.match(line)
        if match and not line.startswith("Class"):
            sensors[match.group(1).strip()] = match.group(2)
    return sensors


def _parse_ntp(lines) -> dict:
    peers = {}
    for line in lines:
        match = _NTP_ASSOC.match(line)
        if match:
            synced = match.group(1).strip() in ("*", "^") or match.group(2) == "*"
            peers[match.group(3)] = "synced" if synced else "st%s" % match.group(5)
    return peers


def _parse_ospf_interfaces(lines) -> dict:
    interfaces = {}
    for line in lines:
        match = _OSPF_IF.match(line)
        if match:
            interfaces[match.group(1)] = "%s nbrs=%s" % (match.group(2), match.group(5))
    return interfaces


def _parse_lldp(lines) -> dict:
    neighbors = {}
    for line in lines:
        match = _LLDP_NBR.match(line)
        if match and not line.startswith("Local"):
            neighbors["%s -> %s" % (match.group(1), match.group(3))] = True
    return sorted(neighbors)


def _parse_re(lines) -> dict:
    """Routing Engine facts that are not expected to change across a reboot."""
    keep = {}
    for line in lines:
        stripped = line.strip()
        for label in ("Current state", "Mastership", "Model", "DRAM"):
            if stripped.startswith(label):
                keep[label] = stripped[len(label):].strip()
    return keep


def junos_state(results, names) -> dict:
    """Build the normalised state of one switch.

    results: the `results` list returned by juniper.device.command
    names:   the CLI commands, in the same order
    """
    if not isinstance(results, list) or not isinstance(names, list) or len(results) != len(names):
        raise AnsibleFilterError("junos_state needs equally sized lists of results and names")

    by_name = {}
    for name, result in zip(names, results):
        key = name.replace("show ", "", 1).replace(" | display set", "")
        by_name[key] = result

    def result(command):
        return by_name.get(command, {})

    protocols = {}
    for protocol in ("bgp", "ospf", "ldp", "rsvp", "mpls"):
        summary_command = {"bgp": "bgp summary", "ospf": "ospf neighbor", "ldp": "ldp neighbor",
                           "rsvp": "rsvp neighbor", "mpls": "mpls lsp"}[protocol]
        protocols[protocol] = _protocol_state(protocol, result("configuration protocols %s" % protocol),
                                              result(summary_command))

    r = result
    ntp_lines = _lines(r("ntp associations"))
    env = _parse_environment(_lines(r("chassis environment")))
    route_config = _stdout(r("route summary"))
    route_header = {}
    for line in route_config.splitlines():
        for label in ("Autonomous system number", "Router ID"):
            if line.strip().startswith(label):
                route_header[label] = line.strip().split(":", 1)[-1].strip()

    def count(command, pattern):
        return _count_matches(_lines(r(command)), pattern)

    def configured(stanza):
        return _is_configured(r("configuration %s" % stanza))

    routes = _parse_routes(_lines(r("route terse")))

    extra = {
        "chassis_alarms": _parse_alarms(_lines(r("chassis alarms"))),
        "environment": env,
        "environment_absent": _is_absent(r("chassis environment")),
        "core_dumps": count("system core-dumps", _CORE_DUMP),
        "ntp": _parse_ntp(ntp_lines),
        "ntp_configured": configured("system ntp") or bool(_parse_ntp(ntp_lines)),
        "ospf_interfaces": _parse_ospf_interfaces(_lines(r("ospf interface"))),
        "ldp_database": None if _is_absent(r("ldp database")) else count("ldp database", _LDP_LABEL),
        "ldp_database_text": "" if not _is_absent(r("ldp database")) else _absent_text(r("ldp database")),
        "rsvp_interfaces": None if _is_absent(r("rsvp interface")) else count("rsvp interface", _RSVP_ROW),
        "rsvp_sessions": None if _is_absent(r("rsvp session")) else count("rsvp session", _RSVP_ROW),
        "rsvp_text": _absent_text(r("rsvp interface")) if _is_absent(r("rsvp interface")) else "",
        "route_as": route_header.get("Autonomous system number", ""),
        "router_id": route_header.get("Router ID", ""),
        "lldp": _parse_lldp(_lines(r("lldp neighbors"))),
        "lldp_configured": configured("protocols lldp"),
        "lacp": None if _is_absent(r("lacp interfaces")) else count("lacp interfaces", _LACP_ROW),
        "lacp_text": _absent_text(r("lacp interfaces")) if _is_absent(r("lacp interfaces")) else "",
        "routing_engine": _parse_re(_lines(r("chassis routing-engine"))),
    }

    return {
        "interfaces": _parse_interfaces(_lines(result("interfaces terse"))),
        "descriptions": _parse_descriptions(_lines(result("interfaces descriptions"))),
        "alarms": _parse_alarms(_lines(result("system alarms"))),
        "routes": routes,
        "protocols": protocols,
        "config": _stdout(result("configuration")).strip(),
        **extra,
    }

def _absent_row(label, text, rows):
    rows.append(_row(label, text or "not configured", text or "not configured", "skipped", ""))


def _changed_map(before: dict, after: dict) -> list:
    return sorted("%s: %s -> %s" % (key, before.get(key, "absent"), after.get(key, "absent"))
                  for key in set(before) | set(after) if before.get(key) != after.get(key))


def _row(check, before, after, result="same", note=""):
    return {"check": check, "before": before, "after": after, "result": result, "note": note}


def junos_diff(before, after) -> list:
    """Compare two states produced by junos_state. Returns rows for a table."""
    rows = []

    b_int, a_int = before["interfaces"], after["interfaces"]
    changed = _changed_map(b_int, a_int)
    rows.append(_row("Interfaces (admin/link)", "%d interfaces" % len(b_int), "%d interfaces" % len(a_int),
                     "same" if not changed else "CHANGED", "; ".join(changed)))

    b_desc, a_desc = before["descriptions"], after["descriptions"]
    changed = _changed_map(b_desc, a_desc)
    rows.append(_row("Interface descriptions", "%d" % len(b_desc), "%d" % len(a_desc),
                     "same" if not changed else "CHANGED", "; ".join(changed)))

    b_alm, a_alm = before["alarms"], after["alarms"]
    new = sorted(set(a_alm) - set(b_alm))
    cleared = sorted(set(b_alm) - set(a_alm))
    changed = bool(new or cleared)
    result = "NEW ALARMS" if new else ("alarms cleared" if cleared else "same")
    note = "; ".join(["new: " + x for x in new] + ["cleared: " + x for x in cleared])
    if not changed and a_alm:
        note = "after: " + "; ".join(a_alm)
    rows.append(_row("Active alarms", "%d" % len(b_alm), "%d" % len(a_alm), result, note))

    b_rt, a_rt = set(before["routes"]), set(after["routes"])

    def per_table(routes):
        counts = {}
        for route in routes:
            counts[route.split()[0]] = counts.get(route.split()[0], 0) + 1
        return ", ".join("%s=%d" % kv for kv in sorted(counts.items()))

    added = sorted(a_rt - b_rt)
    removed = sorted(b_rt - a_rt)
    note = "; ".join(["added: " + x for x in added] + ["removed: " + x for x in removed])
    if not note:
        note = "after: " + per_table(a_rt)
    rows.append(_row("Routes", "%d routes" % len(b_rt), "%d routes" % len(a_rt),
                     "same" if not (added or removed) else "CHANGED", note))

    labels = {"bgp": "BGP", "ospf": "OSPF", "ldp": "LDP", "rsvp": "RSVP", "mpls": "MPLS LSPs"}
    for protocol in ("bgp", "ospf", "ldp", "rsvp", "mpls"):
        b_p, a_p = before["protocols"][protocol], after["protocols"][protocol]
        label = labels[protocol]
        if not b_p["configured"] and not a_p["configured"]:
            rows.append(_row(label, "not configured", "not configured", "skipped", ""))
        elif not (b_p.get("available") and a_p.get("available")):
            reason = a_p.get("reason") or b_p.get("reason") or "no live state to compare on this platform"
            rows.append(_row(label, b_p["text"], a_p["text"], "skipped", reason))
        else:
            same = (b_p.get("count"), b_p.get("total")) == (a_p.get("count"), a_p.get("total"))
            rows.append(_row(label, b_p["text"], a_p["text"], "same" if same else "CHANGED",
                             a_p.get("raw", "")))

    def optional(label, key, text_key, describe):
        b_v, a_v = before.get(key), after.get(key)
        b_text = before.get(text_key) or ""
        a_text = after.get(text_key) or ""
        if b_v is None and a_v is None:
            _absent_row(label, a_text or b_text, rows)
            return
        rows.append(_row(label, describe(b_v), describe(a_v), "same" if b_v == a_v else "CHANGED", ""))

    b_ca, a_ca = set(before.get("chassis_alarms", [])), set(after.get("chassis_alarms", []))
    rows.append(_row("Chassis alarms", "%d" % len(b_ca), "%d" % len(a_ca),
                     "same" if b_ca == a_ca else "CHANGED",
                     "; ".join(sorted(a_ca - b_ca) + ["cleared: " + x for x in sorted(b_ca - a_ca)])))

    b_env, a_env = before.get("environment", {}), after.get("environment", {})
    if before.get("environment_absent") and after.get("environment_absent"):
        _absent_row("Chassis environment", "not available", rows)
    else:
        changed = _changed_map(b_env, a_env)
        bad = sorted(k for k, v in a_env.items() if v != "OK")
        rows.append(_row("Chassis environment",
                         "%d sensors, %d not OK" % (len(b_env), sum(1 for v in b_env.values() if v != "OK")),
                         "%d sensors, %d not OK" % (len(a_env), sum(1 for v in a_env.values() if v != "OK")),
                         "same" if not changed else "CHANGED",
                         "; ".join(changed + (["not OK after: " + ", ".join(bad)] if bad else []))))

    b_cd, a_cd = before.get("core_dumps", 0), after.get("core_dumps", 0)
    rows.append(_row("Core dumps", "%d" % b_cd, "%d" % a_cd,
                     "same" if a_cd <= b_cd else "CHANGED",
                     "new core dump(s) after the upgrade" if a_cd > b_cd else ""))

    if not before.get("ntp_configured") and not after.get("ntp_configured"):
        _absent_row("NTP", "not configured", rows)
    else:
        b_ntp, a_ntp = before.get("ntp", {}), after.get("ntp", {})
        synced = lambda d: sum(1 for v in d.values() if v == "synced")
        rows.append(_row("NTP", "%d of %d synced" % (synced(b_ntp), len(b_ntp)),
                         "%d of %d synced" % (synced(a_ntp), len(a_ntp)),
                         "same" if b_ntp == a_ntp else "CHANGED", "; ".join(_changed_map(b_ntp, a_ntp))))

    b_oi, a_oi = before.get("ospf_interfaces", {}), after.get("ospf_interfaces", {})
    nbrs = lambda d: sum(int(v.split("nbrs=")[1]) for v in d.values() if "nbrs=" in v)
    rows.append(_row("OSPF interfaces", "%d interfaces, %d neighbors" % (len(b_oi), nbrs(b_oi)),
                     "%d interfaces, %d neighbors" % (len(a_oi), nbrs(a_oi)),
                     "same" if b_oi == a_oi else "CHANGED", "; ".join(_changed_map(b_oi, a_oi))))

    optional("LDP database", "ldp_database", "ldp_database_text", lambda v: "%d labels" % (v or 0))
    optional("RSVP interfaces", "rsvp_interfaces", "rsvp_text", lambda v: "%d up" % (v or 0))
    optional("RSVP sessions", "rsvp_sessions", "rsvp_text", lambda v: "%d up" % (v or 0))

    rows.append(_row("Routing: AS / Router ID", "%s / %s" % (before.get("route_as"), before.get("router_id")),
                     "%s / %s" % (after.get("route_as"), after.get("router_id")),
                     "same" if (before.get("route_as"), before.get("router_id")) == (after.get("route_as"), after.get("router_id"))
                     else "CHANGED", ""))

    if not before.get("lldp_configured") and not after.get("lldp_configured"):
        _absent_row("LLDP neighbors", "not configured", rows)
    else:
        b_ll, a_ll = set(before.get("lldp", [])), set(after.get("lldp", []))
        rows.append(_row("LLDP neighbors", "%d" % len(b_ll), "%d" % len(a_ll),
                         "same" if b_ll == a_ll else "CHANGED",
                         "; ".join(["new: " + x for x in sorted(a_ll - b_ll)] + ["lost: " + x for x in sorted(b_ll - a_ll)])))

    optional("LACP", "lacp", "lacp_text", lambda v: "%d aggregated interfaces" % (v or 0))

    def re_text(state):
        return "%s %s, %s" % (state.get("Current state", "?"), state.get("Model", "?"), state.get("DRAM", ""))

    b_re, a_re = before.get("routing_engine", {}), after.get("routing_engine", {})
    rows.append(_row("Routing Engine", re_text(b_re), re_text(a_re),
                     "same" if b_re == a_re else "CHANGED", "; ".join(_changed_map(b_re, a_re))))

    same_config = before["config"] == after["config"]
    rows.append(_row("Running configuration", "captured", "captured",
                     "same" if same_config else "CHANGED",
                     "" if same_config else "the configuration changed during the upgrade"))
    return rows


def junos_table(rows) -> str:
    """Render rows from junos_diff as an aligned plain-text table."""
    widths = {"check": 26, "before": 34, "after": 34, "result": 13}
    header = "%s  %s  %s  %s  %s" % (
        "CHECK".ljust(widths["check"]), "BEFORE".ljust(widths["before"]),
        "AFTER".ljust(widths["after"]), "RESULT".ljust(widths["result"]), "DETAIL")
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append("%s  %s  %s  %s  %s" % (
            row["check"].ljust(widths["check"]), row["before"].ljust(widths["before"]),
            row["after"].ljust(widths["after"]), row["result"].ljust(widths["result"]),
            row.get("note", "")))
    return "\n".join(lines)


class FilterModule(object):
    def filters(self):
        return {"junos_state": junos_state, "junos_diff": junos_diff, "junos_table": junos_table}
