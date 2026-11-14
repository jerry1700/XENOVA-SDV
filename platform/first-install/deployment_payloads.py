"""dom0 API·MQTT·Gateway·실행 로그 설치 소스 계약. 조립 시 과거 payload를 거부한다."""
from pathlib import Path
import stat

from prepare_rootfs import inside

ROOT = Path(__file__).resolve().parents[2]
RECIPES = ROOT / "platform/yocto/meta-xenova/recipes-core"
API = ROOT / "platform/services/dom0-manager"
MQTT = ROOT / "platform/services/vm-agent/mqtt-client"
GATEWAY = ROOT / "platform/services/gateway"
WORLD = ROOT / "platform/apps/drive/world/python"
DISPLAY = ROOT / "platform/services/display/router"
RUNTIME_LOGS = RECIPES / "xenova-dom0-manager/files/runtime-logs"
GATEWAY_FILES = RECIPES / "xenova-gateway/files"
COLLECTOR = "xenova-runtime-log-collector.service"
LOG_ENV = "etc/xenova/runtime-logs/collector.env"


def source_files(site_packages):
    """배포 대상 소스와 rootfs 경로를 연결한다. 실행·파일 변경은 하지 않는다."""
    result = {}
    for project, package in ((API, "dom0_manager"), (MQTT, "xenova_mqtt"),
                              (GATEWAY, "xenova_gateway"), (WORLD, "xenova_drive_world"),
                              (DISPLAY, "xenova_display")):
        if not (project/package/"__init__.py").is_file():
            raise ValueError("required deployment source package missing: " + package)
        for source in sorted((project/package).rglob("*.py")):
            result[str(Path(site_packages)/package/source.relative_to(project/package))] = source
    result.update({
        "usr/lib/systemd/system/xenova-dom0-manager.service": RECIPES/"xenova-dom0-manager/files/xenova-dom0-manager.service",
        "usr/lib/systemd/system/xenova-recovery-worker.service": API/"deploy/xenova-recovery-worker.service",
        "usr/lib/systemd/system/xenova-recovery-config.service": API/"deploy/xenova-recovery-config.service",
        "etc/xenova/dom0-manager.env": RECIPES/"xenova-dom0-manager/files/dom0-manager.env",
        "usr/lib/systemd/system/" + COLLECTOR: RUNTIME_LOGS/COLLECTOR,
        "usr/lib/systemd/system/xenova-dom0-manager.service.d/30-runtime-logs.conf": RUNTIME_LOGS/"runtime-logs-manager.conf",
        LOG_ENV: RUNTIME_LOGS/"runtime-log-db.env",
        "usr/share/xenova/dom0-manager-pyproject.toml": API/"pyproject.toml",
        "usr/share/xenova/mqtt-client-pyproject.toml": MQTT/"pyproject.toml",
        "usr/lib/systemd/system/xenova-gateway.service": GATEWAY_FILES/"xenova-gateway.service",
        "usr/share/xenova/gateway.env.example": GATEWAY_FILES/"gateway.env.example",
        "usr/share/xenova/drive-rejoin.example.json": API/"deploy/drive-rejoin.example.json",
        **{"usr/share/xenova/"+name: GATEWAY/"deploy"/name for name in (
            "90-esp32-serial.conf.example", "90-xenova-esp32.rules.example", "90-drive-rejoin.conf.example")},
        "usr/lib/systemd/system/xenova-display-feed.service": RECIPES/"xenova-display/files/xenova-display-feed.service",
        "usr/lib/systemd/system/xenova-display-guest-relay.service": DISPLAY/"deploy/guest-display/xenova-display-guest-relay.service",
        "usr/share/xenova/display/examples/guest-display-data-only.json.example": DISPLAY/"deploy/guest-display/guest-display-data-only.json.example",
        "usr/lib/xenova/xenova_display_setup.py": RECIPES/"xenova-platform/files/xenova_display_setup.py",
        "usr/sbin/xenova-display-setup": RECIPES/"xenova-platform/files/xenova-display-setup",
        "usr/lib/systemd/system/xenova-display-check.service": RECIPES/"xenova-platform/files/xenova-display-check.service",
    })
    return result


def installed_sources(root):
    """필수 런타임·enable 링크를 확인하고 소스 일치 검사 목록을 반환한다."""
    def path(name):
        return inside(root, name, create_parents=False)

    sites = []
    for candidate in path("usr/lib").glob("python3*/site-packages"):
        relative = str(candidate.relative_to(root))
        if path(relative+"/dom0_manager/__init__.py").is_file() and path(relative+"/xenova_mqtt/__init__.py").is_file():
            sites.append(relative)
    if len(sites) != 1:
        raise ValueError("required dom0 API/MQTT payload missing or ambiguous; rebuild dom0")
    site = sites[0]
    for name in ("fastapi/__init__.py", "uvicorn/__init__.py", "paho/mqtt/client.py",
                 "pydantic/__init__.py", "starlette/__init__.py", "anyio/__init__.py"):
        if not path(site+"/"+name).is_file():
            raise ValueError("required API/MQTT runtime dependency missing: "+name)
    if not path("usr/bin/python3").is_file() or not path("usr/bin/python3").stat().st_mode & 0o111:
        raise ValueError("required Python runtime missing")
    wrapper = path('usr/sbin/xenova-display-setup')
    if not wrapper.is_file() or stat.S_IMODE(wrapper.stat().st_mode) != 0o755:
        raise ValueError('Display setup entrypoint must be installed executable (0755)')
    for unit, source in (("xenova-dom0-manager.service", RECIPES/"xenova-dom0-manager/files/xenova-dom0-manager.service"),
                         ("xenova-recovery-worker.service", API/"deploy/xenova-recovery-worker.service")):
        link = root/"etc/systemd/system/multi-user.target.wants"/unit
        if not link.is_symlink() or path(str(link.relative_to(root))).read_bytes() != source.read_bytes():
            raise ValueError("required dom0 service not enabled: " + unit)
    if stat.S_IMODE(path("etc/xenova/dom0-manager.env").stat().st_mode) != 0o600:
        raise ValueError("dom0 API environment must be root-only (0600)")
    for unit in ('xenova-display-feed.service', 'xenova-display-guest-relay.service', 'xenova-display-check.service'):
        link = root/'etc/systemd/system/multi-user.target.wants'/unit
        if not link.is_symlink() or path(str(link.relative_to(root))) != path('usr/lib/systemd/system/'+unit):
            raise ValueError('required Display service not enabled: '+unit)
    for relative in ('etc/xenova/display.env', 'etc/xenova/display-bindings.json',
                     'etc/xenova/guest-display.json', 'etc/xenova/guest-display/guest-data.token'):
        if (root/relative).is_symlink() or path(relative).exists():
            raise ValueError('common image must not contain board Display configuration or credentials')
    for relative in (
        'etc/xenova/gateway.env', 'etc/xenova/drive-rejoin.json',
        'etc/xenova/vehicle-limits.json', 'etc/xenova/gateway-odometry.json', 'etc/xenova/gateway-feedback3.json',
        'etc/systemd/system/xenova-gateway.service.d/90-esp32-serial.conf',
        'etc/systemd/system/xenova-recovery-worker.service.d/90-drive-rejoin.conf',
        'etc/udev/rules.d/90-xenova-esp32.rules',
    ):
        if (root/relative).is_symlink() or path(relative).exists():
            raise ValueError('common image contains board gateway settings: ' + relative)
    collector_link = "etc/systemd/system/multi-user.target.wants/" + COLLECTOR
    if not (root/collector_link).is_symlink() or path(collector_link) != path("usr/lib/systemd/system/" + COLLECTOR):
        raise ValueError("required dom0 service not enabled: " + COLLECTOR)
    log_env = path(LOG_ENV)
    if log_env.is_file() and stat.S_IMODE(log_env.stat().st_mode) != 0o600:
        raise ValueError("runtime log environment must be root-only (0600): " + LOG_ENV)
    if (root/"etc/xenova/dom0-manager-token.env").exists() or (root/"etc/xenova/dom0-manager-token.env").is_symlink():
        raise ValueError("common image must not contain a board service token")
    return source_files(site)
