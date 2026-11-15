"""Package the factory guests into the assembled SD's private copy of the boot FAT."""

import hashlib
import json
from pathlib import Path
import shutil
import struct
import zlib

from artifact_io import regular, sha
from first_install_contract import SECTOR
import xenova_zephyr as zephyr


# Physical load windows on the Pi 4. The BSP's working FDT and U-Boot scripts
# are below 128 MiB. These addresses are unrelated to each guest's IPA.
SLOTS = {
    'debian-dev': (('debian.img', 0x08000000, 0x10000000, 'kernel'),
                   ('debian.rd', 0x10000000, 0x18000000, 'ramdisk')),
    'zephyr-drive-a': (('drive-a.img', 0x18000000, 0x1A000000, 'kernel'),),
    'zephyr-drive-b': (('drive-b.img', 0x1A000000, 0x1C000000, 'kernel'),),
}
SCRIPT = 'xenova-guests.scr'
ROOT_WAIT = b"""#!/bin/sh
case "$1" in prereqs) exit 0 ;; esac
case " $(cat /proc/cmdline) " in
    *" xenova.early-root=1 "*) ;;
    *) exit 0 ;;
esac
remaining=540
while [ ! -b /dev/xvda ] && [ "$remaining" -gt 0 ]; do
    sleep 1
    remaining=$((remaining - 1))
done
"""


def crc32(path):
    with path.open('rb') as stream:
        checksum = 0
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum = zlib.crc32(block, checksum)
        return f'{checksum:08x}'


def require_firmware(entry):
    if not entry['metadata'].get('hardware_profile'):
        return
    config = regular(Path(entry['template']) / 'zephyr.config').read_text().splitlines()
    if 'CONFIG_XENOVA_DOM0LESS_BOOT=y' not in config:
        raise ValueError('rebuild the Zephyr drive template with Xen boot guest backend waiting enabled')


def boot_script(script):
    """Emit U-Boot's legacy, uncompressed script image with both discovery CRCs."""
    payload = struct.pack('>II', len(script), 0) + script
    fields = [0x27051956, 0, 0, len(payload), 0, 0, zlib.crc32(payload),
              5, 22, 6, 0, b'Xenova factory guests']
    fmt = '>7I4B32s'
    fields[1] = zlib.crc32(struct.pack(fmt, *fields))
    return struct.pack(fmt, *fields) + payload


def _newc(name, payload, inode, mode):
    name = name.encode() + b'\0'
    fields = (inode, mode, 0, 0, 1, 0, len(payload), 0, 0, 0, 0, len(name), 0)
    entry = b'070701' + ''.join(f'{value:08x}' for value in fields).encode() + name
    return entry + b'\0' * (-len(entry) % 4) + payload + b'\0' * (-len(payload) % 4)


def _early_initrd(original, work):
    # The kernel accepts a newc archive after the original compressed archive.
    # Debian initramfs-tools rootdelay also sleeps when the disk is ready.
    path = work / 'debian-early-initrd'
    with path.open('xb') as output, original.open('rb') as source:
        shutil.copyfileobj(source, output)
        output.write(b'\0' * (-output.tell() % 4))
        output.write(_newc('scripts/init-premount/xenova-root', ROOT_WAIT, 1, 0o100755))
        output.write(_newc('TRAILER!!!', b'', 0, 0))
    return path


def prepare(factory, data, preset_path):
    """Use the exact registered seed assets and the factory CPU preset."""
    preset = json.loads(preset_path.read_text())
    exclusive = {item['instance_id']: [item['pcpu']] for item in preset['exclusive_instances']}
    records = {item['record']['id']: item['record']
               for item in factory['instances'] + factory['firmware_instances']}
    if set(records) != set(SLOTS):
        raise ValueError('early boot requires the three current factory guests')
    guests, files = {}, {}
    for name, slots in SLOTS.items():
        record = records[name]
        seed = data / 'seeds' / name
        linux = name == 'debian-dev'
        paths = (seed / 'Image', seed / 'initrd') if linux else (
            zephyr.active_directory(seed, record) / 'zephyr.bin',)
        assets = {}
        for path, (filename, start, end, kind) in zip(paths, slots, strict=True):
            size = regular(path).stat().st_size
            if not 0 < size < end - start:
                raise ValueError(f'{name}: {kind} does not fit its U-Boot load window')
            files[filename] = path
            assets[kind] = {'sha256': sha(path), 'crc32': crc32(path), 'bytes': size}
        guests[name] = {'assets': assets,
                        'memory_mib': record['memory_mib'], 'vcpus': record['vcpus'],
                        'pcpus': exclusive.get(name, preset['shared_pcpus'])}
    return guests, files


def render(guests, shared_cpus):
    """Describe all domUs in the Pi's working FDT using their exact FAT lengths."""
    shared = ','.join(map(str, shared_cpus))
    lines = [
        'fdt resize 0x4000 || exit 1',
        'setenv dom0_bootargs "${dom0_bootargs} xenova.dom0less=1"',
        'fdt set /chosen xen,dom0-bootargs "${dom0_bootargs}" || exit 1',
    ]
    for vcpu in range(2):
        child = f'/chosen/dom0/vcpu{vcpu}'
        lines.extend([
            f'fdt mknode /chosen/dom0 vcpu{vcpu} || exit 1',
            f'fdt set {child} compatible "xen,vcpu" || exit 1',
            f'fdt set {child} id <{vcpu}> || exit 1',
            f'fdt set {child} hard-affinity "{shared}" || exit 1',
        ])
    for name, slots in SLOTS.items():
        guest = guests[name]
        node = '/chosen/' + name
        boot_id_cells = ' '.join(guest['boot_id'][i:i+2] for i in range(0, 32, 2))
        mask = ','.join(map(str, guest['pcpus']))
        lines.extend([
            f'fdt mknode /chosen {name} || exit 1',
            f'fdt set {node} compatible "xen,domain" || exit 1',
            f'fdt set {node} "#address-cells" <1> || exit 1',
            f'fdt set {node} "#size-cells" <1> || exit 1',
            f'fdt set {node} memory <0 0x{guest["memory_mib"] * 1024:x}> || exit 1',
            f'fdt set {node} cpus <{guest["vcpus"]}> || exit 1',
            f'fdt set {node} xen,enhanced "enabled" || exit 1',
            f'fdt set {node} xenova,boot-id [{boot_id_cells}] || exit 1',
        ])
        for vcpu in range(guest['vcpus']):
            child = f'vcpu{vcpu}'
            lines.extend([
                f'fdt mknode {node} {child} || exit 1',
                f'fdt set {node}/{child} compatible "xen,vcpu" || exit 1',
                f'fdt set {node}/{child} id <{vcpu}> || exit 1',
                f'fdt set {node}/{child} hard-affinity "{mask}" || exit 1',
            ])
        for filename, start, _, kind in slots:
            module = f'module@{start:x}'
            asset = guest['assets'][kind]
            size = asset['bytes']
            lines.extend([
                f'fatload mmc ${{devnum}}:1 0x{start:x} {filename} 0x{size + 1:x} || exit 1',
                f'if itest.l 0x${{filesize}} == 0x{size:x} && crc32 0x{start:x} 0x{size:x} 0x07fffff8 '
                f'&& itest.l *0x07fffff8 == 0x{asset["crc32"]}; then',
                f'    fdt mknode {node} {module} || exit 1',
                f'    fdt set {node}/{module} compatible "multiboot,{kind}" "multiboot,module" || exit 1',
                f'    fdt set {node}/{module} reg <0x{start:x} 0x{size:x}> || exit 1',
                'else',
                f'    echo Xenova: size or CRC check failed for {filename}',
                '    exit 1',
                'fi',
            ])
            if name == 'debian-dev' and kind == 'kernel':
                lines.append(f'fdt set {node}/{module} bootargs '
                             '"console=hvc0 root=/dev/xvda rw xenova.early-root=1 net.ifnames=0" || exit 1')
    return ('\n'.join(lines) + '\n').encode()


def install(image, table, work, data, factory, preset_path, command):
    guests, files = prepare(factory, data, preset_path)
    original = guests['debian-dev']['assets']['ramdisk']
    ramdisk = _early_initrd(files['debian.rd'], work)
    _, start, end, _ = SLOTS['debian-dev'][1]
    size = ramdisk.stat().st_size
    if size >= end - start:
        raise ValueError('debian-dev: early initrd does not fit its U-Boot load window')
    original.update(source_sha256=original['sha256'], sha256=sha(ramdisk),
                    crc32=crc32(ramdisk), bytes=size)
    files['debian.rd'] = ramdisk
    for name, guest in guests.items():
        # Xen reports this handle before XenStore exists. Include both the role
        # and the bytes passed to Xen, then replace it with the per-card UUID.
        evidence = json.dumps([name, guest['assets']], sort_keys=True, separators=(',', ':'))
        guest['boot_id'] = hashlib.sha256(evidence.encode()).hexdigest()[:32]
    shared = json.loads(preset_path.read_text())['shared_pcpus']
    script = work / SCRIPT
    script.write_bytes(boot_script(render(guests, shared)))
    files[SCRIPT] = script
    volume = f"{image}@@{table['partitions'][0]['start'] * SECTOR}"
    fingerprints = {}
    for filename, path in files.items():
        expected = sha(path)
        command(['mcopy', '-i', volume, path, '::' + filename])
        readback = work / ('fat-' + filename)
        command(['mcopy', '-i', volume, '::' + filename, readback])
        if sha(readback) != expected:
            raise ValueError(f'factory boot FAT verification failed: {filename}')
        fingerprints[filename] = expected
    return {'guests': guests, 'files': fingerprints}
