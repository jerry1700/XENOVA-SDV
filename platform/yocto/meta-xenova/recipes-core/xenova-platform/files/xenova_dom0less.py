"""Bind the three Xen boot domains to the existing per-board registry."""

import json
import os
from pathlib import Path
from uuid import UUID

from xenova_runtime import PlatformError
from xenova_vm import guest_kind, has_network, regular_asset, validate_record


FACTORY_GUESTS = ('debian-dev', 'zephyr-drive-a', 'zephyr-drive-b')
INIT = '/usr/lib/xen/bin/init-dom0less'
XVDA = 51712  # Xen PV block device number for xvda.


def enabled():
    return 'xenova.dom0less=1' in Path('/proc/cmdline').read_text().split()


def receipts(platform):
    execution = platform.execution()
    value = execution.read(platform.runtime / 'dom0less.json')
    if value is None or value['host_boot_id'] != execution.host:
        return {}
    return value['guests']


def owns_execution(platform, record, row, deadline):
    proof = receipts(platform).get(record['id'])
    if proof and proof['phase'] == 'ready' and proof['uuid'] == record['uuid'] and proof['domid'] == row['domid']:
        unique = platform.execution().unique(row, deadline)
        if proof['xen_unique_id'] == unique:
            return unique
    return None


def _boot_inventory(platform, deadline):
    # Unlike Platform.domains(), this accepts the three unnamed loader handles.
    raw = platform.timed_command(['xl', 'list', '-v'], deadline).splitlines()
    expected_header = ['Name', 'ID', 'Mem', 'VCPUs', 'State', 'Time(s)', 'UUID',
                       'Reason-Code', 'Security', 'Label']
    if not raw or raw[0].split() != expected_header:
        raise PlatformError('unexpected Xen boot inventory')
    domains = {}
    domids = set()
    for line in raw[1:]:
        fields = line.split()
        if len(fields) < 9 or not fields[1].isdigit():
            raise PlatformError('invalid Xen boot inventory row')
        name, domid, handle = fields[0], int(fields[1]), str(UUID(fields[6]))
        if domid in domids or handle in domains:
            raise PlatformError('duplicate Xen boot inventory identity')
        domids.add(domid)
        domains[handle] = {'domid': domid, 'name': name}
    if 0 not in domids:
        raise PlatformError('dom0 absent from Xen boot inventory')
    return domains


def prepare(platform, records, deadline):
    """Called under the VM lock after first registration, before CPU prepare."""
    if not enabled():
        return
    execution = platform.execution()
    execution.gate()
    manifest = json.loads(regular_asset(platform.data, '.xenova-install.json').read_text())
    guests = manifest['dom0less']['guests']
    state = json.loads(regular_asset(platform.data, '.first-install-state.json').read_text())
    if set(guests) != set(FACTORY_GUESTS) or set(state['identities']) != set(guests):
        raise PlatformError('invalid dom0less factory roles')

    path = platform.runtime / 'dom0less.json'
    owned = receipts(platform)

    def save():
        execution.write(path, {'schema_version': '1.0', 'host_boot_id': execution.host, 'guests': owned})

    policy = platform.cpu().policy()
    for key in FACTORY_GUESTS:
        guest = guests[key]
        handle = str(UUID(hex=guest['boot_id']))
        identity = state['identities'][key]['uuid']
        record = records.get(key)
        prior = owned.get(key)
        if prior and prior['phase'] in ('ready', 'discarded'):
            continue
        domains = _boot_inventory(platform, deadline)
        row = domains.get(handle)
        if prior and row is None:
            row = domains.get(identity)
        if row is None:
            if prior and prior['phase'] == 'discarding':
                owned[key] = {**prior, 'phase': 'discarded'}
                save()
                continue
            raise PlatformError(f'{key}: Xen did not report the expected factory boot domain')
        if not row['domid'] or (handle in domains and identity in domains):
            raise PlatformError(f'{key}: conflicting Xen boot identity')
        current = {**row, 'xen_unique_id': execution.unique(row, deadline)}
        if prior and any(prior[k] != current[k] for k in ('domid', 'xen_unique_id')):
            raise PlatformError(f'{key}: factory boot domain incarnation changed')
        if prior is None and row['name'] != '(null)':
            raise PlatformError(f'{key}: boot domain was modified before registration')
        if any(item['name'] == key and item['domid'] != row['domid'] for item in domains.values()):
            raise PlatformError(f'{key}: registered Xen name is already in use')

        keep = bool(record and record['uuid'] == identity and record['name'] == key
                    and record['memory_mib'] == guest['memory_mib']
                    and record['vcpus'] == guest['vcpus'])
        if keep:
            decision = execution.boot_decision(record)
            keep = decision['start']
        if keep and policy:
            from xenova_cpu import vm_placement
            keep = vm_placement(policy, identity)[1] == guest['pcpus']
        if keep:
            directory = platform.data / 'instances' / key
            try:
                validate_record(record, directory)
                if guest_kind(record) == 'linux':
                    boot = json.loads(regular_asset(directory, 'boot.json').read_text())
                    keep = (boot['kernel_sha256'] == guest['assets']['kernel']['sha256']
                            and boot['initrd_sha256'] == guest['assets']['ramdisk']['source_sha256'])
                else:
                    keep = record['active_firmware']['sha256'] == guest['assets']['kernel']['sha256']
            except (OSError, ValueError, KeyError, PlatformError):
                # Exclude this incarnation so autostart can report the bad asset
                # without withholding network/storage from the other two VMs.
                keep = False
        owned[key] = {'domid': row['domid'], 'xen_unique_id': current['xen_unique_id'],
                      'uuid': identity, 'phase': 'registering' if keep else 'discarding'}
        save()
        platform.timed_command([INIT, str(row['domid']), handle, identity, key], deadline)
        observed = _boot_inventory(platform, deadline).get(identity)
        if (observed != {'domid': row['domid'], 'name': key}
                or execution.unique(row, deadline) != current['xen_unique_id']):
            raise PlatformError(f'{key}: factory boot domain changed during registration')
        if not keep:
            # No guest disk, NIC or MQTT credentials have been supplied.
            platform.timed_command(['xl', 'destroy', str(row['domid'])], deadline)
            if any(item['domid'] == row['domid'] for item in _boot_inventory(platform, deadline).values()):
                raise PlatformError(f'{key}: excluded factory boot domain is still present')
        owned[key]['phase'] = 'ready' if keep else 'discarded'
        save()


def prepare_cpu(platform, sample, policy):
    """Validate preloaded guests before allowing boot-only CPU preparation."""
    rows = {}
    for guest in receipts(platform).values():
        row = sample['domains'].get(guest['domid'])
        if guest['phase'] == 'ready' and row and row['xen_unique_id'] == guest['xen_unique_id']:
            if row['vm_id'] != guest['uuid']:
                raise PlatformError('factory boot domain UUID changed')
            rows[guest['domid']] = row
    if rows:
        from xenova_cpu import vm_placement
        for row in rows.values():
            _, cpus = vm_placement(policy, row['vm_id'])
            if any(vcpu['pcpus'] != cpus for vcpu in row['hard_affinity']):
                raise PlatformError('factory boot domain CPU placement changed')
    return {**sample, 'domains': {key: row for key, row in sample['domains'].items() if key not in rows}}


def adopt(platform, record, row, deadline, *, owner=None, result=None):
    """Attach PV backends to the same incarnation and publish its execution once."""
    proof = receipts(platform).get(record['id'])
    if not proof or proof['phase'] != 'ready' or proof['domid'] != row['domid']:
        return None
    execution = platform.execution()
    unique = execution.unique(row, deadline)
    if unique != proof['xen_unique_id']:
        return None
    if proof['uuid'] != record['uuid']:
        raise PlatformError('factory boot domain no longer matches the registered VM')
    previous = execution.run_record(record)
    if (previous and previous['phase'] == 'observed' and previous.get('cpu_verification') == 'complete'
            and previous['domid'] == row['domid'] and previous['xen_unique_id'] == unique):
        return
    expected = {**row, 'xen_unique_id': unique}
    domid = str(row['domid'])
    directory = platform.data / 'instances' / record['id']

    def current():
        execution.gate()
        return platform.resolve_domain(record, expected=expected, deadline=deadline)['config']

    def attach(args):
        current()
        if result is not None:
            result.update(physical_submitted=True, stage='start_submit')
        platform.timed_command(args, deadline)
        return current()

    # Reuse the execution identity across partial device attachment and retry.
    # An xl create monitor does not exist for this Xen-created incarnation.
    if previous is None or previous['phase'] == 'stopped':
        launch = execution.prepare_launch(record, owner, deadline=deadline, early_boot=True)
    elif previous.get('early_boot'):
        launch = previous
    else:
        raise PlatformError('a different launch already owns the factory VM')
    execution.guard_launch(record, launch, deadline)
    config = current()
    if guest_kind(record) == 'linux':
        platform.timed_command(['modprobe', 'xen-blkback'], deadline)
        disk = regular_asset(directory, 'root.raw')
        for other in platform.domains(deadline=deadline):
            if other['domid'] == row['domid']:
                continue
            for item in other.get('config', {}).get('disks', []):
                path = item.get('pdev_path')
                if path and Path(path).exists() and os.path.samefile(path, disk):
                    raise PlatformError('factory root disk already attached to another VM')
        disks = config['disks']
        if not disks:
            # The standard block script manages a loop device for raw files.
            # qdisk requires a per-guest QEMU already running at hotplug time.
            disks = attach(['xl', 'block-attach', domid,
                           f'format=raw,vdev=xvda,access=rw,backendtype=phy,script=block,target={disk}'])['disks']
        if (len(disks) != 1 or disks[0].get('vdev') != 'xvda'
                or not disks[0].get('pdev_path') or not os.path.samefile(disks[0]['pdev_path'], disk)):
            raise PlatformError('factory root disk attachment could not be verified')
        backend = f'/local/domain/0/backend/vbd/{domid}/{XVDA}'
        if not platform.timed_command(['xenstore-read', backend + '/physical-device'], deadline).strip():
            raise PlatformError('factory root disk backend is not ready')

    if has_network(record):
        nics = current().get('nics', [])
        if not nics:
            nics = attach(['xl', 'network-attach', domid, 'type=vif', f"mac={record['mac']}",
                           'bridge=xenbr0', 'script=vif-bridge']).get('nics', [])
        if len(nics) != 1 or nics[0].get('mac') != record['mac'] or nics[0].get('bridge') != 'xenbr0':
            raise PlatformError('factory network attachment could not be verified')

    execution.guard_launch(record, launch, deadline)
    execution.bind_launch(record, launch, expected, deadline)
