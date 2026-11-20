# Xenova
> Xen 기반 임베디드 멀티 OS 관리 및 장애 대응 플랫폼

Xenova는 Raspberry Pi 4에 오픈소스 하이퍼바이저 Xen을 브링업하고, Linux와 Zephyr RTOS를 하나의 하드웨어에서 독립적으로 실행·관리할 수 있도록 구축한 임베디드 가상화 플랫폼입니다.

Yocto 기반 시스템 이미지부터 VM 템플릿과 배포, 가상머신의 생명주기·자원 관리, 상태 모니터링, 원격 관리 인터페이스까지 통합하여 멀티 OS 운영 환경을 제공합니다.

![XENOVA_overview.png](docs\images\readme\XENOVA_overview.png)

또한 자율주행 RC카를 응용 사례로 활용하여, 서로 다른 OS와 VM에 분산된 기능의 격리·협력·장애 대응을 실증합니다. 주행 VM 장애 시 예비 VM으로의 제어권 전환, 인지 시스템 장애 시 주행 모드 전환, 장애 VM의 자동 복구와 재합류를 주요 검증 목표로 삼습니다.

## 목차
[1. 시스템 구성 (System Overview)](#1-시스템-구성-system-overview)
[- 1.1. 전체 시스템 구성도](#11-전체-시스템-구성도)  
[- 1.2. 하드웨어 사양 및 역할](#12-하드웨어-사양-및-역할)  
[- 1.3. 기술 스택](#13-기술-스택)  

[2. 시스템 아키텍처 (Architecture)](#2-시스템-아키텍처-architecture) 
[- 2.1. 전체 소프트웨어 구조](#21-전체-소프트웨어-구조)  
[- 2.2. Xen 및 멀티 OS 플랫폼](#22-xen-및-멀티-os-플랫폼)  
[- 2.3. VM 관리 및 복구 계층](#23-vm-관리-및-복구-계층)  
[- 2.4. 차량 제어 및 Gateway](#24-차량-제어-및-gateway)  
[- 2.5. 모니터링 및 외부 관리](#25-웹-관리-및-화면-표시)  
[- 2.6. 통신 구조](#26-통신-구조)  

[3. 주요 기능 (Key Features)](#3-주요-기능-key-features)  
[- 3.1. 멀티 OS 실행 및 VM 관리](#31-멀티-os-실행-및-vm-관리)  
[- 3.2. 리소스 할당 및 제어](#32-리소스-할당-및-제어)  
[- 3.3. 상태 모니터링 및 진단](#33-상태-모니터링-및-진단)  
[- 3.4. 장애 감지 및 자동 복구](#34-장애-감지-및-자동-복구)  
[- 3.5. 주행 제어권 전환 및 모드 전환](#35-주행-제어권-전환-및-모드-전환)  
[- 3.6. 웹 기반 관리 및 시각화](#36-웹-기반-관리-및-시각화)  

[4. 장애 대응 시나리오 (Scenarios)](#4-장애-대응-시나리오-scenarios)  
[- 4.1. Jetson 인지 장애](#41-jetson-인지-장애)  
[- 4.2. 활성 Zephyr VM 장애](#42-활성-zephyr-vm-장애)  
[- 4.3. Zephyr 애플리케이션 장애](#43-zephyr-애플리케이션-장애)  
[- 4.4. 인지 장애와 활성 VM 장애 동시 발생](#44-인지-장애와-활성-vm-장애-동시-발생)  
[- 4.5. Jetson 인지 기능 회복](#45-jetson-인지-기능-회복)  
[- 4.6. 장애 VM 복구 및 Standby 재합류](#46-장애-vm-복구-및-standby-재합류)  
[- 4.7. 양쪽 Zephyr VM 사용 불가](#47-양쪽-zephyr-vm-사용-불가)   

[5. 설치 및 사용 (Getting Started)]()
[- 5.1 실행 환경 및 사전 준비]()  
[- 5.2 빌드 및 배포]()  
[- 5.3 실행 및 관리 방법]()  
[- 5.4 상세 기술 문서]()  

## 1. 시스템 구성 (System Overview)
### 1.1. 전체 시스템 구성도  
![overview.png](docs\images\readme\overview.png)
Xenova는 **Xen 기반 멀티 OS 플랫폼**을 중심으로 이를 제어 및 시각화하는 **웹 서비스**와 이를 통해 구동되는 **차량 시스템**으로 구성됩니다.

| | |
| --- | --- |
| Web Management Console | VM 제어, 시스템 상태 모니터링 및 장애·복구 과정 시각화 |
| Vehicle Display | 차량 상태를 계기판·인포테인먼트 화면으로 표시 |
| Jetson | 카메라 기반 환경 인지, 지도 생성 및 차량 위치 추정 |
| Raspberry Pi 4 | Xen 기반 멀티 OS 실행·관리, VM 장애 대응 및 제어권 중재, 주행 판단 |
| ESP32 | 모터·조향 제어, 센서 피드백 |

### 1.2. 하드웨어 사양 및 역할  
| 구분 | 하드웨어 | 주요 역할 |
|---|---|---|
| 중앙 컴퓨팅 | Raspberry Pi 4 Model B (8GB) | Xen 하이퍼바이저 구동, 멀티 OS 관리 및 주행 제어 |
| 환경 인지 | NVIDIA Jetson Nano B01 (4GB) | 영상 처리, 환경 인지, 지도 및 위치 정보 생성 |
| 카메라 | Luxonis OAK-D-Lite | RGB·Depth 영상 수집 |
| 하위 제어 | ESP32 DevKitC V4 | 모터·서보 제어, 엔코더 수집, 명령 유효기간 감시 |
| 구동계 | RC카 섀시, 모터·엔코더, 조향 서보 | 차량 구동 및 주행 상태 피드백 |
| 근거리 센서 | VL53L0X ToF 센서 | 전후방 근거리 거리 측정 |
| 차량 화면 | HDMI 모니터 / USB 연결 휴대폰 | 계기판 및 인포테인먼트 화면 출력 |

웹 콘솔은 별도의 웹 서버와 사용자 브라우저를 통해 제공합니다.

### 1.3 기술 스택  
| 영역 | 주요 기술 |
|---|---|
| Hypervisor & OS | Xen (ARM64), Yocto, Linux, Zephyr RTOS, Debian |
| VM Management | Python, FastAPI, Xen Toolstack (`xl`, XenStore) |
| Backend | Java, Spring Boot, MySQL, Redis |
| Frontend | React, TypeScript |
| Communication | Xen PV Network, MQTT (Mosquitto), REST API, WebSocket, USB Serial |
| Perception | Python, DepthAI, OpenCV |
| Embedded Control | Zephyr (C), ESP32 (Arduino/C++) |
| Display | React, Chromium, Xorg |
| Build & Deployment | BitBake, Docker, Docker Compose, Jenkins, AWS EC2 |

## 2. 시스템 아키텍처 (Architecture)

### 2.1 전체 소프트웨어 구조

![Xenova Architecture](docs/images/readme/architecture.png)

Xenova는 Raspberry Pi 4의 **Xen 기반 가상화 환경을 중심으로**, VM 관리·복구·차량 제어·화면 표시 기능의 책임을 분리하여 구성했습니다.

관리 도메인(dom0)은 VM 운영과 장애 대응, 주행 명령 중재 및 화면 출력 서비스를 담당합니다. 게스트 도메인(domU)은 각각 독립된 운영체제 환경에서 주행 제어 또는 사용자 상호작용 기능을 실행합니다.

외부 웹 서비스는 VM 관리와 시스템 상태 조회를 제공하며, Jetson과 ESP32는 각각 환경 인지와 물리적 구동을 담당합니다.

### 2.2 Xen 및 멀티 OS 플랫폼

Raspberry Pi 4에 Xen 하이퍼바이저를 구성하고, Yocto 기반 Linux 관리 도메인(dom0)과 Linux·Zephyr RTOS 게스트 도메인(domU)을 실행합니다.

| 구성 | 역할 |
|---|---|
| Xen Hypervisor | 하드웨어 자원 가상화, VM 실행 격리 및 vCPU 스케줄링 |
| dom0 (Yocto Linux)** | Xen Toolstack, VM 관리 서비스, 가상 네트워크 및 장치 I/O 관리 |
| Zephyr A / B | 독립적으로 실행되는 주행 제어 VM. 동일한 주행 펌웨어를 기반으로 Active/Standby 역할 수행 |
| Debian | Interaction 및 차량 화면 애플리케이션을 실행하는 Linux VM |

플랫폼은 VM별 자산과 실행 신원을 구분하여 관리하며, Xen의 가상 네트워크를 통해 게스트 간 통신 환경을 제공합니다.

### 2.3 VM 관리 및 복구 계층

dom0의 VM 관리·복구 구조는 **Platform Manager, FastAPI(dom0-manager), Recovery Worker**로 구성됩니다.
 
| 구성요소 | 책임 |
|---|---|
| Platform Manager | VM 생성·실행·정지, 자산 및 실행 의도 관리, Xen 제어 |
| FastAPI (dom0-manager) | 플랫폼 관리 기능을 내부 REST API와 WebSocket으로 제공 |
| Recovery Worker | Xen·Heartbeat 기반 장애 감시, 복구 정책 실행, VM 재시작 및 재합류 지원 |

Platform Manager는 VM 실행과 자산 관리의 공통 기반을 제공하며, Recovery Worker는 이를 이용해 장애 복구 작업을 수행합니다.

### 2.4 차량 제어 및 Gateway

환경 인지, 주행 판단, 하위 구동 제어를 서로 다른 컴퓨팅 영역에 배치했습니다.

- **Jetson:** 카메라 기반 환경 인지, 지도 및 위치 정보 생성
- **Zephyr Drive VM:** 인지 정보와 차량 상태를 기반으로 주행 경로·행동 판단 및 제어 명령 생성
- **Gateway (dom0):** 활성 주행 VM과 주행 모드 결정, 제어권 관리, 명령 검증 및 중재
- **ESP32:** 유효한 구동 명령에 따른 모터·조향 제어 및 센서 피드백

Gateway는 차량 제어권의 기준을 관리하고, 현재 허용된 VM의 명령만 하위 제어 장치에 전달합니다.

### 2.5 웹 관리 및 화면 표시

Xenova는 외부 웹 관리 콘솔과 차량 내부 디스플레이를 분리하여 구성합니다.

**웹 관리 시스템**

React 기반 관리 콘솔과 Spring Boot 백엔드를 통해 VM 관리, 자원 모니터링, 장애·복구 이력 및 주행 상태를 제공합니다. 백엔드는 dom0-manager API와 연동하여 실제 VM 관리 기능에 접근합니다.

**차량 화면 시스템**

dom0의 Display Feed가 VM 및 차량 상태를 수집·정리하고, Debian Interaction VM이 계기판·인포테인먼트 웹 화면을 제공합니다.

화면은 출력 모드에 따라 dom0 Chromium 키오스크를 이용한 HDMI 모니터 또는 USB 연결 휴대폰의 브라우저에서 표시됩니다.

### 2.6 통신 구조

각 구성요소는 관리, 상태 관측, 인지 데이터 전달, 물리 제어 등 목적에 따라 서로 다른 통신 경로를 사용합니다.

| 통신 구간 | 주요 방식 | 목적 |
|---|---|---|
| Web Console ↔ Spring Boot | REST API, WebSocket | 사용자 요청 및 실시간 상태 제공 |
| Spring Boot ↔ dom0-manager | REST API, WebSocket | VM 관리 요청 및 콘솔 연결 |
| dom0 ↔ domU | Xen 가상 네트워크, MQTT | 게스트 통신, 상태 보고 및 애플리케이션 메시지 |
| Jetson ↔ Raspberry Pi | 네트워크 기반 통신 | 환경 인지·지도·차량 상태 정보 교환 |
| Gateway ↔ ESP32 | USB Serial | 구동 명령, 센서 피드백 및 장치 상태 |
| dom0 ↔ Interaction VM | 가상 네트워크, HTTP | 차량 화면 데이터 및 웹 콘텐츠 제공 |

## 3. 주요 기능 (Key Features)
> 참고: [CLI 문서](docs\platform\cli-manager-spec.md)

### 3.1 멀티 OS 실행 및 VM 관리

- Xen 기반 가상화 환경에서 Linux와 Zephyr RTOS를 독립적인 VM으로 실행하고, CLI 및 웹 관리 콘솔을 통해 가상머신을 관리합니다.

![XENOVA_vmcontrol.png](docs\images\readme\XENOVA_vmcontrol.png)
![XENOVA_console.png](docs\images\readme\XENOVA_console.png)

- 템플릿 카탈로그를 기반으로 VM을 생성하며, 시작·정지·재부팅과 등록 자산의 보관·복원 기능을 제공합니다.

![XENOVA_vmcreate.png](docs\images\readme\XENOVA_vmcreate.png)

### 3.2 리소스 할당 및 제어

- VM별 vCPU·메모리 자원 설정과 CPU 배치 정책을 제공합니다.
- vCPU와 물리 CPU의 배치를 설정하고 실제 Xen 실행 상태와 대조할 수 있습니다.

![XENOVA_vmdetail1.png](docs\images\readme\XENOVA_vmdetail1.png)
![XENOVA_vmdetail2.png](docs\images\readme\XENOVA_vmdetail2.png)

### 3.3 상태 모니터링 및 진단

- Xen 실행 상태와 게스트 OS·애플리케이션의 상태를 수집하여 VM별 운영 정보를 제공합니다.

![XENOVA_overview.png](docs\images\readme\XENOVA_overview.png)

- CPU·메모리·네트워크 등의 자원 정보와 Agent·애플리케이션의 상태 보고를 구분하여 표시합니다.

![XENOVA_cpu.png](docs\images\readme\XENOVA_cpu.png)
![XENOVA_vmmemory.png](docs\images\readme\XENOVA_vmmemory.png)
![XENOVA_network.png](docs\images\readme\XENOVA_network.png)

### 3.4 장애 감지 및 자동 복구

- Recovery Worker가 Xen 실행 상태와 게스트의 생존 신호를 감시하여 VM 장애를 판정합니다.
- 비정상 종료와 사용자의 의도적인 정지를 구분하고, 복구 정책에 따라 재시작을 수행합니다.

![XENOVA_timeline.png](docs\images\readme\XENOVA_timeline.png)

### 3.5 주행 제어권 전환 및 모드 전환

- 동일한 주행 기능을 가진 두 Zephyr VM을 Active/Standby 구조로 운용하여 주행 제어 기능의 장애 대응을 지원합니다.
- 활성 VM에 장애가 발생하면 Gateway가 기존 제어권을 무효화하고 준비된 예비 VM으로 제어권을 전환합니다. 또한 환경 인지 기능의 장애는 VM 장애와 구분하여 AUTO/MANUAL 모드 전환으로 대응합니다.
-  유효한 제어권이나 주행 입력이 확보되지 않으면 주행 명령을 차단하고 안전 정지 상태로 전환합니다.

![XENOVA_timeline2.png](docs\images\readme\XENOVA_timeline2.png)

### 3.6 웹 기반 관리 및 시각화

- React 기반 웹 관리 콘솔에서 VM 관리, 리소스 현황, 장애·복구 이력 및 차량 주행 상태를 확인할 수 있습니다.
- 별도의 차량 화면 시스템은 Debian Interaction VM이 제공하는 계기판·인포테인먼트 화면을 HDMI 모니터 또는 USB 연결 휴대폰에 표시합니다.
- 관리 콘솔과 차량 디스플레이를 분리하여 운영·제어 기능과 차량 정보 표시 기능을 각각 제공합니다.

![XENOVA-instrument.png](docs\images\readme\XENOVA-instrument.png)
![XENOVA_infotainment.png](docs\images\readme\XENOVA_infotainment.png)

## 4. 장애 대응 시나리오 (Scenarios)

Xenova는 두 개의 Zephyr VM이 동일한 AUTO/MANUAL 주행 기능을 수행할 수 있는 이중화 구조를 기반으로, 장애 발생 시 **주행 모드 전환, VM 제어권 이전, 자동 복구 및 안전 정지**를 수행하도록 설계했습니다.

인지 기능의 장애와 VM 자체의 장애를 구분하며, 장애 상황에 따라 다음 7가지 시나리오로 대응합니다.

### 4.1. Jetson 인지 장애

자율주행 중 Jetson의 인지 기능에 문제가 발생하면, 현재 활성 VM을 유지한 채 AUTO에서 MANUAL 모드로 전환합니다. 수동주행 준비가 완료되지 않았다면 안전 정지합니다.

### 4.2. 활성 Zephyr VM 장애

현재 차량을 제어하는 Zephyr VM에 장애가 발생하면, 준비된 예비 VM으로 제어권을 이전합니다. 가능한 경우 기존 주행 모드를 유지하며, 장애 VM은 별도로 복구합니다.

### 4.3. Zephyr 애플리케이션 장애

VM 자체는 실행 중이지만 내부 주행 애플리케이션이 응답하지 않는 상황입니다. VM 장애와 애플리케이션 장애를 구분하여 예비 VM으로의 전환과 지원되는 앱·VM 복구 방법을 선택합니다.

### 4.4. 인지 장애와 활성 VM 장애 동시 발생

Jetson 인지 기능과 활성 주행 VM에 동시에 장애가 발생하면, 준비된 예비 VM으로 제어권을 이전하고 MANUAL 모드로 전환합니다. 인계 또는 수동주행이 불가능하면 안전 정지합니다.

### 4.5. Jetson 인지 기능 회복

Jetson의 인지 기능이 복구되면 현재 활성 VM에서 인지 정보와 AUTO 주행 준비 상태를 확인합니다. 이후 사용자의 명시적인 재개 요청에 따라 자율주행으로 복귀합니다.

### 4.6. 장애 VM 복구 및 Standby 재합류

장애 VM이 다시 실행되면 현재 활성 VM의 제어권을 유지한 채, 복구된 VM의 실행 신원·애플리케이션 준비·상태 동기화를 확인합니다. 준비가 완료된 VM은 Standby로 재합류하며, 제어권을 자동으로 되찾지 않습니다.

### 4.7. 양쪽 Zephyr VM 사용 불가

두 주행 VM을 모두 사용할 수 없으면 주행 제어권을 비활성화하고 안전 정지합니다. Gateway의 정지 처리와 ESP32의 독립적인 명령 감시·출력 차단으로 안전 상태를 유지하며, 복구 이후에도 자동 출발하지 않습니다.

## 5. 설치 및 사용 (Getting Started)

### 5.1. 실행 환경 및 사전 준비

Xenova는 웹 관리 서비스와 Raspberry Pi 4 기반 임베디드 플랫폼으로 구성되며, 실행 목적에 따라 필요한 환경이 다릅니다.

| 구분 | 실행 환경 |
|---|---|
| 웹 관리 서비스 | Docker Engine, Docker Compose v2 |
| Xen 플랫폼 | Raspberry Pi 4 (ARM64), Xenova 시스템 이미지 |
| 플랫폼 빌드 | Ubuntu 24.04 (WSL2 또는 Linux), Yocto/BitBake |
| 게스트 OS | Debian Linux, Zephyr RTOS |
| 차량 시스템 | Jetson Nano, OAK-D-Lite, ESP32 및 RC카 구동계 |

웹 관리 서비스는 Docker Compose를 통해 별도로 실행할 수 있습니다. 실제 VM 제어와 차량 기능을 사용하려면 Xen 플랫폼과 관련 장치의 설치·연결 및 설정이 필요합니다.

### 5.2. 빌드 및 배포

**웹 관리 서비스 실행**

프로젝트 루트에서 환경변수를 설정한 뒤 Docker Compose로 서비스를 실행합니다.

```bash
cp .env.example .env

# .env의 필수 설정값 및 비밀값 수정
docker compose config
docker compose up -d --build
docker compose ps
```

Docker Compose는 Nginx, Spring Boot, MySQL, Redis, 웹 관리 콘솔 및 VM 화면 서비스를 실행합니다.

**Raspberry Pi 플랫폼 빌드**

Xenova의 임베디드 플랫폼은 Yocto 기반 이미지 빌드 및 조립 과정을 통해 배포합니다.

```text
빌드 환경 준비
    ↓
Linux·Zephyr 게스트 템플릿 준비
    ↓
Yocto 기반 dom0 이미지 빌드
    ↓
VM 템플릿 및 초기 설정 데이터 구성
    ↓
최종 이미지 조립·검증
    ↓
Raspberry Pi 4 설치 및 부팅
```

전체 빌드 절차와 환경 설정, 이미지 조립 방법은 [Xenova 빌드 가이드](docs/platform/build-guide.md)를 참고하세요.

### 5.3. 실행 및 관리 방법

**웹 관리 콘솔**

Docker Compose 실행 후 기본 HTTP 설정에서는 다음 주소로 관리 콘솔에 접근합니다.

```text
http://localhost
```

관리 콘솔에서는 VM 목록 조회·생명주기 제어, 자원 설정, 상태 모니터링, 웹 콘솔 및 이벤트 이력을 이용할 수 있습니다.

**Raspberry Pi CLI**

Xenova 이미지가 설치된 Raspberry Pi의 dom0 터미널에서는 `xenova-vm` 명령어로 VM을 관리할 수 있습니다.

```bash
# 등록된 VM 목록 조회
xenova-vm list --short

# 사용 가능한 VM 템플릿 조회
xenova-vm templates

# 특정 VM의 현재 상태 조회
xenova-vm get <VM_UUID>
```

자세한 명령어와 사용조건은 [CLI·Manager 기능 명세서](docs/platform/cli-manager-spec.md) 참고

### 5.4. 상세 기술 문서

| 문서 | 내용 |
|---|---|
| [플랫폼 구성 안내](docs/platform/README.md) | Xen, dom0/domU 및 플랫폼 구성요소 |
| [빌드 가이드](docs/platform/build-guide.md) | Yocto 이미지·VM 템플릿 빌드 및 배포 |
| [CLI·Manager 기능 명세](docs/platform/cli-manager-spec.md) | VM 관리 명령어 및 실행 계약 |
| [VM 관리 API 계약](docs/contracts/vm-api-contract-v1.md) | 웹 백엔드와 dom0-manager 연동 |
| [MQTT 메시지 계약](docs/contracts/mqtt-contract-v1.md) | VM·애플리케이션 간 메시지 구조 |
| [Gateway](platform/services/gateway/README.md) | 주행 제어권 관리 및 명령 중재 |
| [차량 화면 운영 가이드](docs/platform/display-operations-handoff.md) | HDMI·휴대폰 화면 출력 및 전환 |
| [RC카 조립 가이드](docs/vehicle/assembly-guide.md) | 하드웨어 구성 및 연결 |

