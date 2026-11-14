# xen-tools-net-conf의 광범위한 NIC 브릿지 편입과 자동 브릿지 생성을 대체한다.
# meta-xenova에서 xen-tools를 사용하는 이미지 전체에 적용된다.
FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# 브릿지는 xenova-bridge.service가 생성하고 고정 IP를 설정한다.
# virt_networking.bbclass로 비관리 규칙과 USB 전화의 주소 전용 정책을 설치한다.
VIRT_NETWORKING_FILES = "${UNPACKDIR}/10-ether.network ${UNPACKDIR}/10-xenbr0.network ${UNPACKDIR}/20-xenova-usb-phone.network"
SRC_URI:remove = "file://10-xenbr0.netdev"
SRC_URI += "file://20-xenova-usb-phone.network"
SRC_URI += "file://0003-dom0less-factory-registration.patch"
CONFFILES:${PN}-net-conf += "${sysconfdir}/systemd/network/20-xenova-usb-phone.network"
SRC_URI += "file://0002-console-filter-xenova-evidence.patch file://xenova_console_evidence.h file://xenova_console_evidence_file.h file://xenova_console_evidence_async.h"
python do_patch:append() {
    import shutil
    shutil.copyfile(d.getVar('UNPACKDIR') + '/xenova_console_evidence.h',
                    d.getVar('S') + '/tools/console/daemon/xenova_console_evidence.h')
    shutil.copyfile(d.getVar('UNPACKDIR') + '/xenova_console_evidence_file.h',
                    d.getVar('S') + '/tools/console/daemon/xenova_console_evidence_file.h')
    shutil.copyfile(d.getVar('UNPACKDIR') + '/xenova_console_evidence_async.h',
                    d.getVar('S') + '/tools/console/daemon/xenova_console_evidence_async.h')
}
