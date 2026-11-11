# Xen dom0에서 CPUFreq가 등록되지 않는 Pi 4의 승인된 정규 클럭 프로필.
# 최대 주파수·전압·온도/저전압 보호는 firmware 기본값을 유지한다.
RPI_EXTRA_CONFIG:append:raspberrypi4-64 = "\n[all]\nforce_turbo=1\n"
