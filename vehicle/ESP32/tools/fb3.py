"""feedback-3 프레임과 세션·시각·표본 payload 의 PC 쪽 구현, 그리고 시험 대상과 바이트를 주고받는 통로.

펌웨어 점검 도구(link_bench.py, cutoff_stress.py, pi_link_check.py)가 쓴다. 표준 라이브러리만 쓰며
시리얼 포트를 열 때만 pyserial 이 필요하다. 계약 docs/contracts/gateway-feedback-3.md §2·§3·§7.
"""
import collections
import queue
import struct
import subprocess
import threading
import time

HELLO, HELLO_ACK = 0x0f00, 0x0f01
CLOCK_PROBE, CLOCK_REPLY, CLOCK_INSTALL, CLOCK_ACK = 0x0f10, 0x0f11, 0x0f12, 0x0f13
SENSOR = 0x0f15
HELLO3 = struct.Struct('<B16s32s')
ACK3 = struct.Struct('<B16s16sH32s32sB')
PROBE3 = struct.Struct('<16s16sQQ')
CLOCK3 = struct.Struct('<16s16sQQQQQQqqBiH')
INSTALL3 = struct.Struct('<16s16s16sQQQqqQQ')
CLOCK_ACK3 = struct.Struct('<16s16s16sQQBHQQ')
SENSOR3 = struct.Struct('<16s16sQQqqBiH16sQ')
REQUIRED_CAPS = 0x01f7  # Pi 가 ready 로 받는 최소 capability
MAX_FRAME = 256
REASONS = (
    'OK', 'BAD_LENGTH', 'BAD_CRC', 'SESSION_MISMATCH', 'SEQUENCE_REPLAY', 'IDEMPOTENCY_CONFLICT',
    'CLOCK_UNAVAILABLE', 'CLOCK_EXPIRED', 'COMMAND_EXPIRED', 'RANGE', 'DIRECTION_STOP_REQUIRED',
    'DEVICE_FAULT', 'CONFIG_MISMATCH', 'COUNTER_EXHAUSTED', 'E_STOP', 'GENERATION_REPLAY',
    'UNSUPPORTED_PROFILE', 'TIMESTAMP_ORDER', 'SENSOR_UNAVAILABLE', 'DEADLINE_MISMATCH', 'CLOCK_DRIFT',
)


def now_us():
    return time.monotonic_ns() // 1000


def reason_name(reason):
    return f'{reason} {REASONS[reason]}' if reason < len(REASONS) else str(reason)


def crc16(data):
    crc = 0xffff
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1 ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xffff
    return crc


def cobs_encode(raw):
    out = bytearray()
    block = bytearray()
    for byte in raw:
        if byte:
            block.append(byte)
        if not byte or len(block) == 254:
            out.append(len(block) + 1)
            out += block
            block.clear()
    out.append(len(block) + 1)
    return bytes(out + block)


def cobs_decode(data):
    out = bytearray()
    index = 0
    while index < len(data):
        code = data[index]
        block = data[index + 1:index + code]
        if code == 0 or index + code > len(data) or 0 in block:
            raise ValueError('COBS')
        out += block
        index += code
        if code < 0xff and index < len(data):
            out.append(0)
    return bytes(out)


def packet(kind, payload):
    raw = struct.pack('<HB', kind, len(payload)) + payload
    return cobs_encode(raw + struct.pack('<H', crc16(raw))) + b'\0'


def unpack(wire):
    """구분자까지 포함한 프레임 하나를 (종류, payload) 로 푼다. 잘못된 프레임이면 ValueError."""
    if not wire.endswith(b'\0') or len(wire) > MAX_FRAME:
        raise ValueError('boundary/size')
    raw = cobs_decode(wire[:-1])
    if len(raw) < 5:
        raise ValueError('short')
    kind, length = struct.unpack_from('<HB', raw)
    if len(raw) != length + 5 or crc16(raw[:-2]) != struct.unpack('<H', raw[-2:])[0]:
        raise ValueError('length/CRC')
    return kind, raw[3:-2]


class Splitter:
    """바이트 흐름을 0x00 으로 나눈다. 256 B 를 넘는 조각은 다음 0x00 까지 버린다."""

    def __init__(self):
        self._buffer = bytearray()
        self._discard = False

    def feed(self, data):
        """끝난 조각마다 구분자까지 포함한 wire 를 내준다."""
        for byte in data:
            if byte == 0:
                if not self._discard and self._buffer:
                    yield bytes(self._buffer) + b'\0'
                self._buffer.clear()
                self._discard = False
            elif not self._discard:
                if len(self._buffer) >= MAX_FRAME - 1:
                    self._buffer.clear()
                    self._discard = True
                else:
                    self._buffer.append(byte)


class Simulator:
    """호스트로 빌드한 통신 처리(test/host 의 sim_link)를 자식 프로세스로 띄운다."""

    def __init__(self, executable, ticks_per_second=0):
        self._process = subprocess.Popen([executable, str(ticks_per_second)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        self._received = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            data = self._process.stdout.read(4096)
            if not data:
                return
            self._received.put(data)

    def write(self, data):
        self._process.stdin.write(data)

    def read(self, timeout):
        try:
            return self._received.get(timeout=timeout)
        except queue.Empty:
            return b''

    def close(self):
        self._process.stdin.close()
        self._process.wait(timeout=5)


class SerialPort:
    """실보드의 USB 시리얼."""

    def __init__(self, port, baudrate=115200, boot_wait=2.0):
        import serial
        self._serial = serial.Serial()
        self._serial.port = port
        self._serial.baudrate = baudrate
        # 읽기 timeout 은 열기 전에 한 번만 정한다. pyserial 은 열린 포트의 timeout 을 바꿀 때마다 포트 설정을
        # 다시 넣는데, 프레임이 오가는 도중에 그러면 프레임이 사라진다
        self._serial.timeout = 0.01
        # 포트를 열 때 DTR·RTS 가 켜지면 DevKitC 는 리셋되거나 다운로드 모드로 들어간다
        self._serial.dtr = False
        self._serial.rts = False
        self._serial.open()
        # 그래도 리셋됐다면 부팅이 끝날 때까지 기다리고 부팅 문구를 버린다
        time.sleep(boot_wait)
        self._serial.reset_input_buffer()
        # 앞서 보내던 쪽이 프레임 중간에 끊겼다면 장치에 그 조각이 남아 있다. 구분자로 끊는다
        self.write(b'\0')

    def write(self, data):
        # flush 는 부르지 않는다. Windows 의 pyserial 은 보낼 것이 남아 있으면 50 ms 씩 쉬어서 응답을 그만큼 늦게 읽는다
        self._serial.write(data)

    def read(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            data = self._serial.read(1)
            if data:
                return data + self._serial.read(self._serial.in_waiting)
            if time.monotonic() >= deadline:
                return b''

    def close(self):
        self._serial.close()


def add_device_arguments(parser):
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--sim', metavar='실행파일', help='호스트로 빌드한 sim_link')
    target.add_argument('--port', help='실보드 시리얼 포트 (예: COM8, /dev/ttyUSB0)')
    parser.add_argument('--baud', type=int, default=115200)
    parser.add_argument('--ticks-per-second', type=int, default=0,
                        help='시뮬레이터의 양쪽 엔코더가 도는 속도 (기본 0, 멈춰 있음)')


def open_device(args):
    return Simulator(args.sim, args.ticks_per_second) if args.sim else SerialPort(args.port, args.baud)


class Peer:
    """시험 대상과 프레임 단위로 주고받는다."""

    def __init__(self, device):
        self.device = device
        self.invalid = 0
        self.broken = []  # 풀지 못한 조각. 어떻게 깨졌는지 보려고 남긴다
        self._splitter = Splitter()
        self._frames = collections.deque()

    def send(self, kind, payload):
        self.device.write(packet(kind, payload))

    def send_wire(self, wire):
        self.device.write(wire)

    def receive(self, timeout=1.0):
        """다음 프레임을 (종류, payload, 받은 시각 us) 로. 시간 안에 오지 않으면 None."""
        deadline = time.monotonic() + timeout
        while not self._frames:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            data = self.device.read(remaining)
            at = now_us()
            for wire in self._splitter.feed(data):
                try:
                    self._frames.append(unpack(wire) + (at,))
                except ValueError:
                    self.invalid += 1
                    self.broken.append(wire)
        return self._frames.popleft()
