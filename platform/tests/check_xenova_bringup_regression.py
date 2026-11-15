"""임시 경로와 명령 대역만 사용하며 실제 mount/NIC/부팅을 수행하지 않는다."""
import configparser
import fnmatch
import os
import sys
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2] / 'platform/yocto/meta-xenova'
boot = (ROOT / 'recipes-bsp/xen-rpi-u-boot-scr/files/boot.cmd.xen.in').read_text()
# U-Boot 셀 표기는 호스트 셸 대역에서 문자열로 전달한다.
boot = boot.replace('<${kernel_loadaddr} ${kernel_size}>', '"<${kernel_loadaddr} ${kernel_size}>"').replace('<1>', "'<1>'")
boot = boot.replace('@@RPI_DOM0_MEM@@', '1024M').replace('@@RPI_DEBUG_XEN_ARGS@@', 'sync_console bootscrub=0')
boot_mocks = r'''
setenv() { key=$1; shift; export "$key=$*"; }
declare -A fdt_args fdt_guests
fdt() {
    echo "FDT $*"
    [ "$FAIL" != "fdt-$1" ] && [ "$FAIL" != "fdt-$1-${3:-}" ] || return 1
    case "$1" in
        move)
            [ "$FAIL" != fdt-copy-guest ] || [ "$3" != 0x07800000 ] || return 1
            fdt_args[$3]=${fdt_args[$2]:-}
            fdt_guests[$3]=${fdt_guests[$2]:-}
            working_fdt=$3
            ;;
        addr) working_fdt=$2 ;;
        set)
            if [ "$3" = xen,dom0-bootargs ]; then fdt_args[$working_fdt]=$4; fi
            ;;
        get) setenv "$3" "${fdt_args[$working_fdt]}" ;;
        mknode)
            if [ "$3" != dom0 ]; then fdt_guests[$working_fdt]+=" $3"; fi
            ;;
    esac
}
part() { echo "PART $*"; [ "${FAIL:-}" != part ] || return 1; rootfs_partuuid=test-root; }
fatload() {
    echo "LOAD $*"
    [ "$FAIL" != "$4" ] || return 1
    filesize=1234
    if [ "$4" = xenova-guests.scr ]; then filesize=${SCRIPT_FILESIZE:-1234}; fi
}
size() { [ "${GUEST_SCRIPT:-}" = 1 ]; }
itest.l() {
    case "$2" in
        -gt) (( $1 > $3 )) ;;
        -le) (( $1 <= $3 )) ;;
        *) return 2 ;;
    esac
}
source() {
    echo "SOURCE $*"
    [ "$FAIL" != source-header ] || return 1
    # Model failure after the real guest script has changed the working FDT.
    setenv dom0_bootargs "$dom0_bootargs xenova.dom0less=1"
    fdt set /chosen xen,dom0-bootargs "$dom0_bootargs"
    fdt mknode /chosen debian-dev
    [ "$FAIL" != source ] || return 1
    fdt mknode /chosen zephyr-drive-a
    fdt mknode /chosen zephyr-drive-b
}
booti() {
    echo "BOOT $*"
    [ "$3" = "$working_fdt" ] || return 1
    echo "BOOT_ARGS ${fdt_args[$3]}"
    echo "BOOT_ENV $dom0_bootargs"
    echo "BOOT_GUESTS${fdt_guests[$3]}"
    echo "BOOT_SKIP ${xenova_skip_boot_guests:-}"
}
sleep() { :; }
'''
boot_cases = [
    ('bootflow-mmc0', {'devtype': 'mmc', 'devnum': '0'}, False),
    ('bootflow-mmc1', {'devtype': 'mmc', 'devnum': '1'}, False),
    ('manual-mmc0-fallback', {}, False),
    ('debug-console-opt-in', {'xenova_debug': '1'}, False),
    ('reject-usb', {'devtype': 'usb', 'devnum': '0'}, True),
    ('stop-on-fdt-copy-failure', {'FAIL': 'fdt-move'}, True),
    ('stop-on-sdio-dmas-removal-failure', {'FAIL': 'fdt-rm-dmas'}, True),
    ('stop-on-sdio-dma-names-removal-failure', {'FAIL': 'fdt-rm-dma-names'}, True),
    ('stop-on-partuuid-failure', {'FAIL': 'part'}, True),
    ('stop-on-xen-load-failure', {'FAIL': 'xen'}, True),
    ('stop-on-kernel-load-failure', {'FAIL': 'Image'}, True),
    ('load-factory-domains', {'GUEST_SCRIPT': '1'}, False),
    ('skip-factory-domains', {'GUEST_SCRIPT': '1', 'xenova_skip_boot_guests': '1'}, False),
    ('fallback-on-overlapping-base-fdt', {'GUEST_SCRIPT': '1', 'fdt_addr_r': '0x07800000'}, False),
    ('fallback-on-guest-script-load-failure', {'GUEST_SCRIPT': '1', 'FAIL': 'xenova-guests.scr'}, False),
    ('fallback-on-oversized-guest-script', {'GUEST_SCRIPT': '1', 'SCRIPT_FILESIZE': '100001'}, False),
    ('fallback-on-guest-fdt-copy-failure', {'GUEST_SCRIPT': '1', 'FAIL': 'fdt-copy-guest'}, False),
    ('fallback-on-invalid-guest-script-header', {'GUEST_SCRIPT': '1', 'FAIL': 'source-header'}, False),
    ('fallback-after-partial-guest-fdt-changes', {'GUEST_SCRIPT': '1', 'FAIL': 'source'}, False),
]
for name, values, failure in boot_cases:
    env = {**os.environ, 'devtype': '', 'devnum': '', 'FAIL': '', 'xenova_debug': '',
           'fdtcontroladdr': '0x3aff0000', 'fdt_addr_r': '0x05600000',
           'GUEST_SCRIPT': '', 'SCRIPT_FILESIZE': '', 'xenova_skip_boot_guests': '', **values}
    result = subprocess.run(['bash', '-c', boot_mocks + boot], env=env, text=True, capture_output=True, timeout=5)
    assert (result.returncode != 0) == failure, (name, result.stdout, result.stderr)
    if failure:
        assert 'BOOT ' not in result.stdout, (name, result.stdout)
    else:
        devnum = values.get('devnum', '0')
        assert f'PART uuid mmc {devnum}:2 rootfs_partuuid' in result.stdout
        assert f'LOAD mmc {devnum}:1 0x00200000 xen' in result.stdout
        assert f'LOAD mmc {devnum}:1 0x00400000 Image' in result.stdout
        lines = result.stdout.splitlines()
        early = name == 'load-factory-domains'
        fdt_address = '0x07800000' if early else env['fdt_addr_r']
        assert [line for line in lines if line.startswith('BOOT ')] == [f'BOOT 0x00200000 - {fdt_address}']
        boot_args = next(line.removeprefix('BOOT_ARGS ') for line in lines if line.startswith('BOOT_ARGS '))
        assert f'BOOT_ENV {boot_args}' in lines
        assert ('xenova.dom0less=1' in boot_args) == early
        assert f'BOOT_SKIP {values.get("xenova_skip_boot_guests", "")}' in lines
        assert next(line for line in lines if line.startswith('BOOT_GUESTS')) == (
            'BOOT_GUESTS debian-dev zephyr-drive-a zephyr-drive-b' if early else 'BOOT_GUESTS')
        if early:
            assert result.stdout.index('SOURCE 0x07000000') < result.stdout.index('BOOT ')
        if name == 'skip-factory-domains':
            assert 'SOURCE ' not in result.stdout
        if name.startswith('fallback-'):
            fallback_log = {
                'fallback-on-overlapping-base-fdt': 'base Device Tree address is outside the early boot layout',
                'fallback-on-guest-script-load-failure': 'xenova-guests.scr load failed',
                'fallback-on-oversized-guest-script': 'xenova-guests.scr load failed',
                'fallback-on-guest-fdt-copy-failure': 'guest Device Tree copy failed',
                'fallback-on-invalid-guest-script-header': 'xenova-guests.scr execution failed',
                'fallback-after-partial-guest-fdt-changes': 'xenova-guests.scr execution failed',
            }[name]
            assert result.stdout.count('booting dom0 only') == 1
            assert result.stdout.index(fallback_log) < result.stdout.index('BOOT ')
            assert result.stdout.count('SOURCE ') == int('execution failed' in fallback_log)
        if name == 'fallback-after-partial-guest-fdt-changes':
            assert result.stdout.index('FDT mknode /chosen debian-dev') < result.stdout.index('booting dom0 only')
        if name == 'fallback-on-overlapping-base-fdt':
            assert 'LOAD mmc 0:1 0x07000000 xenova-guests.scr' not in result.stdout
        xen_args = next(line.split() for line in result.stdout.splitlines()
                        if line.startswith('FDT set /chosen xen,xen-bootargs '))
        assert [arg for arg in xen_args if arg.startswith('dom0_max_vcpus=')] == ['dom0_max_vcpus=2'], (name, xen_args)
        assert not any(arg.startswith('dom0_vcpus_pin') for arg in xen_args), (name, xen_args)
        dom0_args = next(line.split() for line in result.stdout.splitlines()
                         if line.startswith('FDT set /chosen xen,dom0-bootargs '))
        debug = values.get('xenova_debug') == '1'
        assert [arg for arg in dom0_args if arg.startswith('loglevel=')] == [
            'loglevel=7' if debug else 'loglevel=4'], (name, dom0_args)
        assert ('systemd.log_target=console' if debug else
                'systemd.log_target=journal-or-kmsg') in dom0_args, (name, dom0_args)
        assert ('earlycon=xenboot' in dom0_args) == debug, (name, dom0_args)
        assert result.stdout.index(f'FDT move 0x3aff0000 {env["fdt_addr_r"]}') < result.stdout.index('FDT set ')
        assert result.stdout.index('FDT move ') < result.stdout.index('FDT rm /soc/mmcnr@7e300000 dmas')
        assert result.stdout.index('FDT rm /soc/mmcnr@7e300000 dmas') < result.stdout.index('FDT rm /soc/mmcnr@7e300000 dma-names')
        assert result.stdout.index('FDT rm /soc/mmcnr@7e300000 dma-names') < result.stdout.index('LOAD ')
        assert 'FDT rm /soc/mmc@7e340000' not in result.stdout
        assert 'FDT rm /soc/dma' not in result.stdout
    print('PASS', name)

init = (ROOT / 'recipes-core/xenova-domu-init/files/init').read_text().split('\nwhile true; do', 1)[0]
init_mocks = r'''
mock_mount() {
    echo "MOUNT $2 $4"
    [ -d "$4" ] || return 1
    [ "$FAIL_MOUNT" != "$2" ] || return 1
    if [ "$2" = sysfs ] && [ "$NIC_DELAY" -eq 0 ]; then mkdir -p "$TEST_ROOT/sys/class/net/eth0"; fi
}
uname() { :; }
ip() {
    echo "IP $*"
    [ "$*" != 'link set eth0 up' ] || [ "$FAIL_LINK" -eq 0 ]
}
sleep() {
    waits=$((waits + 1))
    echo "WAIT $waits"
    if [ "$waits" -eq "$NIC_DELAY" ]; then mkdir -p "$TEST_ROOT/sys/class/net/eth0"; fi
}
udhcpc() { echo "DHCP $*"; return "$DHCP_RESULT"; }
waits=0
'''
for name, values, dhcp, waits in [
    ('nic-ready-after-mounts', {}, True, 0),
    ('nic-delayed-3s', {'NIC_DELAY': '3'}, True, 3),
    ('nic-missing-bounded-20s', {'NIC_DELAY': '999'}, False, 20),
    ('proc-mount-failure', {'FAIL_MOUNT': 'proc'}, False, 0),
    ('sysfs-mount-failure', {'FAIL_MOUNT': 'sysfs'}, False, 0),
    ('devtmpfs-mount-failure', {'FAIL_MOUNT': 'devtmpfs'}, False, 0),
    ('eth0-up-failure', {'FAIL_LINK': '1'}, False, 0),
    ('dhcp-failure-reaches-shell', {'DHCP_RESULT': '1'}, True, 0),
]:
    with tempfile.TemporaryDirectory(prefix='xenova-init-test-') as temp:
        script = re.sub(r'/(proc|sys|dev|run|tmp)(?=[/\s])', lambda m: temp + m.group(), init)
        script = script.replace('/bin/mount', 'mock_mount')
        env = {**os.environ, 'TEST_ROOT': temp, 'NIC_DELAY': '0', 'FAIL_MOUNT': '', 'FAIL_LINK': '0', 'DHCP_RESULT': '0', **values}
        result = subprocess.run(['sh', '-c', init_mocks + script], env=env, text=True, capture_output=True, timeout=5)
        assert result.returncode == 0, (name, result.stdout, result.stderr)
        assert ('DHCP -f ' in result.stdout) == dhcp, (name, result.stdout, result.stderr)
        assert result.stdout.count('WAIT ') == waits, (name, result.stdout)
        assert 'Minimal guest shell' in result.stdout
        if dhcp:
            assert result.stdout.index('MOUNT devtmpfs') < result.stdout.index('IP link set eth0 up') < result.stdout.index('DHCP -f ')
            assert '-n -q -t 5 -T 2' in result.stdout
        if values.get('DHCP_RESULT') == '1':
            assert 'DHCP did not succeed' in result.stdout
        print('PASS', name)

rules = []
for filename in ['10-ether.network', '10-xenbr0.network']:
    config = configparser.ConfigParser()
    config.read(ROOT / 'recipes-extended/xen/files' / filename)
    assert config['Link']['Unmanaged'] == 'yes'
    assert 'Network' not in config
    rules.append(config['Match']['Name'])
assert not any(fnmatch.fnmatchcase('eth0', pattern) for pattern in rules)
assert any(fnmatch.fnmatchcase('vif1.0', pattern) for pattern in rules)
assert any(fnmatch.fnmatchcase('xenbr0', pattern) for pattern in rules)
print('PASS network-rules-match-only-vif-and-xenbr0')
print(f'PASS {len(boot_cases) + 9} boot/init/network regression cases (hardware not exercised)')
