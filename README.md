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
