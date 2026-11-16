"""Bind the three Xen boot domains to the existing per-board registry."""

import json
import os
from pathlib import Path
from uuid import UUID

from xenova_runtime import PlatformError
from xenova_vm import DomainIdentityError, guest_kind, has_network, regular_asset, validate_record


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


def _factory(platform):
    manifest = json.loads(regular_asset(platform.data, '.xenova-install.json').read_text())
    guests = manifest['dom0less']['guests']
    identities = json.loads(regular_asset(platform.data, '.first-install-state.json').read_text())['identities']
    if set(guests) != set(FACTORY_GUESTS) or set(identities) != set(guests):
        raise PlatformError('invalid dom0less factory roles')
    return guests, identities


def prepare(platform, records, deadline):
    """Called under the VM lock after first registration, before CPU prepare."""
    if not enabled():
        return
    execution = platform.execution()
    execution.gate()
    guests, identities = _factory(platform)

    path = platform.runtime / 'dom0less.json'
    owned = receipts(platform)

    def save():
        execution.write(path, {'schema_version': '1.0', 'host_boot_id': execution.host, 'guests': owned})

    policy = platform.cpu().policy()
    for key in FACTORY_GUESTS:
        guest = guests[key]
        handle = str(UUID(hex=guest['boot_id']))
        identity = identities[key]['uuid']
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


def _check_unattached(platform, domid, deadline):
    """Check frontend and dom0 backend nodes, including incomplete hotplug."""
    def children(path):
        return set(platform.timed_command(['xenstore-list', path], deadline).splitlines())

    domains = children('/local/domain')
    if '0' not in domains:
        raise PlatformError('dom0 missing from XenStore; boot domain devices unverified')
    guest = '/local/domain/' + domid
    # init-dom0less creates device/suspend before any platform hotplug.
    attached = domid in domains and 'device' in children(guest) and children(guest + '/device') - {'suspend'}
    backend = '/local/domain/0/backend'
    if attached or ('backend' in children('/local/domain/0') and
                    any(domid in children(backend + '/' + kind) for kind in children(backend))):
        raise PlatformError(f'factory boot domain {domid} has devices; refusing discard')


def discard_unadopted(platform, deadline):
    """Dispose only proven unattached boot domains under the shutdown VM lock."""
    if not enabled():
        return
    guests, identities = _factory(platform)
    targets = {key: (str(UUID(guests[key]['boot_id'])), str(UUID(identities[key]['uuid'])))
               for key in FACTORY_GUESTS}
    identifiers = {value for pair in targets.values() for value in pair}
    if len(identifiers) != 2 * len(targets):
        raise DomainIdentityError('duplicate factory boot UUID during shutdown')
    owned = receipts(platform)
    execution = platform.execution()
    inventory = _boot_inventory(platform, deadline)
    for key, (handle, identity) in targets.items():
        proof = owned.get(key)
        raw = inventory.get(handle)
        row = raw or inventory.get(identity)
        if not row or (not raw and not proof):
            continue  # Ordinary registered execution or absent factory domain.
        if not row['domid'] or (raw and identity in inventory):
            raise DomainIdentityError(f'{key}: ambiguous factory boot domain during shutdown')
        if execution.run_record({'uuid': identity}) is not None:
            continue  # A launch may already have submitted hotplug. Apply normal stop policy.
        phases = ('registering', 'discarding') if raw else ('registering', 'discarding', 'ready')
        if proof and (proof['phase'] not in phases or proof['uuid'] != identity or proof['domid'] != row['domid']):
            continue  # A stale receipt does not authorize disposal of a replacement.
        names = ('(null)',) if raw else ('(null)', key)
        if row['name'] not in names:
            raise DomainIdentityError(f'{key}: factory boot name changed during shutdown')
        expected = execution.unique(row, deadline)
        if proof and proof['xen_unique_id'] != expected:
            continue
        domid = str(row['domid'])
        _check_unattached(platform, domid, deadline)
        locator = handle if raw else identity
        current = _boot_inventory(platform, deadline)
        if (current.get(locator) != row or (raw and identity in current)
                or execution.unique(row, deadline) != expected):
            raise DomainIdentityError(f'{key}: factory boot incarnation changed before discard')
        platform.timed_command(['xl', 'destroy', domid], deadline)
        after = _boot_inventory(platform, deadline)
        if handle in after or identity in after or any(r['domid'] == row['domid'] for r in after.values()):
            raise PlatformError(f'{key}: factory boot domain removal is unconfirmed')
        execution.event('factory-boot-domain-discarded', details={'id': key, 'domid': row['domid']})


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
    """Attach PV backends once and return the execution identity for start confirmation."""
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
        return previous
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
    return execution.bind_launch(record, launch, expected, deadline)
