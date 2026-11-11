"""실제 이미지 검사기로 누락·충돌한 CPU/클럭 부팅 설정을 거절한다."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('check_wic_profile', ROOT / 'platform/scripts/check-xenova-wic.py')
wic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wic)

SCRIPT = 'fdt set /chosen xen,xen-bootargs "console=dtuart dom0_max_vcpus=2 dom0_mem=3072M" || exit 1\n'
CONFIG = '#force_turbo=0\n[all]\nforce_turbo=1\n'


class Dom0BootProfileTest(unittest.TestCase):
    def test_approved_profile_passes(self):
        wic.check_dom0_profile(CONFIG, SCRIPT)

    def test_absent_disabled_conditional_or_conflicting_turbo_is_rejected(self):
        for config in ['', '#force_turbo=1\n', '[all]\nforce_turbo=0\n',
                       '[pi4]\nforce_turbo=1\n', CONFIG+'[pi4]\nforce_turbo=0\n',
                       CONFIG+'force_turbo=1\n']:
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, 'force_turbo'):
                wic.check_dom0_profile(config, SCRIPT)

    def test_real_xen_argument_requires_two_without_duplicate_or_direct_pinning(self):
        for script in [SCRIPT.replace('dom0_max_vcpus=2 ', ''),
                       SCRIPT.replace('dom0_max_vcpus=2', 'dom0_max_vcpus=4'),
                       SCRIPT.replace('dom0_max_vcpus=2', 'dom0_max_vcpus=2 dom0_max_vcpus=4'),
                       '#'+SCRIPT, SCRIPT.replace('console=dtuart', 'console=dtuart dom0_vcpus_pin=1')]:
            with self.subTest(script=script), self.assertRaises(ValueError):
                wic.check_dom0_profile(CONFIG, script)


if __name__ == '__main__':
    unittest.main()
