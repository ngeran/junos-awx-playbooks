# Junos AWX Playbooks

Playbooks and a custom AWX Execution Environment (EE) for managing Juniper
Junos EX switches with `juniper.device`.

## Why the `_pyez_conn` error happens

`AttributeError: 'JuniperJunosModule' object has no attribute '_pyez_conn'`
is a **secondary** error. When the module cannot get a working connection, its
`fail_json()` handler calls `close_configuration()`, which touches
`self._pyez_conn` before that attribute has been assigned. The original error
(no PyEZ, wrong connection plugin, auth failure, etc.) is hidden.

There are two real problems in the original setup:

1. The stock `quay.io/ansible/awx-ee:24.6.1` image does **not** contain
   `junos-eznc`, so PyEZ cannot work at all.
2. `awx-ee:24.6.1` ships **ansible-core 2.15.12**. On that core version the
   `juniper.device.pyez` connection plugin crashes with
   `'Connection' object has no attribute 'nonetype'` (fixed in ansible-core
   2.16+). The playbooks therefore use `connection: local`, which lets the
   `juniper.device` modules open the PyEZ session themselves.

## Execution Environment contents

| Component | Version | Why |
| --- | --- | --- |
| Base image | `quay.io/ansible/awx-ee:24.6.1` | Matches the AWX job runner (ansible-core 2.15 / Python 3.9) |
| Collection | `juniper.device` 1.0.7 | Last release that supports ansible-core 2.15 |
| Python | `junos-eznc`, `jxmlease`, `xmltodict`, `looseversion` | PyEZ and the collection's runtime dependencies |

`juniper.device` 1.0.8+ requires ansible-core 2.16/2.17. Do not raise the
collection version unless the EE base is also upgraded to a newer core.

## Build and push the EE

```bash
python3 -m venv .venv-build
. .venv-build/bin/activate
pip install ansible-builder

# Docker (use --container-runtime podman if that is what you use)
ansible-builder build \
  --tag registry.example.com/awx/junos-ee:1.0.0 \
  --container-runtime docker

# Verify PyEZ is really inside the image
docker run --rm registry.example.com/awx/junos-ee:1.0.0 \
  python3 -c "import jnpr.junos, jxmlease; print(jnpr.junos.__version__, jxmlease.__version__)"

docker push registry.example.com/awx/junos-ee:1.0.0
```

## Register the EE in AWX

1. If the registry is private, create a **Container Registry** credential.
2. **Administration → Execution Environments → Add**:
   - Image: `registry.example.com/awx/junos-ee:1.0.0`
   - Pull: `Always` while testing, then `Only pull if not present`
   - Registry credential: select the one created above
3. Edit the Job Template and select the new EE.

## Credentials

Attach a **Machine** credential with the device username (`admin`) and
password to the Job Template. With `connection: local`, Ansible passes the
`--ask-pass` password to the module as `passwd`.

Inventory needs the device address:

```ini
[ex]
EX2300 ansible_host=192.0.2.10 ansible_user=admin
```

Do not put the password in the playbook. For local CLI runs, use `-k` or
`-e ansible_password=...`.

## Supplying the image name and MD5

`step1_validate_image.yml` does not hardcode the image. It needs two values:

- `image_filename`: file name in `/var/tmp` on the switch
- `expected_md5`: the 32-character MD5 the image should have

On the command line Ansible prompts for both. In AWX, add the survey in
`awx-survey.json` to the Job Template (**Templates → Edit → Survey → Add**,
or load the file with the API) so the launcher is asked when starting the job.

If the image is not on the switch, the play prints a message telling the user
to upload it manually and stops for that host without failing.

## Step 2: upgrade (`step2_upgrade.yml`)

Runs the checks, then installs and optionally reboots. Each check is a gate
that either fails, or stops with an instruction for the user.

Inputs come from prompts on the command line, or in AWX from the survey in
`awx-survey-upgrade.json`:

| Variable | Meaning |
| --- | --- |
| `image_filename` | File in `/var/tmp` on the switch |
| `expected_md5` | MD5 the image must have |
| `target_version` | Optional; taken from the file name if blank |
| `validate_package` | `yes` / `no`: check the image against the config before installing |
| `run_mode` | `check_only`, `install_only`, `install_and_reboot` |
| `save_rescue` | `yes` / `no`: save a rescue config if none exists |

Measured on an EX2300-C-12P: package validation takes about 11 minutes and a
reboot about 6.5 minutes. AWX shows no output while a single task runs, so
expect silence during validation and during the install.

## Pre/post comparison

In `install_only` and `install_and_reboot` the play captures the switch state
before the install (`tasks/collect_state.yml`) and compares it with the state
after the reboot:

- interfaces (admin/link) and interface descriptions
- system alarms and chassis alarms (timestamps ignored, since they always change)
- chassis environment (power, temperature, fan status)
- Routing Engine state and model
- core dumps (a new one after the upgrade is a difference)
- NTP associations (how many are synced)
- routes per table, with prefix and protocol, plus AS number and router ID
- OSPF interfaces and neighbors
- BGP, LDP, RSVP, MPLS and LACP. A protocol that is not configured or not
  running is shown as such and its comparison is `skipped`.
- LLDP neighbors
- the running configuration

Commands sent are listed in `junos_state_commands` in `step2_upgrade.yml`.
`| no-more` and `| count` are not used: the Junos module rejects them, NETCONF
never pages, and the route count is computed from the parsed routes.
`request system configuration rescue save` is an action, not a check; it is
done once before the install when `save_rescue` is `yes`.

The result is a plain-text table.

The playbook does not judge differences. Rows are:

- `same` and `skipped` (feature not configured or not running): nothing to do.
- `informational`: expected after any install. Only the `set version` line of
  the configuration changed, or alarms about a full `/var` partition cleared.
- `CHANGED` and `NEW ALARMS`: listed in the report under **Engineer review**
  with a sign-off section.

By default the job stays successful and the engineer reviews the report.
Launch with `post_check_strict: yes` to fail the job instead.

Parsing lives in `filter_plugins/junos_state.py`. With `install_only` there is
no post-check inside the job, because the reboot happens later by hand.

`ansible.cfg` sets `result_format = yaml`, which keeps line breaks in the AWX
output so the table is readable.

Not yet run against a switch: `install_only` and `install_and_reboot` with the
comparison included. The comparison itself was tested by capturing the same
switch twice, and the parser was checked on this EX2300-C. Other Junos
platforms may print these commands differently.

## Report

Every run of `step2_upgrade.yml` ends by building a Markdown report
(`templates/upgrade_report.md.j2`): result, versions, start and finish time,
what was requested, the pre/post table and the list of differences. It is
written when the run succeeds and when it stops or fails at a later gate
(the input check and the MD5 check fail before a report is possible).

The report is available in two places:

- **Job output.** It is printed at the end, so it is part of the job log.
  In AWX open the job, and download the output as a file with
  `GET /api/v2/jobs/<id>/stdout/?format=txt_download` (saves `job_<id>.txt`).
- **Job artifacts.** It is stored with `set_stats` as `report_markdown`
  (AWX: job output, Artifacts tab; API: `GET /api/v2/jobs/<id>/artifacts/`).

AWX keeps both with the job record. The files inside the job's own container
disappear when the job ends, so the report is not written to disk there.

## Test order when the switch is back online

```bash
# 1. Confirms PyEZ is installed and shows the real connection error, if any
ansible-playbook -i inventory.ini diagnose_pyez.yml -vv

# 2. The actual validation playbook
ansible-playbook -i inventory.ini step1_validate_image.yml
```

In AWX, run `diagnose_pyez.yml` as a Job Template first. If it fails, the
error message is now the real one (for example
`Unable to make a PyEZ connection: ConnectRefusedError(None)` or an
authentication error) instead of `_pyez_conn`.

## Notes

- `juniper.device.command` does **not** support pipe modifiers such as
  `| match Junos:`. The playbook runs `show version` and filters the output
  in Ansible.
- `stdout` for a single command is a multi-line string, not a list.
  Use `stdout_lines` or search `stdout` directly; `stdout[0]` is the first
  character.
- The `ANSIBLE_COLLECTIONS_PATHS` deprecation warning is emitted by
  **AWX 24.6.1 itself** (it sets the old plural variable). It is fixed in
  newer AWX releases. It is harmless and unrelated to the Juniper failure.
- If you later move to an EE with ansible-core >= 2.16, you can switch the
  playbook back to `connection: juniper.device.pyez` and use
  `juniper.device` 2.0.x.
